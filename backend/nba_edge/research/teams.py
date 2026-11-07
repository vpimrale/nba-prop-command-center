"""NBA team identities: ESPN abbreviation <-> full name (as PropLine/odds feeds name teams)."""
from __future__ import annotations

import re
import unicodedata

# (ESPN abbreviation, full name, extra aliases)
TEAMS = [
    ("ATL", "Atlanta Hawks", ()), ("BOS", "Boston Celtics", ()), ("BKN", "Brooklyn Nets", ("BRK", "NJ")),
    ("CHA", "Charlotte Hornets", ("CHO",)), ("CHI", "Chicago Bulls", ()), ("CLE", "Cleveland Cavaliers", ()),
    ("DAL", "Dallas Mavericks", ()), ("DEN", "Denver Nuggets", ()), ("DET", "Detroit Pistons", ()),
    ("GS", "Golden State Warriors", ("GSW",)), ("HOU", "Houston Rockets", ()), ("IND", "Indiana Pacers", ()),
    ("LAC", "LA Clippers", ("Los Angeles Clippers",)), ("LAL", "Los Angeles Lakers", ()), ("MEM", "Memphis Grizzlies", ()),
    ("MIA", "Miami Heat", ()), ("MIL", "Milwaukee Bucks", ()), ("MIN", "Minnesota Timberwolves", ()),
    ("NO", "New Orleans Pelicans", ("NOP",)), ("NY", "New York Knicks", ("NYK",)), ("OKC", "Oklahoma City Thunder", ()),
    ("ORL", "Orlando Magic", ()), ("PHI", "Philadelphia 76ers", ()), ("PHX", "Phoenix Suns", ("PHO",)),
    ("POR", "Portland Trail Blazers", ()), ("SAC", "Sacramento Kings", ()), ("SA", "San Antonio Spurs", ("SAS",)),
    ("TOR", "Toronto Raptors", ()), ("UTAH", "Utah Jazz", ("UTA",)), ("WSH", "Washington Wizards", ("WAS",)),
]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower())


_LOOKUP: dict[str, str] = {}
for abbr, name, aliases in TEAMS:
    for k in (abbr, name, *aliases, name.split()[-1]):
        _LOOKUP[_norm(k)] = abbr
_LOOKUP[_norm("Trail Blazers")] = "POR"
_LOOKUP[_norm("76ers")] = "PHI"
_LOOKUP[_norm("Sixers")] = "PHI"


def espn_abbr(value: str | None) -> str | None:
    """Any team spelling (ESPN/app abbreviation, full name, nickname) -> ESPN abbreviation."""
    if not value:
        return None
    return _LOOKUP.get(_norm(value))


def full_name(abbr: str) -> str | None:
    return next((n for a, n, _ in TEAMS if a == abbr), None)
