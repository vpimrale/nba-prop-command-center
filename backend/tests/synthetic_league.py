"""SYNTHETIC league for model tests: 4 teams, players with KNOWN per-minute Poisson rates and
minutes, a season of games, one upcoming slate game, and synthetic PropLine odds rows.
Nothing here is real data."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import insert

from nba_edge.db.research_schema import nba_games, nba_player_games, nba_team_games
from nba_edge.db.schema import games as odds_games, odds_current
from nba_edge.odds.math import american_to_decimal
from nba_edge.odds.normalize import game_date_et, quote_key

TEAMS = ["AAA", "BBB", "CCC", "DDD"]
SEASON = 2027
STAT_COLS = ("pts", "reb", "ast", "fg3m", "stl", "blk", "tov")


def roster(team: str) -> list[dict]:
    """8 players per team: 5 starters (~32 min), 3 bench (~16 min). Rates per minute."""
    out = []
    for i in range(8):
        starter = i < 5
        out.append({"athlete_id": f"{team}{i}", "name": f"Synth {team} Player{i}", "team": team, "pos": ["PG", "SG", "SF", "PF", "C"][i % 5],
                    "minutes": 32.0 if starter else 16.0, "starter": starter,
                    "rate": {"pts": 0.55 - 0.03 * i, "reb": 0.18 + (0.08 if i % 5 == 4 else 0), "ast": 0.12 if i % 5 == 0 else 0.06,
                             "fg3m": 0.06, "stl": 0.03, "blk": 0.02 + (0.03 if i % 5 == 4 else 0), "tov": 0.05}})
    return out


def build(engine, start: datetime, n_days: int = 40, seed: int = 1, slate_in_hours: float = 5.0) -> dict:
    rng = np.random.default_rng(seed)
    rosters = {t: roster(t) for t in TEAMS}
    gid = 1000
    pg_rows, game_rows, team_rows = [], [], []
    for d in range(n_days):
        day = start + timedelta(days=d)
        pairs = [("AAA", "BBB"), ("CCC", "DDD")] if d % 2 == 0 else [("AAA", "CCC"), ("BBB", "DDD")]
        for home, away in pairs:
            gid += 1
            eid = str(gid)
            tip = day.replace(hour=0, minute=30)
            game_rows.append(dict(espn_event_id=eid, season=SEASON, season_type=2, date_et=game_date_et(tip), start_time=tip, home=home, away=away,
                                  home_score=None, away_score=None, status="STATUS_FINAL", completed=True, box_ingested=True, updated_at=tip))
            score = {}
            for team, opp in ((home, away), (away, home)):
                tot = {c: 0 for c in STAT_COLS}
                for p in rosters[team]:
                    m = float(max(1, min(46, rng.normal(p["minutes"], 4))))
                    row = dict(espn_event_id=eid, athlete_id=p["athlete_id"], player_name=p["name"], position=p["pos"], team=team, opponent=opp,
                               is_home=team == home, starter=p["starter"], played=True, dnp_reason=None, minutes=m, season=SEASON, season_type=2,
                               date_et=game_date_et(tip), start_time=tip, fgm=0, fga=int(m * 0.4), ftm=0, fta=int(m * 0.1), oreb=1, dreb=3,
                               fg3a=3, pf=2, plus_minus=0)
                    for c in STAT_COLS:
                        row[c] = int(rng.poisson(p["rate"][c] * m))
                        tot[c] += row[c]
                    pg_rows.append(row)
                score[team] = tot
            for team, opp in ((home, away), (away, home)):
                t = score[team]
                team_rows.append(dict(espn_event_id=eid, team=team, opponent=opp, is_home=team == home, pts=t["pts"], opp_pts=score[opp]["pts"],
                                      fga=130, fta=25, oreb=10, tov=t["tov"], reb=t["reb"], ast=t["ast"], fg3m=t["fg3m"], minutes=48.0,
                                      possessions=100.0 + (2 if team in ("AAA", "BBB") else -2), season=SEASON, season_type=2, date_et=game_date_et(tip)))
            game_rows[-1]["home_score"], game_rows[-1]["away_score"] = score[home]["pts"], score[away]["pts"]
    now = start + timedelta(days=n_days, hours=15)   # ~10-11am ET, slate later the same ET day
    slate_tip = now + timedelta(hours=slate_in_hours)
    slate = dict(espn_event_id="9999", season=SEASON, season_type=2, date_et=game_date_et(slate_tip), start_time=slate_tip, home="AAA", away="BBB",
                 home_score=None, away_score=None, status="STATUS_SCHEDULED", completed=False, box_ingested=False, updated_at=now)
    with engine.begin() as conn:
        conn.execute(insert(nba_games), game_rows + [slate])
        conn.execute(insert(nba_player_games), pg_rows)
        conn.execute(insert(nba_team_games), team_rows)
    return {"now": now, "slate": slate, "rosters": rosters}


def add_odds(engine, now: datetime, slate: dict, quotes: list[tuple]) -> None:
    """quotes: (book, market_key, market_code, player_name, side, line, american, is_alt)."""
    ev = "pl-1"
    with engine.begin() as conn:
        conn.execute(insert(odds_games).values(provider="propline", provider_event_id=ev, sport_key="basketball_nba", commence_time=slate["start_time"],
                                               game_date_et=slate["date_et"], home_team="Synth Home", away_team="Synth Away", first_seen_at=now,
                                               last_seen_at=now, espn_event_id=slate["espn_event_id"]))
        rows = []
        for book, mk, code, player, side, line, american, alt in quotes:
            rows.append(dict(quote_key=quote_key("propline", ev, book, mk, player, side, line), provider="propline", sport_key="basketball_nba",
                             provider_event_id=ev, commence_time=slate["start_time"], game_date_et=slate["date_et"], home_team="Synth Home",
                             away_team="Synth Away", bookmaker=book, market_key=mk, market_code=code, is_alternate=alt, player_name=player,
                             side=side, line=line, american_odds=american, decimal_odds=american_to_decimal(american), market_status="active",
                             provider_player_id=None, line_type="milestone" if alt else "main", outcome_label=None, source_seen_at=now - timedelta(minutes=2),
                             source_changed_at=now - timedelta(minutes=30), stale=False, first_seen_at=now, last_seen_at=now, last_changed_at=now, last_run_id=1))
        conn.execute(insert(odds_current), rows)
