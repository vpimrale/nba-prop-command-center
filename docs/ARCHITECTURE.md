# NBA Prop Command Center — architecture

_Last updated 2026-11-08 (Stage 0, Stage 1, free odds source, model, paper-trading automation). Status markers: **built+tested** = exists
and covered by automated tests (synthetic data); **designed** = schema/plan only;
**not started**._

## 1. Components

```
 ┌──────────────────────────── tracker PWA (existing, static) ─────────────────────────┐
 │ index.html + app.js + pure modules (line-rules, prop-status, settlement,             │
 │ game-binding, ladder, archive, parse-slip). Data in each browser's localStorage.     │
 │ Reads ESPN's public scoreboard/summary feed for live settlement.   built+tested      │
 └──────────────────────────────────────────────────────────────────────────────────────┘
                     (Stage 5: reads a read-only JSON API — not started)
 ┌───────────────────────────── backend/ (Python 3.11+) ───────────────────────────────┐
 │ nba_edge.providers source adapters behind one interface:             built+tested    │
 │                    propline (FREE, default — verified live) ·                        │
 │                    the-odds-api (paid, optional — not live-verified)                 │
 │ nba_edge.odds      shared client errors · normalization · odds math  built+tested    │
 │ nba_edge.pipeline  snapshot capture · scheduler · credit budget      built+tested    │
 │ nba_edge.queries   status/job health · coverage · closing lines      built+tested    │
 │ nba_edge.db        SQLAlchemy Core schema (SQLite ⇄ Postgres)        built+tested    │
 │ nba_edge.research  ESPN box scores/schedules/injuries, name+team     built+tested    │
 │                    matching (PropLine injuries as 2nd source)                        │
 │ nba_edge.model     minutes/rates/context, 10k Monte Carlo, pricing,  built+tested    │
 │                    NO-BET gates, grading, backtest, retention                        │
 │ nba_edge.model.paper immutable paper ledger, grading at rec. price, built+tested    │
 │                    CLV, Brier/ROI/calibration report by segment                      │
 │ nba_edge.pipeline.ops lock/heartbeat, gap detection, health,       built+tested    │
 │                    daily prune + backup + ledger hash check                          │
 │ nba_edge.dashboard local read-only research dashboard (127.0.0.1)    built+tested    │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

The tracker does not depend on the backend; it keeps working with no backend at all.

## 2a. Odds sources (provider adapters)

The engine (snapshots, diffing, budget, scheduler, queries) only talks to
`providers.base.OddsProvider`: `list_events`, `fetch_odds(event_ids, max_units)`,
`estimate_units`, `market_in_scope`, `budget_window` (`day` or `month`). Adding a source,
free or paid, means one new adapter module plus a registry line; no engine changes. Each
row's `provider` column keeps sources separate. See `docs/ODDS_SOURCES.md` for the free
source audit. PropLine was chosen because it is the only free source verified to carry
both FanDuel and Fanatics NBA props, and one bulk request snapshots a whole slate.

## 2. Odds snapshot data model (Stage 1)

| Table | Mutability | Purpose |
|---|---|---|
| `odds_snapshots` | **append-only** | one row per quote whenever it is first seen, changes price, disappears (`removed` tombstone) or reappears |
| `odds_current` | mutable cache | latest state per quote, used only to diff; rebuildable from `odds_snapshots` |
| `games` | upsert | provider events (tip-off, teams, ET game date); `espn_event_id` joined in Stage 2 |
| `snapshot_runs` | append + finish | every attempted capture incl. `blocked_no_key`, `skipped_budget`, `failed`, `partial` |
| `api_requests` | append | every provider call: path (no query/key), status, duration, provider-reported credits |
| `slot_completions` | insert-once | scheduler slots served or `missed` — no double spend, gaps visible |
| `job_runs` | append + finish | events refreshes and scheduler ticks (job health) |

A quote is identified by `provider | event | bookmaker | market | player | side | line`.
Each snapshot row stores: capture time, ET game date, teams, bookmaker, market key, the
tracker's stat code, alternate flag, player, side, line, American and decimal odds,
market status, change type, previous price, provider `last_update` stamps, run/reason,
and empty `model_projection`/`model_version` columns filled once projections exist.
Player team/opponent are empty until Stage 2 because the odds feed only gives a name.

**Why change-only storage:** storing every price on every run would be roughly tens of
millions of rows per season at this market breadth. Storing only changes, plus explicit
tombstones, loses no information: the price at any instant T is the latest row at or
before T, and a tombstone means "not on offer at T". `queries.price_at()` and
`queries.closing_lines()` implement exactly this.

**Integrity rules (tested):** rows are never updated or deleted; a failed or partial
fetch never writes tombstones; only markets and books that were requested can be
tombstoned; games that have tipped off are never fetched or stored (pre-game only, so a
live price can't pollute a closing line); provider duplicates are dropped; outcomes
without a usable price are skipped and counted, never filled in.

## 3. Schedule (US Eastern)

* **fixed** 10:00 (initial daily capture) and 15:00 (after most teams' official
  game-day injury reports, filed 11:00–13:00 local).
* **pretip** T−60 min per game.
* **closing** T−10 min per game, the closing-line reference. With a 5-minute tick it
  lands between T−10 and T−5.

`run-due` is idempotent per slot. Simultaneous tips share one run, and an event is
fetched at most once per tick. Estimated cost with the default 22 markets is about
22 credits per game per snapshot (an upper bound), or about 4 snapshots × 7.5 games ×
22 ≈ 660 credits per slate day.

## 4. Security
* Secrets only via environment; `Secret` wrapper never prints; logging scrubs keys.
* The provider key is a URL query parameter (provider's scheme). httpx's URL logging is
  suppressed and scrubbed; `api_requests` stores the path only.
* `.env` files are git-ignored. Nothing is deployed and no host has credentials.
* The tracker PWA is public static code and must never receive the provider key. The
  Stage 5 dashboard reads a backend API, never the provider directly.

## 5. Deployment (not done — see docs/OPERATIONS.md)
One always-on process (`run-scheduler`) with SQLite on local disk. The measured data volume
is ~6–7 GB per season, which rules out the free managed Postgres tiers (0.5 GB) as the
primary store. The recommendation is a computer you already own that stays on, using the
systemd, Windows Task Scheduler, launchd or Docker templates in `backend/deploy/`. An Oracle
Always Free VM is the fallback; it needs a card at sign-up and your decision. GitHub Actions
cron and sleeping PaaS tiers are rejected because they miss closing slots. Postgres remains
supported (all tests run on both backends, and the immutability triggers exist for both).

## 5b. Paper-trading tables (schema v4)
| Table | Mutability | Purpose |
|---|---|---|
| `paper_recommendations` | **insert-only** (DB trigger) | PAPER_CANDIDATE frozen at first qualification: exact line/price, snapshot id, projection, probabilities, EV, gates, model version, timestamp, SHA-256 |
| `paper_outcomes` | **insert-only** (DB trigger) | actual stat, result, profit at the recommendation price, closing price, CLV |
| `prop_grades` | **insert-only** (DB trigger) | all-quotes calibration track (each quote's last pre-tip evaluation) |
| `scheduler_lock` | heartbeat row | single scheduler per database |

## 6. Roadmap
| Stage | Scope | State |
|---|---|---|
| 0 | settlement fix, persisted results, test suite, pinned OCR | done |
| 1 | backend, DB, odds snapshot pipeline, scheduler, job health | built + tested; **live-verified on PropLine free tier** (FanDuel + Fanatics) |
| 1b | provider abstraction; PropLine adapter (bulk + fallback, milestones, stale/suspended/withdrawn handling, burst vs daily limits) | done; live-verified |
| 2 | ESPN schedules/box scores/injuries (+ PropLine injuries); player matching | done (official NBA injury PDFs and pre-tip confirmed lineups: not yet — ESPN/PropLine injury lists used) |
| 3 | minutes + rate projections with shrinkage; 10k Monte Carlo; walk-forward validation | done — v1.2, see docs/MODEL.md |
| 4 | no-vig, fair odds, EV, uncertainty-aware gates, NO BET default | done |
| 5 | dashboard: top props, projections, player research, model performance, data freshness | done (local, read-only; separate from the tracker PWA) |
| 6 | paper trading, live calibration, CLV (30-day period) | done: immutable ledger, automatic grading, health/gap/missed-run tracking; first real slate graded 2026-11-08 (market more accurate than model); 0 paper recommendations so far; not yet deployed (needs your choice of host) |
| 7 | correlated parlays (joint simulation), fractional-Kelly sizing with caps | not started |
| 8 | historical backtest — only if a historical-odds backfill is approved | not started |
