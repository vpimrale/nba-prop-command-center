"""Ingest ESPN schedules/box scores and injury reports into the research tables.

Idempotent: a game row is upserted every time its scoreboard day is read; a final game's
box score is fetched once (box_ingested) and replaced atomically if re-fetched.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from ..db.research_schema import injury_snapshots, nba_games, nba_player_games, nba_team_games
from ..db.schema import job_runs, utcnow
from ..odds.client import OddsApiError
from .espn import EspnClient, GameRow, parse_box, parse_injuries, parse_scoreboard
from .teams import espn_abbr

log = logging.getLogger(__name__)
WANTED_SEASON_TYPES = (2, 3, 5)   # regular, postseason, play-in (preseason is skipped)


def _upsert_game(conn, g: GameRow, now: datetime) -> None:
    vals = dict(season=g.season, season_type=g.season_type, date_et=g.date_et, start_time=g.start_time, home=g.home, away=g.away,
                home_score=g.home_score, away_score=g.away_score, status=g.status, completed=g.completed, updated_at=now)
    if conn.execute(select(nba_games.c.espn_event_id).where(nba_games.c.espn_event_id == g.espn_event_id)).first():
        conn.execute(update(nba_games).where(nba_games.c.espn_event_id == g.espn_event_id).values(**vals))
    else:
        conn.execute(insert(nba_games).values(espn_event_id=g.espn_event_id, box_ingested=False, **vals))


def ingest_box(engine: Engine, client: EspnClient, g: GameRow) -> int:
    players, teams = parse_box(client.summary(g.espn_event_id), g)
    if not players:
        return 0
    with engine.begin() as conn:
        conn.execute(delete(nba_player_games).where(nba_player_games.c.espn_event_id == g.espn_event_id))
        conn.execute(delete(nba_team_games).where(nba_team_games.c.espn_event_id == g.espn_event_id))
        conn.execute(insert(nba_player_games), players)
        if teams:
            conn.execute(insert(nba_team_games), teams)
        conn.execute(update(nba_games).where(nba_games.c.espn_event_id == g.espn_event_id).values(box_ingested=True))
    return len(players)


def ingest_dates(engine: Engine, client: EspnClient, start: date, end: date, *, now: datetime | None = None,
                 refetch: bool = False) -> dict:
    """Read every scoreboard day in [start, end]; fetch box scores of completed games not yet ingested."""
    now = now or utcnow()
    with engine.begin() as conn:
        job = conn.execute(insert(job_runs).values(job_name="ingest_espn", started_at=now, status="running")).inserted_primary_key[0]
    days = games_seen = boxes = rows = errors = 0
    notes: list[str] = []
    d = start
    while d <= end:
        try:
            board = parse_scoreboard(client.scoreboard(d.strftime("%Y%m%d")))
        except OddsApiError as e:
            errors += 1
            notes.append(f"{d}: scoreboard {e}")
            d += timedelta(days=1)
            continue
        days += 1
        # Only games between two of the 30 NBA teams: ESPN labels All-Star weekend exhibitions
        # (e.g. "STARS" vs "WORLD") as regular season, and their stats must not enter player rates.
        skipped = [g for g in board if not (espn_abbr(g.home) and espn_abbr(g.away))]
        if skipped:
            notes.append(f"{d}: skipped {len(skipped)} non-NBA-team game(s): " + ", ".join(f"{g.away}@{g.home}" for g in skipped))
        board = [g for g in board if espn_abbr(g.home) and espn_abbr(g.away)]
        with engine.begin() as conn:
            done = {r[0] for r in conn.execute(select(nba_games.c.espn_event_id).where(and_(
                nba_games.c.espn_event_id.in_([g.espn_event_id for g in board] or ["-"]), nba_games.c.box_ingested.is_(True))))}
            for g in board:
                _upsert_game(conn, g, now)
        for g in board:
            games_seen += 1
            if g.season_type not in WANTED_SEASON_TYPES or not g.completed or (g.espn_event_id in done and not refetch):
                continue
            try:
                n = ingest_box(engine, client, g)
                boxes += 1 if n else 0
                rows += n
            except OddsApiError as e:
                errors += 1
                notes.append(f"{g.espn_event_id}: box {e}")
        d += timedelta(days=1)
    status = "ok" if not errors else ("partial" if boxes or days else "failed")
    detail = f"days={days} games={games_seen} boxes={boxes} player_rows={rows} errors={errors}" + ("; " + "; ".join(notes[:10]) if notes else "")
    with engine.begin() as conn:
        conn.execute(update(job_runs).where(job_runs.c.id == job).values(finished_at=utcnow(), status=status, detail=detail))
    return {"status": status, "days": days, "games": games_seen, "boxes": boxes, "player_rows": rows, "errors": errors, "notes": notes[:10]}


# ----------------------------------------------------------------- injuries
_KEYWORDS = [  # most specific first; applied only to fresh reports (see effective_status)
    (re.compile(r"\b(ruled out|will not play|won't play|is out\b|out for)\b", re.I), "Out"),
    (re.compile(r"\bdoubtful\b", re.I), "Doubtful"),
    (re.compile(r"\bquestionable\b", re.I), "Questionable"),
    (re.compile(r"\bprobable\b", re.I), "Probable"),
    (re.compile(r"\b(will play|is available|has been cleared|cleared to play|will return)\b", re.I), "Available"),
]


def effective_status(status: str, detail: str | None, reported_at: datetime | None, now: datetime) -> str:
    """ESPN's status field is coarse (mostly Out / Day-To-Day). A fresh (<36h) comment that
    states a game status (questionable, ruled out, ...) refines it. Heuristic — documented."""
    if detail and reported_at and (now - reported_at) < timedelta(hours=36):
        for rx, label in _KEYWORDS:
            if rx.search(detail):
                return label
    return status


def ingest_injuries_espn(engine: Engine, client: EspnClient, now: datetime | None = None) -> dict:
    now = now or utcnow()
    rows = parse_injuries(client.injuries(), espn_abbr)
    with engine.begin() as conn:
        if rows:
            conn.execute(insert(injury_snapshots), [dict(captured_at=now, source="espn", athlete_id=r.athlete_id, player_name=r.player_name,
                                                         team=r.team, status=r.status, detail=r.detail, reported_at=r.reported_at) for r in rows])
        conn.execute(insert(job_runs).values(job_name="ingest_injuries:espn", started_at=now, finished_at=utcnow(), status="ok",
                                             detail=f"{len(rows)} entries"))
    return {"status": "ok", "source": "espn", "entries": len(rows)}


def ingest_injuries_propline(engine: Engine, client, sport: str = "basketball_nba", now: datetime | None = None) -> dict:
    """PropLine's free injury endpoint (sourced from ESPN, refreshed ~10 min). Stored as a
    second source; the model uses the most recent report per player across sources."""
    now = now or utcnow()
    data = client.get(f"/sports/{sport}/injuries").data or {}
    players = data.get("players") or []
    with engine.begin() as conn:
        if players:
            from ..odds.normalize import parse_ts
            conn.execute(insert(injury_snapshots), [dict(
                captured_at=now, source="propline", athlete_id=str(p["espn_athlete_id"]) if p.get("espn_athlete_id") else None,
                player_name=p.get("player_name") or "", team=espn_abbr(p.get("team_abbr") or p.get("team") or ""),
                status=p.get("status") or "Unknown", detail=p.get("detail") or p.get("injury_type"),
                reported_at=parse_ts(p.get("status_since") or p.get("source_updated_at"))) for p in players])
        conn.execute(insert(job_runs).values(job_name="ingest_injuries:propline", started_at=now, finished_at=utcnow(), status="ok",
                                             detail=f"{len(players)} entries"))
    return {"status": "ok", "source": "propline", "entries": len(players)}


def latest_injuries(engine: Engine, now: datetime, max_age_h: int = 48) -> dict[str, dict]:
    """athlete_id (or 'name:<lower name>') -> most recent report within max_age_h across sources,
    with `effective` status. Players absent from the latest reports are treated as available."""
    since = now - timedelta(hours=max_age_h)
    with engine.connect() as conn:
        last_capture = {src: t for src, t in conn.execute(
            select(injury_snapshots.c.source, injury_snapshots.c.captured_at).where(injury_snapshots.c.captured_at >= since)
            .order_by(injury_snapshots.c.captured_at))}
        out: dict[str, dict] = {}
        for src, t in last_capture.items():
            for r in conn.execute(select(injury_snapshots).where(and_(injury_snapshots.c.source == src, injury_snapshots.c.captured_at == t))).mappings():
                key = r["athlete_id"] or f"name:{r['player_name'].lower()}"
                prev = out.get(key)
                rep = r["reported_at"] or r["captured_at"]
                if prev is None or rep > prev["reported_at"]:
                    out[key] = {"status": r["status"], "detail": r["detail"], "reported_at": rep, "source": src, "team": r["team"],
                                "player_name": r["player_name"], "effective": effective_status(r["status"], r["detail"], rep, now)}
    return out
