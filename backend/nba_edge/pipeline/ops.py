"""Operations: single-instance lock, scheduler-gap detection, daily maintenance (retention +
backup + ledger hash check), health checks and an optional dead-man's-switch ping.

Restart safety comes from the design rather than from in-memory state:
  * every job decides whether it is due from `job_runs` in the database, so a restarted
    process simply picks up where the last one stopped (catch-up on the next tick);
  * every odds slot is served at most once (`slot_completions`) and an unserved slot whose
    window passed is recorded as `missed`;
  * a gap between scheduler ticks longer than 3x the intended interval (machine asleep,
    process down, host reboot) is recorded as a `scheduler_gap` job row;
  * a DB-row lock with a heartbeat stops two schedulers from running against one database;
    a lock whose heartbeat is older than `lock_stale_min` is taken over (crashed holder), and
    a holder on this same machine whose process no longer exists is taken over at once, so a
    service manager restarting after a crash or power cut is not refused.
"""
from __future__ import annotations

import gzip
import logging
import os
import shutil
import socket
import sqlite3
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, insert, select, update
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db.paper_schema import paper_outcomes, paper_recommendations, scheduler_lock
from ..db.research_schema import nba_games
from ..db.schema import games, job_runs, slot_completions, snapshot_runs, utcnow

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)
LOCK_NAME = "scheduler"


