"""Snapshot scheduling.

Slots per ET game day:
  * fixed  — e.g. 10:00 and 15:00 ET: every not-yet-started game of the day
             (initial daily research capture; post-official-injury-report capture)
  * pretip — N minutes before each game's tip-off (e.g. 60)
  * closing — the LAST pre-tip slot (e.g. 10 min before tip): closing-line reference

`run_due()` is meant to be invoked every ~5 minutes (cron, systemd timer, or the
built-in `run-scheduler` loop). Each slot is served at most once (slot_completions),
so a crashed/restarted scheduler can't double-spend credits; a slot whose window passed
unserved is recorded as 'missed' so the data-status view can show the gap honestly.
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, insert, select
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db.schema import games, job_runs, slot_completions, utcnow
from .snapshots import open_provider, refresh_events, run_snapshot

ET = ZoneInfo("America/New_York")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Slot:
    key: str
    reason: str                 # fixed | pretip | closing
    due_at: datetime
    deadline: datetime
    event_ids: tuple[str, ...]  # games this slot covers


def fixed_slot_times(settings: Settings) -> list[str]:
    """Configured fixed slots, plus every `snapshot_interval_min` between interval start/end
    (ET) when set — useful on PropLine, where a whole-slate snapshot is ~1 request."""
    times = set(settings.fixed_slots_et)
    if settings.snapshot_interval_min:
        sh, sm = (int(x) for x in settings.interval_start_et.split(":"))
        eh, em = (int(x) for x in settings.interval_end_et.split(":"))
        t, end = sh * 60 + sm, eh * 60 + em
        while t <= end:
            times.add(f"{t // 60:02d}:{t % 60:02d}")
            t += settings.snapshot_interval_min
    return sorted(times)


def plan_slots(game_rows: list[dict], day: date, settings: Settings) -> list[Slot]:
    day_games = [g for g in game_rows if g["game_date_et"] == day.isoformat()]
    slots: list[Slot] = []
    grace = timedelta(minutes=settings.slot_grace_min)
    for hhmm in fixed_slot_times(settings):
        hh, mm = (int(x) for x in hhmm.split(":"))
        due = datetime.combine(day, time(hh, mm), ET).astimezone(timezone.utc)
        covered = tuple(g["provider_event_id"] for g in day_games if g["commence_time"] > due)
        if covered:
            slots.append(Slot(f"{day.isoformat()}:fixed:{hhmm}", "fixed", due, due + grace, covered))
    offsets = sorted(set(settings.pretip_offsets_min), reverse=True)
    for g in day_games:
        tip = g["commence_time"]
        for off in offsets:
            reason = "closing" if off == offsets[-1] else "pretip"
            due = tip - timedelta(minutes=off)
            slots.append(Slot(f"{day.isoformat()}:{reason}:{off}:{g['provider_event_id']}", reason, due, tip, (g["provider_event_id"],)))
    return sorted(slots, key=lambda s: s.due_at)


def due_slots(slots: list[Slot], now: datetime, done: set[str]) -> list[Slot]:
    return [s for s in slots if s.key not in done and s.due_at <= now < s.deadline]


def missed_slots(slots: list[Slot], now: datetime, done: set[str]) -> list[Slot]:
    return [s for s in slots if s.key not in done and s.deadline <= now]


def _games_for(engine: Engine, day: date, provider: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(select(games).where(and_(games.c.provider == provider, games.c.game_date_et == day.isoformat())))
        return [dict(r._mapping) for r in rows]


def _done_keys(engine: Engine, prefix: str) -> set[str]:
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(select(slot_completions.c.slot_key).where(slot_completions.c.slot_key.like(prefix + "%")))}


def _events_fresh(engine: Engine, now: datetime, settings: Settings, provider: str) -> bool:
    with engine.connect() as conn:
        last_ok = conn.execute(select(job_runs.c.finished_at).where(and_(job_runs.c.job_name == f"refresh_events:{provider}", job_runs.c.status == "ok"))
                               .order_by(job_runs.c.finished_at.desc()).limit(1)).scalar()
    return bool(last_ok) and (now - last_ok) < timedelta(minutes=settings.events_refresh_min)


def run_due(engine: Engine, settings: Settings, *, now: datetime | None = None, transport=None, sleep=None) -> dict:
    """One scheduler tick. Returns a summary dict (also safe to print)."""
    simulated = now is not None
    now = now or utcnow()
    t0 = _time.monotonic()
    day = now.astimezone(ET).date()
    summary: dict = {"now": now.isoformat(), "day_et": day.isoformat(), "runs": [], "missed": []}
    with engine.begin() as conn:
        tick_id = conn.execute(insert(job_runs).values(job_name="scheduler_tick", started_at=now, status="running")).inserted_primary_key[0]
    client, recorder = open_provider(settings, engine, transport=transport, sleep=sleep, clock=(lambda: now) if simulated else utcnow)
    summary["provider"] = client.name
    try:
        if not client.has_credentials():
            summary["status"] = "blocked_no_key"
            summary["detail"] = f"No API key configured for {client.name} — no provider calls made."
        else:
            if not _events_fresh(engine, now, settings, client.name):
                summary["events_refresh"] = refresh_events(engine, client, now)
            slots = plan_slots(_games_for(engine, day, client.name), day, settings)
            done = _done_keys(engine, day.isoformat())
            for s in missed_slots(slots, now, done):
                with engine.begin() as conn:
                    conn.execute(insert(slot_completions).values(slot_key=s.key, run_id=None, completed_at=now, status="missed"))
                summary["missed"].append(s.key)
            due = due_slots(slots, now, done)
            # Group due slots by reason so simultaneous tips share one run; each event is
            # fetched once per tick even if several slots cover it.
            fetched_by: dict[str, tuple[int, str]] = {}  # event id -> (run id, run status) within this tick
            for reason in ("closing", "pretip", "fixed"):
                group = [s for s in due if s.reason == reason]
                if not group:
                    continue
                ids = [e for e in sorted({e for s in group for e in s.event_ids}) if e not in fetched_by]
                live_ids = _still_pregame(engine, ids, now, client.name)
                if not live_ids:
                    # Every event was already captured earlier in this same tick (or has tipped
                    # off): the slot is served by that capture — no second paid call.
                    with engine.begin() as conn:
                        for s in group:
                            prior = next((fetched_by[e] for e in s.event_ids if e in fetched_by), (None, "no_events"))
                            conn.execute(insert(slot_completions).values(slot_key=s.key, run_id=prior[0], completed_at=now, status=prior[1]))
                    continue
                result = run_snapshot(engine, settings, client, recorder, event_ids=live_ids, reason=reason,
                                      slot_key=f"{day.isoformat()}:{reason}:tick:{now.strftime('%H%M')}", now=now)
                for e in live_ids:
                    fetched_by[e] = (result["id"], result["status"])
                with engine.begin() as conn:
                    for s in group:
                        conn.execute(insert(slot_completions).values(slot_key=s.key, run_id=result["id"], completed_at=now, status=result["status"]))
                summary["runs"].append({"reason": reason, "slots": [s.key for s in group], "event_ids": live_ids,
                                        "status": result["status"], "rows_inserted": result["rows_inserted"],
                                        "credits_reported": result["credits_reported"]})
            summary["status"] = "ok"
    finally:
        client.close()
        with engine.begin() as conn:
            conn.execute(job_runs.update().where(job_runs.c.id == tick_id).values(
                finished_at=now + timedelta(seconds=_time.monotonic() - t0), status=summary.get("status", "failed"),
                detail=f"runs={len(summary['runs'])} missed={len(summary['missed'])}"))
    return summary


def _still_pregame(engine: Engine, ids: list[str], now: datetime, provider: str) -> list[str]:
    if not ids:
        return []
    with engine.connect() as conn:
        rows = conn.execute(select(games.c.provider_event_id).where(and_(games.c.provider == provider,
                            games.c.provider_event_id.in_(ids), games.c.commence_time > now)).order_by(games.c.commence_time))
        return [r[0] for r in rows]
