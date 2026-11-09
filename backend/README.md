# nba-edge backend (odds snapshots + research/projection engine)

Captures timestamped NBA player-prop odds snapshots (FanDuel + Fanatics first) into an
append-only database, so a real historical odds dataset accumulates from day one.
Architecture: [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md). Source audit:
[`../docs/ODDS_SOURCES.md`](../docs/ODDS_SOURCES.md). **Projection model, validation results
and known weaknesses: [`../docs/MODEL.md`](../docs/MODEL.md).**

The research engine ingests free ESPN box scores and injuries, projects every rostered
player on the slate (bench included), runs 10,000 Monte Carlo simulations per player,
prices every live FanDuel/Fanatics quote, and defaults to **NO BET**. Its outputs are paper
only until the model has been validated on graded live props.

**Status (2026-11-07):** the default source is **PropLine's free tier** (1,000
requests/day, $0), **verified live** — real FanDuel and Fanatics NBA props for a full
7-game slate were fetched, stored and sanity-checked (see the source audit). The Odds
API remains available as a paid adapter (`ODDS_PROVIDER=the-odds-api`). Nothing is
deployed; snapshots only accumulate while something runs the scheduler.

**Data handling:** PropLine's terms prohibit bulk redistribution. The database
(`backend/data/`, git-ignored) must not be committed or published; test fixtures are
synthetic.

## Getting a free PropLine key
Enter an email at https://prop-line.com (no card). Provide it as `PROPLINE_API_KEY`.
In this project's Claude cloud environment the key is stored as a network secret and
injected by the proxy, so set `PROPLINE_API_KEY=proxy-injected` there (any placeholder;
the proxy overwrites the header).

