"""A SYNTHETIC stand-in for PropLine's API, for tests only.

Shapes mirror what the real API returned on 2026-11-07 (field names, line_type values,
milestone labels, X-Daily-* headers, structured error `detail`), but every team, player,
line and price here is invented. No real PropLine data is stored in this repository
(PropLine's terms prohibit redistributing it).
"""
from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def ou(player: str, point: float, over: int, under: int, seen: datetime, pid: str | None = None) -> list[dict]:
    base = {"description": player, "point": point, "player_id": pid, "last_seen_at": iso(seen), "last_change_at": iso(seen - timedelta(minutes=5)),
            "book_updated_at": None, "side": None, "dfs_odds_type": None, "payout_multiplier": None}
    return [{**base, "name": "Over", "price": over}, {**base, "name": "Under", "price": under}]


def ladder(player: str, rungs: list[tuple[int, int]], stat: str, seen: datetime, pid: str | None = None) -> list[dict]:
    return [{"description": player, "name": f"{n}+ {stat}", "point": None, "price": price, "player_id": pid,
             "last_seen_at": iso(seen), "last_change_at": iso(seen), "side": None} for n, price in rungs]


def market(key: str, player: str, outcomes: list[dict], *, line_type: str = "main", updated: datetime, suspended: bool = False,
           period=None) -> dict:
    return {"key": key, "description": f"{player} - {key}", "line_type": line_type, "period": period, "team": None,
            "last_update": iso(updated), "suspended_at": iso(updated) if suspended else None, "outcomes": outcomes}


class FakePropLine:
    def __init__(self, now: datetime, daily_limit: int = 1000):
        self.now = now
        self.daily_limit = daily_limit
        self.used = 0
        self.events: dict[str, dict] = {}
        self.bulk_ids: set[str] | None = None        # which events the bulk endpoint returns (None = all)
        self.fail_next: dict[str, list[tuple[int, dict]]] = {}  # path -> [(status, detail)]
        self.requests: list[dict] = []

    def add_event(self, event_id: str, home: str, away: str, commence: datetime, books: dict, *, live: bool = False, espn: str | None = None):
        self.events[event_id] = {"id": event_id, "sport_key": "basketball_nba", "commence_time": iso(commence), "home_team": home,
                                 "away_team": away, "live": live, "espn_event_id": espn, "is_outright": False,
                                 "bookmakers": [{"key": k, "title": k.title(), "last_update": iso(b.get("updated", self.now)),
                                                 "pregame_only": False, "markets": b["markets"]} for k, b in books.items()]}

    def book(self, event_id: str, key: str) -> dict:
        return next(b for b in self.events[event_id]["bookmakers"] if b["key"] == key)

    def _filtered(self, ev: dict, params: dict) -> dict:
        out = copy.deepcopy(ev)
        books = set(filter(None, (params.get("bookmakers") or "").split(",")))
        mks = set(filter(None, (params.get("markets") or "").split(",")))
        out["bookmakers"] = [b for b in out["bookmakers"] if not books or b["key"] in books]
        for b in out["bookmakers"]:
            b["markets"] = [m for m in b["markets"] if not mks or m["key"] in mks]
        return out

    def handle(self, path: str, params: dict, headers: dict) -> tuple[int, object, dict]:
        self.requests.append({"path": path, "params": dict(params), "has_key": bool(headers.get("x-api-key"))})
        if not headers.get("x-api-key"):
            return 401, {"detail": {"error": "missing_api_key", "message": "Missing API key."}}, {}
        fails = self.fail_next.get(path)
        if fails:
            status, detail = fails.pop(0)
            hdr = {"Retry-After": str(detail.get("retry_after_seconds"))} if detail.get("retry_after_seconds") else {}
            return status, {"detail": detail}, hdr
        if self.used >= self.daily_limit:
            return 429, {"detail": {"error": "daily_limit_exceeded", "message": "Daily request limit reached."}}, self._quota()
        self.used += 1
        if path == "/v1/sports/basketball_nba/events":
            body: object = [{k: v for k, v in e.items() if k != "bookmakers"} for e in self.events.values()]
        elif path == "/v1/sports/basketball_nba/odds":
            ids = self.bulk_ids if self.bulk_ids is not None else set(self.events)
            body = [self._filtered(e, params) for i, e in self.events.items() if i in ids]
        elif path.startswith("/v1/sports/basketball_nba/events/") and path.endswith("/odds"):
            ev_id = path.split("/events/")[1].split("/")[0]
            if ev_id not in self.events:
                return 404, {"detail": {"error": "event_not_found", "message": "No such event"}}, self._quota()
            body = self._filtered(self.events[ev_id], params)
        else:
            return 404, {"detail": "unknown path"}, self._quota()
        return 200, body, self._quota()

    def _quota(self) -> dict:
        reset = int((self.now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp())
        return {"X-Daily-Limit": str(self.daily_limit), "X-Daily-Used": str(self.used),
                "X-Daily-Remaining": str(max(0, self.daily_limit - self.used)), "X-Daily-Reset": str(reset)}

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            status, body, headers = self.handle(request.url.path, dict(request.url.params.items()),
                                                {k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(status, json=body, headers=headers)
        return httpx.MockTransport(handler)

    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                status, body, headers = fake.handle(u.path, {k: v[0] for k, v in parse_qs(u.query).items()},
                                                    {k.lower(): v for k, v in self.headers.items()})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def standard_slate(now: datetime) -> FakePropLine:
    """Two synthetic games tonight + one tomorrow (only tonight's are in the bulk feed)."""
    f = FakePropLine(now)
    s = now - timedelta(minutes=1)
    f.add_event("1001", "Synthetic Home A", "Synthetic Away A", now + timedelta(hours=4), {
        "fanduel": {"markets": [
            market("player_points", "Test Player One", ou("Test Player One", 24.5, -110, -118, s, "nba:1"), updated=s),
            market("player_points", "Test Player One", ladder("Test Player One", [(20, -400), (25, -110), (30, 250)], "Points", s, "nba:1"),
                   line_type="milestone", updated=s),
            market("player_rebounds", "Test Player Two", ou("Test Player Two", 8.5, 100, -130, s, "nba:2"), updated=s),
        ]},
        "fanatics": {"markets": [market("player_points", "Test Player One", ou("Test Player One", 24.5, -105, -125, s, "nba:1"), updated=s)]},
        "prizepicks": {"markets": [market("player_points", "Test Player One", ou("Test Player One", 24.5, 100, 100, s), updated=s)]},
    }, espn="401000001")
    f.add_event("1002", "Synthetic Home B", "Synthetic Away B", now + timedelta(hours=7), {
        "fanduel": {"markets": [market("player_assists", "Test Player Three", ou("Test Player Three", 6.5, -120, -102, s, "nba:3"), updated=s)]},
        "fanatics": {"markets": [market("player_assists", "Test Player Three", ou("Test Player Three", 6.5, -115, -105, s, "nba:3"), updated=s)]},
    })
    f.add_event("1003", "Synthetic Home C", "Synthetic Away C", now + timedelta(hours=28), {
        "fanduel": {"markets": [market("player_points", "Test Player Four", ou("Test Player Four", 18.5, -110, -110, s, "nba:4"), updated=s)]},
    })
    f.bulk_ids = {"1001", "1002"}
    return f
