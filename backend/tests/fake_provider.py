"""A SYNTHETIC stand-in for The Odds API, for tests only.

Every event, player, line and price produced here is made up for testing and is never
written anywhere except a throwaway test database. It mimics the provider's documented
response shapes and usage headers so the pipeline can be exercised end to end without
a key or network access.
"""
from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_event(event_id: str, home: str, away: str, commence: datetime) -> dict:
    return {"id": event_id, "sport_key": "basketball_nba", "sport_title": "NBA", "commence_time": iso(commence), "home_team": home, "away_team": away}


def make_odds(event: dict, books: dict) -> dict:
    """books = {book_key: {market_key: [(player, side, point, price), ...]}}"""
    return {**event, "bookmakers": [
        {"key": bk, "title": bk.title(), "last_update": event["commence_time"], "markets": [
            {"key": mk, "last_update": event["commence_time"], "outcomes": [
                {"name": side, "description": player, "price": price, **({"point": point} if point is not None else {})}
                for (player, side, point, price) in outcomes]}
            for mk, outcomes in markets.items()]}
        for bk, markets in books.items()]}


class FakeOddsProvider:
    def __init__(self, credits: int = 20_000):
        self.events: dict[str, dict] = {}
        self.odds: dict[str, dict] = {}
        self.fail_next: dict[str, list[int]] = {}   # path -> statuses to return before succeeding
        self.requests: list[dict] = []
        self.remaining = credits
        self.used = 0

    def add(self, event: dict, books: dict) -> None:
        self.events[event["id"]] = event
        self.odds[event["id"]] = make_odds(event, books)

    def set_price(self, event_id: str, book: str, market: str, player: str, side: str, point, price) -> None:
        for b in self.odds[event_id]["bookmakers"]:
            if b["key"] != book:
                continue
            for m in b["markets"]:
                if m["key"] != market:
                    continue
                for o in m["outcomes"]:
                    if o["description"] == player and o["name"] == side and o.get("point") == point:
                        o["price"] = price

    def remove_outcome(self, event_id: str, book: str, market: str, player: str, side: str, point) -> dict:
        for b in self.odds[event_id]["bookmakers"]:
            for m in b["markets"]:
                if b["key"] == book and m["key"] == market:
                    for o in list(m["outcomes"]):
                        if o["description"] == player and o["name"] == side and o.get("point") == point:
                            m["outcomes"].remove(o)
                            return o
        raise KeyError("no such outcome")

    def handle(self, path: str, params: dict) -> tuple[int, object, dict]:
        self.requests.append({"path": path, "params": dict(params)})
        if not params.get("apiKey"):
            return 401, {"message": "API key is missing", "error_code": "MISSING_KEY"}, {}
        fails = self.fail_next.get(path)
        if fails:
            return fails.pop(0), {"message": "synthetic failure"}, {}
        if path.endswith("/events"):
            cost = 0
            body: object = list(self.events.values())
        elif "/events/" in path and path.endswith("/odds"):
            ev_id = path.split("/events/")[1].split("/")[0]
            if ev_id not in self.odds:
                return 404, {"message": "Event not found", "error_code": "EVENT_NOT_FOUND"}, {}
            wanted = set((params.get("markets") or "").split(","))
            books = set((params.get("bookmakers") or "").split(",")) - {""}
            body = copy.deepcopy(self.odds[ev_id])
            body["bookmakers"] = [b for b in body["bookmakers"] if (not books or b["key"] in books)]
            for b in body["bookmakers"]:
                b["markets"] = [m for m in b["markets"] if m["key"] in wanted]
            cost = len({m["key"] for b in body["bookmakers"] for m in b["markets"]})
        else:
            return 404, {"message": "unknown path"}, {}
        self.used += cost
        self.remaining -= cost
        return 200, body, {"x-requests-last": str(cost), "x-requests-used": str(self.used), "x-requests-remaining": str(self.remaining)}

    # In-process transport for httpx
    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            params = {k: v for k, v in request.url.params.items()}
            status, body, headers = self.handle(request.url.path, params)
            return httpx.Response(status, json=body, headers=headers)
        return httpx.MockTransport(handler)

    # Real local HTTP server (for CLI/subprocess tests)
    def serve(self) -> tuple[ThreadingHTTPServer, str]:
        provider = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(u.query).items()}
                status, body, headers = provider.handle(u.path, params)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):  # silence
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, f"http://127.0.0.1:{server.server_address[1]}"
