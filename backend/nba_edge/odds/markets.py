"""NBA player-prop market catalogue for The Odds API.

Market keys below follow The Odds API's documented naming scheme
(`player_points`, `player_points_alternate`, ...). They have NOT been verified against
a live response in this environment (no API key; provider host blocked by the sandbox
network policy). An invalid key makes the provider reject the request (HTTP 422), which
the client surfaces as an explicit error — so a wrong key fails loudly, never silently.
Verify with `nba-edge coverage` once a key is configured.

`code` maps each market to the tracker app's own stat codes (prop-status.js), so
snapshots can later be joined to tracked bets and to projections.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Market:
    key: str
    code: str          # tracker stat code: PTS, REB, AST, 3PM, PRA, PR, PA, RA, STL, BLK, TO
    alternate: bool


_BASE = [
    ("player_points", "PTS"),
    ("player_rebounds", "REB"),
    ("player_assists", "AST"),
    ("player_threes", "3PM"),
    ("player_points_rebounds_assists", "PRA"),
    ("player_points_rebounds", "PR"),
    ("player_points_assists", "PA"),
    ("player_rebounds_assists", "RA"),
    ("player_steals", "STL"),
    ("player_blocks", "BLK"),
    ("player_turnovers", "TO"),
]

MARKETS: dict[str, Market] = {}
for key, code in _BASE:
    MARKETS[key] = Market(key, code, False)
    MARKETS[f"{key}_alternate"] = Market(f"{key}_alternate", code, True)

# Default: every standard + alternate O/U market above (22 markets). Each requested
# market that comes back costs credits per event call, so trimming this list is the
# main cost lever (see budget.py).
DEFAULT_MARKET_KEYS: tuple[str, ...] = tuple(MARKETS.keys())


def market_info(key: str) -> Market:
    """Known market, or a best-effort description of an unknown one (never dropped)."""
    if key in MARKETS:
        return MARKETS[key]
    return Market(key, "", key.endswith("_alternate"))