def holder_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID exists on this machine. Errs on the side of "alive"
    (a wrong "alive" only delays takeover until the lock goes stale; a wrong "dead" could
    start a second scheduler)."""
    if pid <= 0:
        return True
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            h = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.get_last_error() != 87                 # ERROR_INVALID_PARAMETER = no such process
            try:
                code = wintypes.DWORD()
                return not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value == 259   # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001 — unknown: assume alive (falls back to the stale-heartbeat rule)
            return True
    try:
        os.kill(pid, 0)                                     # signal 0: existence check only, sends nothing
    except ProcessLookupError:
        return False
    except OSError:
        return True                                         # e.g. EPERM: exists, owned by someone else
    return True


def holder_is_dead_local(holder: str) -> bool:
    """A holder on THIS host whose process no longer exists (crash, kill -9, power loss then
    reboot). Holders on other hosts can only be judged by heartbeat age."""
    host, _, pid = holder.rpartition(":")
    if host != socket.gethostname() or not pid.isdigit() or int(pid) == os.getpid():
        return False
    return not _pid_alive(int(pid))


# ------------------------------------------------------------------ lock
def acquire_lock(engine: Engine, holder: str, now: datetime, stale_min: int) -> tuple[bool, dict | None]:
    """Take the scheduler lock if free, ours, or stale. Returns (acquired, current_holder_row)."""
    with engine.begin() as conn:
        q = select(scheduler_lock).where(scheduler_lock.c.name == LOCK_NAME)
        if engine.dialect.name == "postgresql":
            q = q.with_for_update()
        row = conn.execute(q).mappings().first()
        if row is None:
            conn.execute(insert(scheduler_lock).values(name=LOCK_NAME, holder=holder, heartbeat_at=now))
            return True, None
        row = dict(row)
        stale = (now - row["heartbeat_at"]) > timedelta(minutes=stale_min)
        dead = not stale and row["holder"] != holder and holder_is_dead_local(row["holder"])
        if row["holder"] == holder or stale or dead:
            conn.execute(update(scheduler_lock).where(scheduler_lock.c.name == LOCK_NAME).values(holder=holder, heartbeat_at=now))
            if row["holder"] != holder:
                why = "its process is gone" if dead else "heartbeat stale"
                conn.execute(insert(job_runs).values(job_name="scheduler_lock", started_at=now, finished_at=now, status="ok",
                                                     detail=f"took over lock from {row['holder']} ({why}; heartbeat {row['heartbeat_at'].isoformat()})"))
            return True, row
        return False, row


def heartbeat(engine: Engine, holder: str, now: datetime) -> bool:
    with engine.begin() as conn:
        n = conn.execute(update(scheduler_lock).where(and_(scheduler_lock.c.name == LOCK_NAME, scheduler_lock.c.holder == holder))
                         .values(heartbeat_at=now)).rowcount
    return bool(n)


def release_lock(engine: Engine, holder: str) -> None:
    from sqlalchemy import delete
    with engine.begin() as conn:
        conn.execute(delete(scheduler_lock).where(and_(scheduler_lock.c.name == LOCK_NAME, scheduler_lock.c.holder == holder)))


# ------------------------------------------------------------------ gaps
def record_gap_if_any(engine: Engine, now: datetime, expected_min: int) -> dict | None:
    """Called at the start of each tick: if the previous tick started more than 3x the
    intended interval ago, record the outage window. Missed odds slots inside the window are
    recorded separately by run_due (slot_completions status 'missed')."""
    with engine.connect() as conn:
        prev = conn.execute(select(func.max(job_runs.c.started_at)).where(and_(job_runs.c.job_name == "scheduler_tick",
                                                                               job_runs.c.started_at < now))).scalar()
    if prev is None:
        return None
    gap = (now - prev).total_seconds() / 60
    if gap <= max(3 * expected_min, 10):
        return None
    detail = f"no scheduler tick for {gap:.0f} min ({prev.isoformat()} -> {now.isoformat()}); jobs catch up on this tick"
    with engine.begin() as conn:
        conn.execute(insert(job_runs).values(job_name="scheduler_gap", started_at=prev, finished_at=now, status="missed", detail=detail))
    log.warning(detail)
    return {"gap_min": round(gap, 1), "from": prev.isoformat(), "to": now.isoformat()}


# ------------------------------------------------------------------ maintenance
def sqlite_path(engine: Engine) -> str | None:
    if engine.dialect.name != "sqlite":
        return None
    db = engine.url.database
    return None if not db or db == ":memory:" else os.path.abspath(db)


def backup_database(engine: Engine, dest_dir: str, keep: int, now: datetime) -> dict:
    """SQLite: consistent online copy (sqlite3 backup API) -> gzip, keep the newest `keep`.
    Postgres: not done here — use the host's pg_dump / provider snapshots (documented)."""
    path = sqlite_path(engine)
    if not path:
        return {"status": "skipped", "detail": f"{engine.dialect.name}: use pg_dump or provider snapshots"}
    os.makedirs(dest_dir, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    raw = os.path.join(dest_dir, f"nba_edge-{stamp}.sqlite3")
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(raw)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    with open(raw, "rb") as f_in, gzip.open(raw + ".gz", "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw)
    existing = sorted(f for f in os.listdir(dest_dir) if f.startswith("nba_edge-") and f.endswith(".sqlite3.gz"))
    removed = existing[:-keep] if keep > 0 else []
    for f in removed:
        os.remove(os.path.join(dest_dir, f))
    size = os.path.getsize(raw + ".gz")
    return {"status": "ok", "file": raw + ".gz", "bytes": size, "rotated_out": len(removed)}


def run_maintenance(engine: Engine, settings: Settings, now: datetime) -> dict:
    from ..model.paper import verify_hashes
    from ..model.retention import prune_evaluations
    out = {"prune": prune_evaluations(engine, keep_days=settings.prune_keep_days),
           "ledger_hash_check": verify_hashes(engine),
           "backup": backup_database(engine, settings.backup_dir, settings.backup_keep, now)}
    bad = out["ledger_hash_check"]["mismatched_ids"]
    status = "failed" if bad else ("ok" if out["backup"]["status"] in ("ok", "skipped") else "partial")
    with engine.begin() as conn:
        conn.execute(insert(job_runs).values(job_name="maintenance", started_at=now, finished_at=utcnow(), status=status,
                                             detail=(f"pruned evals={out['prune']['evaluations_deleted']} backup={out['backup']['status']} "
                                                     f"ledger_hashes={out['ledger_hash_check']['checked']} mismatched={len(bad)}")))
    return {"status": status, **out}


def maintenance_due(engine: Engine, settings: Settings, now: datetime) -> bool:
    local = now.astimezone(ET)
    if local.hour < settings.maintenance_hour_et:
        return False
    today_start = datetime.combine(local.date(), datetime.min.time(), ET).replace(hour=settings.maintenance_hour_et)
    with engine.connect() as conn:
        last = conn.execute(select(func.max(job_runs.c.started_at)).where(and_(job_runs.c.job_name == "maintenance",
                                                                               job_runs.c.status.in_(["ok", "partial"])))).scalar()
    return last is None or last < today_start


# ------------------------------------------------------------------ health
LEVELS = {"ok": 0, "warn": 1, "critical": 2}


def health_report(engine: Engine, settings: Settings, now: datetime | None = None) -> dict:
    """Freshness of every scheduled job, failures, gaps, missed slots and grading backlog.
    `level` is the worst check: ok (exit 0) / warn (exit 1) / critical (exit 2)."""
    now = now or utcnow()
    checks: list[dict] = []

    def add(name, level, detail, **extra):
        checks.append({"check": name, "level": level, "detail": detail, **extra})

    day = now.astimezone(ET).date().isoformat()
    with engine.connect() as conn:
        def last(job, ok_only=True):
            q = select(job_runs).where(job_runs.c.job_name == job)
            if ok_only:
                q = q.where(job_runs.c.status.in_(["ok", "partial"]))
            return conn.execute(q.order_by(job_runs.c.started_at.desc()).limit(1)).mappings().first()

        games_today = conn.execute(select(func.count()).select_from(nba_games).where(and_(nba_games.c.date_et == day,
                                                                                          nba_games.c.season_type.in_([2, 3, 5])))).scalar() or 0
        upcoming_odds = conn.execute(select(func.count()).select_from(games).where(and_(games.c.game_date_et == day, games.c.commence_time > now))).scalar() or 0
        tick = last("scheduler_tick", ok_only=False)
        lock = conn.execute(select(scheduler_lock)).mappings().first()
        failures = dict(conn.execute(select(job_runs.c.job_name, func.count()).where(and_(
            job_runs.c.status == "failed", job_runs.c.started_at >= now - timedelta(hours=24))).group_by(job_runs.c.job_name)).all())
        gaps = [dict(r) for r in conn.execute(select(job_runs.c.started_at, job_runs.c.finished_at, job_runs.c.detail).where(and_(
            job_runs.c.job_name == "scheduler_gap", job_runs.c.finished_at >= now - timedelta(hours=24)))).mappings()]
        missed = [r[0] for r in conn.execute(select(slot_completions.c.slot_key).where(and_(
            slot_completions.c.status == "missed", slot_completions.c.completed_at >= now - timedelta(hours=24))))]
        snap_ok = conn.execute(select(snapshot_runs).where(snapshot_runs.c.status.in_(["ok", "partial"]))
                               .order_by(snapshot_runs.c.started_at.desc()).limit(1)).mappings().first()
        snap_any = conn.execute(select(snapshot_runs).order_by(snapshot_runs.c.id.desc()).limit(1)).mappings().first()
        jobs = {j: last(j) for j in ("ingest_espn", "ingest_injuries:espn", "model_run", "grade", "maintenance")}
        backlog = conn.execute(select(func.count()).select_from(paper_recommendations)
                               .outerjoin(paper_outcomes, paper_outcomes.c.recommendation_id == paper_recommendations.c.id)
                               .join(nba_games, nba_games.c.espn_event_id == paper_recommendations.c.espn_event_id)
                               .where(and_(paper_outcomes.c.recommendation_id.is_(None), nba_games.c.start_time < now - timedelta(hours=12)))).scalar() or 0

    def age(dt):
        return round((now - dt).total_seconds() / 60, 1) if dt else None

    # scheduler liveness
    if not tick:
        add("scheduler", "critical", "no scheduler tick recorded yet")
    else:
        a = age(tick["started_at"])
        lvl = "ok" if a <= 3 * settings.tick_expected_min else ("warn" if a <= 12 * settings.tick_expected_min else "critical")
        add("scheduler", lvl, f"last tick {a} min ago (status {tick['status']}); expected every {settings.tick_expected_min} min", age_min=a)
    if lock:
        add("scheduler_lock", "ok" if age(lock["heartbeat_at"]) <= settings.lock_stale_min else "warn",
            f"held by {lock['holder']}, heartbeat {age(lock['heartbeat_at'])} min ago")
    # odds capture
    if snap_any and snap_any["status"] in ("blocked_no_key", "blocked_auth", "quota_exhausted"):
        add("odds_capture", "critical", f"last snapshot attempt: {snap_any['status']} — {snap_any['detail'] or ''}"[:200])
    elif upcoming_odds or games_today:
        a = age(snap_ok["started_at"]) if snap_ok else None
        lvl = "ok" if a is not None and a <= 6 * 60 else "warn"
        add("odds_capture", lvl, f"last successful snapshot {a} min ago; {upcoming_odds} upcoming odds events today" if a is not None
            else "no successful snapshot yet", age_min=a)
    else:
        add("odds_capture", "ok", "no games today — nothing to capture")
    # research jobs vs their cadence (3x tolerance)
    cadence = {"ingest_espn": settings.espn_ingest_every_min, "ingest_injuries:espn": settings.injuries_every_min}
    if games_today:
        cadence["model_run"] = settings.model_every_min
    for job, every in cadence.items():
        r = jobs[job]
        a = age(r["started_at"]) if r else None
        add(job, "ok" if a is not None and a <= 3 * every else "warn",
            f"last ok {a} min ago (every {every} min)" if a is not None else "never succeeded", age_min=a)
    r = jobs["maintenance"]
    a = age(r["started_at"]) if r else None
    add("maintenance_backup", "ok" if a is not None and a <= 36 * 60 else "warn",
        f"last daily maintenance {round(a / 60, 1)} h ago" if a is not None else "no daily maintenance/backup yet", age_min=a)
    # failures, gaps, missed slots, grading backlog
    add("job_failures_24h", "warn" if failures else "ok", ", ".join(f"{k}×{v}" for k, v in failures.items()) or "none", counts=failures)
    add("scheduler_gaps_24h", "warn" if gaps else "ok", "; ".join(g["detail"] for g in gaps)[:400] or "none", count=len(gaps))
    add("missed_odds_slots_24h", "warn" if missed else "ok", f"{len(missed)} missed" + (": " + ", ".join(missed[:5]) if missed else ""),
        count=len(missed))
    add("paper_grading_backlog", "warn" if backlog else "ok",
        f"{backlog} paper recommendation(s) for games that started >12 h ago are still ungraded" if backlog else "none", count=backlog)
    path = sqlite_path(engine)
    size = os.path.getsize(path) if path and os.path.exists(path) else None
    if path and size is not None:
        free = shutil.disk_usage(os.path.dirname(path)).free
        add("disk", "ok" if free > 2 * size and free > 1_000_000_000 else "warn",
            f"database {size / 1e6:.0f} MB, {free / 1e9:.1f} GB free on its volume", db_bytes=size, free_bytes=free)
    level = max((c["level"] for c in checks), key=LEVELS.get, default="ok")
    return {"as_of": now.isoformat(), "level": level, "games_today": games_today, "checks": checks}


# ------------------------------------------------------------------ dead-man's switch
def ping(settings: Settings, failed: bool = False) -> None:
    """GET the configured ping URL (optional). Sends nothing but the request itself."""
    url = settings.healthcheck_ping_url.reveal()
    if not url:
        return
    try:
        urllib.request.urlopen(url.rstrip("/") + ("/fail" if failed else ""), timeout=10).read(64)
    except Exception as e:  # noqa: BLE001 — monitoring must never break the scheduler
        log.warning("healthcheck ping failed: %s", type(e).__name__)
