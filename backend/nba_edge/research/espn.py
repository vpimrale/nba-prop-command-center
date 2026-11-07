"""ESPN public NBA feed (site.api.espn.com) — free, unauthenticated, undocumented.

Used for: daily scoreboards (schedule + results), per-game box scores (every player,
incl. bench and DNPs, with shot attempts / rebounds split / turnovers / starter flag),
team rosters and the league injury list. ESPN publishes no API terms for this feed; it is
used here at low volume for personal research with caching (each final box score is
fetched once) and a polite request delay.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import httpx

from ..odds.client import ApiResponse, NotFound, OddsApiError, Usage
from ..odds.normalize import game_date_et, parse_ts

log = logging.getLogger(__name__)
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"


class EspnClient:
    def __init__(self, *, base_url: str = BASE, transport=None, sleep: Callable[[float], None] = time.sleep,
                 min_interval_s: float = 0.25, max_retries: int = 3, on_request=None, timeout: float = 20.0):
        self._http = httpx.Client(base_url=base_url.rstrip("/") + "/", transport=transport, timeout=timeout,
                                  headers={"User-Agent": "nba-edge/0.3 (personal research)", "Accept-Encoding": "gzip"})
        self._sleep = sleep
        self._min_interval = min_interval_s
        self._last = 0.0
        self._retries = max_retries
        self._on_request = on_request

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: dict | None = None) -> ApiResponse:
        attempt = 0
        while True:
            attempt += 1
            wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            started = time.monotonic()
            self._last = started
            status = error = None
            try:
                r = self._http.get(path.lstrip("/"), params=params or {})
                status = r.status_code
                if status == 200:
                    try:
                        return ApiResponse(r.json(), Usage(None, None, None), status, path, int((time.monotonic() - started) * 1000))
                    except ValueError:
                        error = "HTTP 200 with non-JSON body"
                        raise OddsApiError(error) from None
                error = f"HTTP {status}: {r.text[:120]}"
                exc = NotFound(error, status=status) if status == 404 else OddsApiError(error, status=status, retryable=status in (429,) or status >= 500)
            except httpx.TransportError as e:
                error = f"network error: {type(e).__name__}: {e}"
                exc = OddsApiError(error, retryable=True)
            finally:
                if self._on_request:
                    try:
                        self._on_request("/espn/" + path.lstrip("/"), status, None, int((time.monotonic() - started) * 1000), error)
                    except Exception:
                        log.exception("request hook failed")
            if exc.retryable and attempt <= self._retries:
                self._sleep(min(20.0, 1.0 * 2 ** (attempt - 1)))
                continue
            raise exc

    def scoreboard(self, yyyymmdd: str) -> dict:
        return self.get("scoreboard", {"dates": yyyymmdd, "limit": 100}).data

    def summary(self, event_id: str) -> dict:
        return self.get("summary", {"event": event_id}).data

    def injuries(self) -> dict:
        return self.get("injuries").data

    def roster(self, team_abbr: str) -> dict:
        return self.get(f"teams/{team_abbr.lower()}/roster").data


# ----------------------------------------------------------------- parsers (pure)
@dataclass
class GameRow:
    espn_event_id: str
    season: int
    season_type: int
    date_et: str
    start_time: datetime | None
    home: str
    away: str
    home_score: int | None
    away_score: int | None
    status: str
    completed: bool


def parse_scoreboard(data: dict) -> list[GameRow]:
    out = []
    for ev in data.get("events") or []:
        comp = (ev.get("competitions") or [{}])[0]
        teams = {c.get("homeAway"): c for c in comp.get("competitors") or []}
        if "home" not in teams or "away" not in teams:
            continue
        st = (ev.get("status") or {}).get("type") or {}
        start = parse_ts(ev.get("date"))
        season = ev.get("season") or {}

        def score(c):
            try:
                return int(c.get("score")) if c.get("score") not in (None, "") else None
            except (TypeError, ValueError):
                return None
        out.append(GameRow(str(ev["id"]), int(season.get("year") or 0), int(season.get("type") or 0), game_date_et(start) or "",
                           start, teams["home"]["team"]["abbreviation"], teams["away"]["team"]["abbreviation"],
                           score(teams["home"]), score(teams["away"]), st.get("name") or "", bool(st.get("completed"))))
    return out


_LABEL_MAP = {"MIN": "minutes", "PTS": "pts", "REB": "reb", "AST": "ast", "TO": "tov", "STL": "stl", "BLK": "blk",
              "OREB": "oreb", "DREB": "dreb", "PF": "pf", "+/-": "plus_minus"}
_SPLIT_MAP = {"FG": ("fgm", "fga"), "3PT": ("fg3m", "fg3a"), "FT": ("ftm", "fta")}


def _num(v):
    if v in (None, "", "--"):
        return None
    try:
        return int(str(v).replace("+", ""))
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return None


def parse_box(summary: dict, game: GameRow) -> tuple[list[dict], list[dict]]:
    """-> (player rows, team rows). Players who did not play get minutes=0, played=False."""
    players: list[dict] = []
    teams: list[dict] = []
    box = summary.get("boxscore") or {}
    for tb in box.get("players") or []:
        abbr = tb.get("team", {}).get("abbreviation")
        if abbr not in (game.home, game.away):
            continue
        opp = game.away if abbr == game.home else game.home
        for grp in tb.get("statistics") or []:
            labels = grp.get("labels") or []
            for a in grp.get("athletes") or []:
                ath = a.get("athlete") or {}
                if not ath.get("id"):
                    continue
                row = dict(espn_event_id=game.espn_event_id, athlete_id=str(ath["id"]), player_name=ath.get("displayName") or "",
                           position=(ath.get("position") or {}).get("abbreviation"), team=abbr, opponent=opp, is_home=abbr == game.home,
                           starter=a.get("starter") if isinstance(a.get("starter"), bool) else None,
                           dnp_reason=(a.get("reason") or None), season=game.season, season_type=game.season_type,
                           date_et=game.date_et, start_time=game.start_time)
                stats = a.get("stats") or []
                vals = {k: None for k in ("pts", "reb", "ast", "fg3m", "fg3a", "fgm", "fga", "ftm", "fta", "oreb", "dreb", "stl", "blk", "tov", "pf", "plus_minus")}
                minutes = 0.0
                for i, lab in enumerate(labels):
                    if i >= len(stats):
                        break
                    if lab in _SPLIT_MAP:
                        parts = str(stats[i]).split("-")
                        if len(parts) == 2:
                            vals[_SPLIT_MAP[lab][0]], vals[_SPLIT_MAP[lab][1]] = _num(parts[0]), _num(parts[1])
                    elif lab == "MIN":
                        m = _num(stats[i])
                        minutes = float(m) if m is not None else 0.0
                    elif lab in _LABEL_MAP:
                        vals[_LABEL_MAP[lab]] = _num(stats[i])
                played = bool(stats) and minutes > 0 and not a.get("didNotPlay")
                if not played:
                    vals = {k: (0 if k != "plus_minus" else None) for k in vals}
                    minutes = 0.0
                    if not row["dnp_reason"]:
                        row["dnp_reason"] = "DNP" if a.get("didNotPlay") or not stats else None
                else:
                    row["dnp_reason"] = None  # ESPN sometimes carries a stale reason on active players
                players.append({**row, **vals, "minutes": minutes, "played": played})
    # Team rows from summed player lines (consistent with player data) + game length.
    for abbr in (game.home, game.away):
        rows = [p for p in players if p["team"] == abbr and p["played"]]
        if not rows:
            continue
        s = lambda k: sum((r.get(k) or 0) for r in rows)  # noqa: E731
        opp = game.away if abbr == game.home else game.home
        team_min = sum(r["minutes"] for r in rows)
        length = 48.0 + 5 * max(0, round((team_min / 5 - 48) / 5)) if team_min else 48.0
        poss = s("fga") + 0.44 * s("fta") - s("oreb") + s("tov")
        teams.append(dict(espn_event_id=game.espn_event_id, team=abbr, opponent=opp, is_home=abbr == game.home, pts=s("pts"),
                          opp_pts=None, fga=s("fga"), fta=s("fta"), oreb=s("oreb"), tov=s("tov"), reb=s("reb"), ast=s("ast"),
                          fg3m=s("fg3m"), minutes=length, possessions=poss, season=game.season, season_type=game.season_type, date_et=game.date_et))
    by = {t["team"]: t for t in teams}
    for t in teams:
        o = by.get(t["opponent"])
        t["opp_pts"] = o["pts"] if o else None
    return players, teams


@dataclass
class InjuryRow:
    athlete_id: str | None
    player_name: str
    team: str | None
    status: str
    detail: str | None
    reported_at: datetime | None


def parse_injuries(data: dict, team_lookup: Callable[[str], str | None]) -> list[InjuryRow]:
    out = []
    for t in data.get("injuries") or []:
        team = team_lookup(t.get("displayName") or "")
        for i in t.get("injuries") or []:
            ath = i.get("athlete") or {}
            aid = None
            for link in ath.get("links") or []:
                href = link.get("href") or ""
                if "/id/" in href:
                    aid = href.split("/id/")[1].split("/")[0]
                    break
            out.append(InjuryRow(aid, ath.get("displayName") or "", team, i.get("status") or "Unknown",
                                 i.get("shortComment") or i.get("longComment"), parse_ts(i.get("date"))))
    return out


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
