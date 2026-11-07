"""Player-name normalization for joining odds-feed players to ESPN athletes."""
from __future__ import annotations

import re
import unicodedata

_SUFFIX = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.I)


def norm_name(name: str | None) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = s.replace(".", " ").replace("'", "").replace("’", "")
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def match_player(name: str, candidates: dict[str, str]) -> tuple[str | None, str]:
    """candidates: athlete_id -> display name (players of the two teams in the game).
    Exact normalized match only, then a unique last-name + first-initial match. Never fuzzy
    beyond that — an unmatched player is reported, not guessed."""
    n = norm_name(name)
    exact = [aid for aid, nm in candidates.items() if norm_name(nm) == n]
    if len(exact) == 1:
        return exact[0], "exact"
    if len(exact) > 1:
        return None, "ambiguous"
    parts = (name or "").replace(".", " ").split()
    if len(parts) >= 2:
        first, last = norm_name(parts[0])[:1], norm_name(" ".join(parts[1:]))
        loose = [aid for aid, nm in candidates.items()
                 if (p := nm.replace(".", " ").split()) and len(p) >= 2 and norm_name(" ".join(p[1:])) == last and norm_name(p[0])[:1] == first]
        if len(loose) == 1:
            return loose[0], "initial_last"
    return None, "unmatched"
