"""Turn a provider event-odds response into flat, validated quote records.

One quote = one priced outcome: (event, bookmaker, market, player, side, line) at a
price. Nothing is invented: an outcome missing a usable price is skipped and counted,
never filled in. Player team/opponent are NOT provided by the odds feed (it only names
the player); they are left empty here and resolved against rosters in Stage 2.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .markets import market_info
from .math import american_to_decimal

ET = ZoneInfo("America/New_York")
PROVIDER = "the-odds-api"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def game_date_et(commence: datetime | None) -> str | None:
    return commence.astimezone(ET).date().isoformat() if commence else None


def line_key(line: float | None) -> str:
    return "" if line is None else f"{float(line):g}"


def quote_key(provider: str, event_id: str, bookmaker: str, market_key: str, player: str, side: str, line: float | None) -> str:
    return "|".join([provider, event_id, bookmaker, market_key, player, side, line_key(line)])


@dataclass(frozen=True)
class Quote:
    provider: str
    sport_key: str
    event_id: str
    commence_time: datetime | None
    game_date_et: str | None
    home_team: str
    away_team: str
    bookmaker: str
    market_key: str
    market_code: str
    is_alternate: bool
    player_name: str
    side: str            # 'over' | 'under' | 'yes' | 'no' | other provider label, lowercased
    line: float | None
    american_odds: int
    decimal_odds: float
    book_last_update: datetime | None
    market_last_update: datetime | None

    @property
    def key(self) -> str:
        return quote_key(self.provider, self.event_id, self.bookmaker, self.market_key, self.player_name, self.side, self.line)


@dataclass
class NormalizeResult:
    quotes: list[Quote]
    skipped: int
    bookmakers_seen: set[str]


def normalize_event_odds(payload: dict, *, provider: str = PROVIDER) -> NormalizeResult:
    event_id = str(payload["id"])
    commence = parse_ts(payload.get("commence_time"))
    base = dict(provider=provider, sport_key=payload.get("sport_key", ""), event_id=event_id, commence_time=commence,
                game_date_et=game_date_et(commence), home_team=payload.get("home_team", ""), away_team=payload.get("away_team", ""))
    quotes: list[Quote] = []
    skipped = 0
    seen_books: set[str] = set()
    seen_keys: set[str] = set()
    for book in payload.get("bookmakers") or []:
        bkey = book.get("key")
        if not bkey:
            continue
        seen_books.add(bkey)
        book_ts = parse_ts(book.get("last_update"))
        for market in book.get("markets") or []:
            mkey = market.get("key")
            if not mkey:
                continue
            info = market_info(mkey)
            m_ts = parse_ts(market.get("last_update"))
            for o in market.get("outcomes") or []:
                player = (o.get("description") or "").strip()
                side = (o.get("name") or "").strip().lower()
                price = o.get("price")
                point = o.get("point")
                if not player or not side or price is None:
                    skipped += 1
                    continue
                try:
                    american = int(round(float(price)))
                    decimal = round(american_to_decimal(american), 6)
                    line = float(point) if point is not None else None
                except (TypeError, ValueError):
                    skipped += 1
                    continue
                q = Quote(**base, bookmaker=bkey, market_key=mkey, market_code=info.code, is_alternate=info.alternate,
                          player_name=player, side=side, line=line, american_odds=american, decimal_odds=decimal,
                          book_last_update=book_ts, market_last_update=m_ts)
                if q.key in seen_keys:  # provider duplicate within one response: keep first
                    skipped += 1
                    continue
                seen_keys.add(q.key)
                quotes.append(q)
    return NormalizeResult(quotes, skipped, seen_books)