## Setup

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock      # exact versions used in testing
.venv/bin/python -m nba_edge init-db            # creates backend/data/nba_edge.sqlite3 by default
.venv/bin/python -m nba_edge config             # effective config, secrets masked
```

Configuration is environment variables only; see [`.env.example`](.env.example).
The API key is read from `ODDS_API_KEY`, is never logged (httpx request logging is
suppressed and every log record is scrubbed of `apiKey=` and the key value), never
stored in the database (requests are recorded by path only), and never printed.

## Commands

| Command | Calls the odds provider? | What it does |
|---|---|---|
| `config` | no | effective configuration (no secrets) |
| `init-db` | no | create/upgrade schema |
| `budget [--games-per-day N --snapshots-per-game M]` | no | PropLine: upper-bound requests/day for the schedule; The Odds API: monthly credits |
| `refresh-events` | yes (1 request on PropLine) | upcoming NBA events (next 36h) |
| `snapshot [--event ID] [--date YYYY-MM-DD]` | **yes** | capture now, for today's (ET) not-yet-started games |
| `run-due` | **yes** | one scheduler tick (cron/timer mode); exits 3 if a scheduler loop holds the DB lock |
| `run-scheduler [--interval S] [--once] [--force]` | **yes** | the long-running loop the service templates run (single instance per DB; a second loop waits as standby; SIGTERM-safe). Global `--log-file PATH` appends rotated logs |
| `health` | no | freshness/failure/gap/missed-slot/backlog/backup checks; exit 0 ok, 1 warn, 2 critical |
| `paper [--all-quotes] [--full]` | no | REAL paper-trading report: record, ROI (95% CI), Brier vs no-vig, calibration, CLV, by market/book/probability bucket/side/main-alt; ledger hash check |
| `maintenance` | no | the daily job now: prune, ledger hash check, SQLite backup (gzip, rotated) |
| `status` | no | data freshness, job health, missed slots, credits (provider-reported) |
| `coverage [--date]` | no | observed provider × bookmaker × market coverage for a date (incl. stale counts) |
| `verify [--date] [--book B]` | no | sanity checks on stored quotes: two-sided pairs, invalid prices, overround, cross-book line agreement, sample rows |
| `closing --event ID` | no | last pre-tip observation of every quote (closing-line reference) |
| `ingest-espn [--start --end \| --days N]` | ESPN (free) | schedules + final box scores |
| `ingest-injuries` | ESPN + 1 PropLine request | injury reports (two sources, most recent wins) |
| `project [--sims 10000]` | no | slate projections + evaluation of every live FanDuel/Fanatics quote |
| `grade` | no | grade the paper ledger (at the recommendation price) and each quote's last pre-tip evaluation against final box scores |
| `backtest --start --end [--every N]` | no | walk-forward projection backtest (SIMULATED lines) |
| `prune [--keep-days 3]` | no | drop old intermediate model evaluations (keeps each game's last pre-tip run) |
| `serve [--port 8765]` | no | local read-only dashboard: top props, projections, player research, model performance, data freshness |

`run-due` / `run-scheduler` also run the research jobs on their own cadence (ESPN results
hourly, injuries every 30 min, the model after snapshots or at least hourly, grading when
new finals arrive and hourly, daily maintenance after 05:00 ET), each isolated so a failure
never stops odds capture (and an odds failure never stops grading). Disable with
`RESEARCH_JOBS=0`. Hosting options, restart behaviour, storage estimates and the
paper-trading record are documented in `docs/OPERATIONS.md`; service templates are in
`deploy/` (not installed anywhere).

Without the active provider's key (`PROPLINE_API_KEY` or `ODDS_API_KEY`), provider-calling
commands make **no** request, record a `blocked_no_key` run, print `BLOCKED: ...` and exit 2.

### PropLine specifics
* One **bulk** request (`/sports/basketball_nba/odds`) returns props for the whole
  near-term slate; events not in it are fetched per event (1 request each).
* Main lines (Over/Under) are stored as `player_points` etc.; milestone ladders
  ("25+ Points") as `player_points_milestone`, side `over`, line 24.5, with the original
  label in `outcome_label`; other line types as `<market>_alternate`.
* Not stored: suspended markets, outcomes the book is withdrawing, non-full-game
  periods, unrecognized outcome shapes, and DFS pick'em boards (PrizePicks, Underdog,
  Pick6, …: synthetic/payout-scaled prices). Counts are written to the run's `detail`.
* Quotes whose bookmaker feed is older than `ODDS_STALE_AFTER_MIN` (default 20) are
  stored with `stale=true` and counted; unknown freshness counts as stale.
* 429 `burst_limit_exceeded` is retried after the provider's `retry_after_seconds`;
  429 `daily_limit_exceeded` stops the run (`quota_exhausted`) — no retry until UTC reset;
  403 `upgrade_required` (paid feature) is never retried.
* Each row keeps PropLine's NBA player id (`provider_player_id`, `nba:<personId>`) and
  each game its ESPN event id (`games.espn_event_id`) for joining to the tracker/stats.

### Spend guards (local, purchase nothing)
* PropLine: `ODDS_DAILY_REQUEST_BUDGET` (default 800 of the free 1,000/UTC day), measured
  from recorded requests; plus the provider's `X-Daily-Remaining` for today.
* The Odds API: `ODDS_MONTHLY_CREDIT_BUDGET` (default 500 = free tier) — measured against
  credits the provider *reported* in `x-requests-last` this calendar month.
* `ODDS_MAX_CREDITS_PER_RUN` (default 300).
* The provider's own `x-requests-remaining`, once seen.
A run that would exceed the tightest limit fetches only the events that fit (closing
slots run first) or is recorded as `skipped_budget`.

## Tests

```bash
.venv/bin/python -m pytest                     # SQLite
# + Postgres, against a SCRATCH server (each test creates and drops its own database):
NBA_EDGE_TEST_PG_URL=postgresql://postgres@127.0.0.1:55432/postgres .venv/bin/python -m pytest
```

Last run (2026-11-09): 78 passed on SQLite; 137 passed with Postgres 16 enabled
(database-backed tests run on both). All provider data in tests is synthetic
(`tests/fake_provider.py`, `tests/fake_propline.py`, `tests/synthetic_league.py`); two tests drive the real CLI as a
subprocess over real HTTP against local fake servers.

## The Odds API (paid adapter) — not yet verified live (needs a key + network access)
* That the market keys in `nba_edge/odds/markets.py` are all accepted for NBA.
* Which of those markets FanDuel and Fanatics actually return, and how many alternate
  lines; whether Fanatics is returned at all on a given plan.
* The real per-call credit cost (the pipeline measures it; the estimate is an upper bound).
* Rate-limit behavior under real load.

Run `nba-edge snapshot` once with a key, then `nba-edge coverage` and `nba-edge status`
to measure all four.
