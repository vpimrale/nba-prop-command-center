"""Logging that cannot leak the odds API key.

Two layers: (1) httpx/httpcore loggers are raised to WARNING because they log full
request URLs, and The Odds API takes its key as a query parameter; (2) a filter on
every handler scrubs `apiKey=...` and any registered secret value from every record.
"""
from __future__ import annotations

import logging
import re

_SECRETS: set[str] = set()
_APIKEY_RE = re.compile(r"(?i)(apiKey=)[^&\s'\"]+")


def register_secret(value: str | None) -> None:
    if value and len(value) >= 4:
        _SECRETS.add(value)


def scrub(text: str) -> str:
    text = _APIKEY_RE.sub(r"\1***", text)
    for s in _SECRETS:
        text = text.replace(s, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        record.msg = scrub(msg)
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            # Render the traceback now, scrubbed, so the formatter can't re-render it raw.
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def configure_logging(level: str = "INFO") -> None:
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    root = logging.getLogger()
    root.setLevel(level)
    if not any(getattr(h, "_nba_edge", False) for h in root.handlers):
        handler = logging.StreamHandler()
        handler._nba_edge = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
