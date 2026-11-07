# nba-edge backend (Stage 1: odds snapshot pipeline)

Captures timestamped NBA player-prop odds snapshots (FanDuel + Fanatics first) into an
append-only database, so a real historical odds dataset accumulates from day one.
Architecture and rationale are in [`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

**Status (2026-11-07): built and tested against a synthetic provider only.** No odds
API key is configured and no plan has been purchased, so the pipeline has never talked
to the real provider. Everything below that says "verified" means verified by the test
suite with synthetic data. Nothing is deployed.

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

| Command | Calls the paid provider? | What it does |
|---|---|---|
| `config` | no | effective configuration (no secrets) |
| `init-db` | no | create/upgrade schema |
| `budget [--games-per-day N --snapshots-per-game M]` | no | upper-bound monthly credit estimate for the current market/book config |
| `refresh-events` | yes (events list; documented as free) | upcoming NBA events (next 36h) |
| `snapshot [--event ID] [--date YYYY-MM-DD]` | **yes** | capture now, for today's (ET) not-yet-started games |
| `run-due` | **yes** | one scheduler tick: serve due slots (run every ~5 min from cron/systemd) |
| `run-scheduler [--interval 300]` | **yes** | the same, as a long-running loop |
| `status` | no | data freshness, job health, missed slots, credits (provider-reported) |
| `coverage [--date]` | no | observed bookmaker × market coverage for a date |
| `closing --event ID` | no | last pre-tip observation of every quote (closing-line reference) |

Without `ODDS_API_KEY`, provider-calling commands make **no** request, record a
`blocked_no_key` run, print `BLOCKED: ...` and exit 2.

### Spend guards (local, purchase nothing)
* `ODDS_MONTHLY_CREDIT_BUDGET` (default 500 = free tier) — measured against credits the
  provider *reported* in `x-requests-last` this calendar month.
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

Last run (2026-11-07): 26 passed on SQLite; 46 passed with Postgres 16.15 enabled
(20 database-backed tests run on both). All provider data in tests is synthetic
(`tests/fake_provider.py`); one test drives the real CLI as a subprocess over real HTTP
against a local fake server.

## Not yet verified (needs a key + network access to api.the-odds-api.com)
* That the market keys in `nba_edge/odds/markets.py` are all accepted for NBA.
* Which of those markets FanDuel and Fanatics actually return, and how many alternate
  lines; whether Fanatics is returned at all on a given plan.
* The real per-call credit cost (the pipeline measures it; the estimate is an upper bound).
* Rate-limit behavior under real load.

Run `nba-edge snapshot` once with a key, then `nba-edge coverage` and `nba-edge status`
to measure all four.
