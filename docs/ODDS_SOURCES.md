# Free NBA player-odds sources — audit (2026-11-07)

Goal: FanDuel + Fanatics NBA player props, automatically, at $0, without scraping
sportsbooks or breaking anyone's terms.

Verification levels: **LIVE** = called the real API from this project with a real free
key and inspected the responses; **DOCS** = read the provider's own current pages or
official SDK; **SEARCH** = third-party/search summaries only (not independently checked).

| Source | Free tier (as verified) | FanDuel props | Fanatics props | Verdict |
|---|---|---|---|---|
| **PropLine** (api.prop-line.com) | 1,000 requests/UTC day, no card (**LIVE**: `ratelimit-policy: 1000;w=86400`, `X-Daily-Limit: 1000`). Current odds incl. props + alt ladders on free; analytics (EV, history, trends, resolution) paid (**DOCS**) | **Yes — LIVE**: 7/7 games, 89 players, 2,758 quotes (1,230 main, 1,528 alt) | **Yes — LIVE**: 7/7 games, 92 players, 7,181 quotes (1,194 main, 5,987 alt) | **Integrated (default)** |
| SportsGameOdds | 2,500 objects/month, 10 req/min, 10-min refresh, 9 books (**DOCS**: pricing page) | listed (DOCS) | **No** — not in the free 9 (FanDuel, DraftKings, BetMGM, Caesars, ESPN BET, Bovada, Unibet, PointsBet, William Hill) | Not needed; no Fanatics |
| TheRundown | 20K data points/day, 3 books (SEARCH) | — | — | **No props on free keys** (its changelog/FAQ, SEARCH) |
| The Odds API | 500 credits/month (SEARCH) | likely | **paid-only** (its bookmaker list, SEARCH) | Already integrated as a paid adapter; free tier far too small |
| odds-api.io | 100 req/h (SEARCH) | ? | ? | **New free keys paused** (its own pricing page, SEARCH) |
| OddsPapi | 250 requests/month (SEARCH) | claimed | claimed, but its own guide says its `fanatics` mirrors PointsBet AU | Too small; Fanatics data provenance unclear |
| "API.SH" | — | — | — | No such odds provider found (SEARCH) |
| Scraping FanDuel/Fanatics directly | — | — | — | **Rejected**: FanDuel's terms bar bots/scrapers without written permission (third-party summary); anti-bot measures; not done |

## PropLine terms that shape the implementation (DOCS: prop-line.com/terms, effective 2026-04-27)
* Internal use (apps, models, tools) is allowed; surfacing individual values is fine.
* **Bulk redistribution is prohibited** (no publishing datasets/dumps). Therefore: raw
  responses and the snapshot database are never committed; test fixtures are synthetic.
* Don't circumvent rate limits; keep the key private (it is proxy-injected / env-only).
* "As is", no warranty — every quote carries provenance so problems can be traced.

## Live verification details (2026-11-07, 7-game slate, PropLine free key)
* One bulk request returned every game's props for FanDuel, Fanatics, DraftKings, Pinnacle
  → 15,854 quotes stored. All verification work that day used 8 of the 1,000 daily
  requests (3 manual probes + 5 pipeline calls), per PropLine's own `X-Daily-Used`
  counter (which lags by a request or so).
* Fields checked on stored rows: player (100% carry an NBA personId `nba:<id>`), market,
  line, side, bookmaker, American odds. 0 invalid prices; 0 one-sided main lines.
* Main-line overround (Over+Under implied): FanDuel median 1.070 (1.064–1.075);
  Fanatics median 1.069 (1.065–1.118).
* Cross-book: on 619 player-markets quoted by all four books, 74% have the identical main line.
* Milestones ("25+ Points") map to Over 24.5: Fanatics "27+ Points" −105 equals its
  main Over 26.5 −105; 10,452 adjacent ladder rungs are all monotonic (0 violations).
* A second snapshot 2 minutes later stored only 388 change rows (294 price moves, 56 new,
  38 pulled) — change-only storage confirmed on real data.

## Known limitations of the free source
* PropLine is an aggregator: data is as good as its ingestion (it documents FanDuel and
  Fanatics as push-fed, ~1–2 s p90). Freshness is checked per book (`last_update`) and
  stale quotes are flagged, not hidden.
* No free historical odds / CLV / resolution from PropLine — the project builds its own
  history from snapshots (that is what the append-only store is for).
* Coverage per game varies with how far from tip-off it is (fewer markets early).
