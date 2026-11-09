"""Local research dashboard: `python -m nba_edge serve` -> http://127.0.0.1:8765/

Read-only, binds to localhost by default, no secrets, no external calls. It is a LOCAL
tool on purpose: PropLine's terms forbid redistributing its data, so this is not meant to
be published or deployed publicly.
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from sqlalchemy.engine import Engine

from . import api

log = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def make_handler(engine: Engine, settings=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nba-edge-dashboard"

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200) -> None:
            self._send(status, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path == "/api/status":
                    return self._json(api.data_status(engine))
                if u.path == "/api/slate":
                    return self._json(api.slate(engine))
                if u.path == "/api/props":
                    return self._json(api.props(engine, decision=q.get("decision") or None, book=q.get("book") or None,
                                                limit=min(1000, int(q.get("limit", 200)))))
                if u.path == "/api/projections":
                    return self._json(api.projection_table(engine))
                if u.path.startswith("/api/player/"):
                    return self._json(api.player(engine, u.path.rsplit("/", 1)[-1]))
                if u.path == "/api/paper":
                    return self._json(api.paper(engine))
                if u.path == "/api/simulated":
                    return self._json(api.simulated(engine))
                if u.path == "/api/health":
                    return self._json(api.health(engine, settings))
                if u.path == "/api/runs":
                    return self._json(api.recent_runs(engine))
                path = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
                full = os.path.normpath(os.path.join(STATIC, path))
                if not full.startswith(STATIC) or not os.path.isfile(full):
                    return self._send(404, b"not found", "text/plain")
                with open(full, "rb") as f:
                    return self._send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))
            except Exception as e:  # never leak internals beyond a message
                log.exception("dashboard error")
                return self._json({"error": type(e).__name__, "message": str(e)[:200]}, 500)

        def log_message(self, fmt, *args):
            log.debug("dashboard: " + fmt, *args)

    return Handler


def serve(engine: Engine, host: str = "127.0.0.1", port: int = 8765, settings=None) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(engine, settings))
