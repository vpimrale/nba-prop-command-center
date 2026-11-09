# Operations: free hosting, automation and the paper-trading record

_Last updated 2026-11-09. Nothing described here has been deployed. No hosting, subscription or
credits have been purchased. Every template in `backend/deploy/` is inert until you install it._

## 1. What has to run, and how often

One long-running process, `nba-edge run-scheduler`, ticks every 5 minutes. On each tick it runs
whatever is due. "Due" is computed from the database (`job_runs`, `slot_completions`), not
from memory.

| Job | Cadence (default) | Source | Cost |
|---|---|---|---|
| Odds capture: fixed slots | 10:00 and 15:00 ET, whole slate | PropLine free tier | ~1 request per slate |
| Odds capture: pre-tip and closing | T−60 and T−10 min per game; games sharing a tip time share one run | PropLine | ~1 request each |
| Events refresh | hourly | PropLine | 1 request |
| ESPN results and box scores | hourly; reaches back up to 7 days after downtime | ESPN public feed | free |
| Injuries | every 30 min | ESPN + PropLine's free endpoint | free / 1 request |
| Slate model (10k sims per player) | hourly, and after each capture (≥20 min apart) | local CPU, ~35 s for a 7-game slate | — |
| Grading (paper ledger + all-quotes track) | right after new finals arrive, and hourly | local | — |
| Maintenance | once per ET day after 05:00: retention, ledger hash check, SQLite backup | local | — |

PropLine use is about 30–40 requests per game day. The free limit is 1,000 per day, and the
local guard is set at 800.

## 2. Restart safety and visibility

- **Single instance.** A heartbeat lock row in the database (`scheduler_lock`) stops a second
  scheduler from running on the same database, which would double-spend requests.
  - **Crash, `kill -9` or power cut on the same machine:** the restarted process sees that the
    old holder's process no longer exists and takes the lock at once. The takeover is logged.
  - **Holder on another machine:** its lock is taken over once its heartbeat is 15 minutes old.
  - **A second `run-scheduler` loop** waits as a standby, logging that it is waiting, instead
    of exiting. Exiting would make systemd, launchd or Task Scheduler restart it in a loop
    until they give up.
  - **`run-due` (cron mode) and `run-scheduler --once`** refuse with exit 3 while a loop
    holds the lock.
- **SQLite concurrency.** WAL mode with a 30-second busy timeout, so a manual command or the
  backup running during a scheduler write waits instead of failing with "database is locked".
- **Catch-up after downtime.** On the first tick after a restart, every job whose last success
  is too old runs. ESPN ingestion reaches back to the day before its last good run, so finals
  missed while the process was down are ingested and graded.
- **Nothing is hidden.**
  - An odds slot whose window passed unserved is recorded as `missed`.
  - A gap of more than 3× the tick interval is recorded as `scheduler_gap`, with its start and
    end times.
  - Every job failure is a `job_runs` row with the error.
  - If odds capture fails, results, injuries and grading still run.
- **Clean shutdown.** On SIGTERM or SIGINT the loop finishes the current tick and releases the
  lock.
- **`nba-edge health`** prints every check and exits with 0 for ok, 1 for warn, or 2 for
  critical.
  - **Checks:** scheduler liveness, lock heartbeat, odds freshness, blocked or quota states,
    each job against its cadence, failures, gaps and missed slots in the last 24 h, grading
    backlog, daily backup age, and disk space.
  - **Where it appears:** the dashboard's *Data freshness* tab.
- **Optional dead-man's switch.** Set `HEALTHCHECK_PING_URL` to a free check URL, e.g.
  healthchecks.io. The process sends a bare GET after every tick, or `…/fail` after a failed
  tick, and the service alerts you when pings stop.
  - The URL is treated as a secret.
  - The free plan's limits could not be checked from here because the site is outside this
    environment's network allowlist. Confirm them on the site before relying on it.
  - It is entirely optional.

**Verified in this environment (2026-11-08):**
- The v3→v4 migration ran on the real data store.
- The first live tick recorded the 24-hour gap since the previous day's session and the
  missed 10:00 ET slot.
- That tick caught up the 7 finals and graded them.
- The model ran on today's slate: 215 players, 10,098 quotes, 0 candidates, so NO BET.
- Health reported WARN for exactly the gap and the missed slots.

## 3. Storage, measured on real data

| Item | Measured | Per game day (FanDuel + Fanatics) | Per season (~165 game days) |
|---|---|---|---|
| Odds snapshots (change-only, append-only) | ~0.7 KB/row incl. indexes. A first daily capture is ~10k rows for FD + Fanatics; recaptures add ~3k changed rows (mostly alternate-ladder repricing) | ~25–30 MB | **~4–5 GB** (×1.6 if DraftKings/Pinnacle reference books are added) |
| Model evaluations | ~0.9 KB/row, ~10k rows per run | ~100 MB transient | Pruned after 3 days. Graded rows are kept: **~1.5 GB** |
| Box scores, injuries, projections, ledger | small | <5 MB | <0.5 GB |
| **Total** | | | **~6–7 GB** (~10 GB with reference books) |

Gzip backups are kept 4 deep by default; late in the season each one is about 1–2 GB.

**Consequence:** free managed Postgres tiers (0.5 GB) fill within a few weeks. They cannot
be the primary store for this dataset.

## 4. Free hosting options (researched 2026-11-07, not purchased, not deployed)

| Option | Free allowance (as published) | Fits this workload? | Main limitations |
|---|---|---|---|
| **Your own always-on computer** (desktop, old laptop, Raspberry Pi 4/5, NAS) | Hardware you already own; electricity only | **Yes.** Data stays local, which matches PropLine's no-redistribution terms. Templates: systemd, Windows Task Scheduler, macOS launchd, Docker | Must not sleep. Home outages create gaps, which are recorded. Back up off the machine yourself. |
| **Oracle Cloud Always Free VM** | Per InfoQ (July 2026), the Ampere A1 allowance was cut to 2 OCPU / 12 GB RAM; ~200 GB block storage; AMD micro VMs | **Yes, technically**: plenty of disk for SQLite | Credit card required at sign-up. Idle Always Free instances can be reclaimed. A1 capacity is often unavailable. The 2026 cut could not be checked against Oracle's own page, because docs.oracle.com is blocked from this environment. A PropLine key would sit on a third-party host. |
| **Google Cloud e2-micro (Always Free)** | 1 shared vCPU, 1 GB RAM, 30 GB standard disk, US regions only | **Marginal.** A 10k-sim slate run on 1 shared vCPU and 1 GB RAM would be slow and could exhaust memory. Fewer sims would be needed. | Billing account required. 30 GB is ~4 seasons. Egress limits. |
| **GitHub Actions (scheduled workflows)** | Free minutes for public repos; private repos have a monthly quota | **No.** | Cron runs are delayed or dropped under load, so closing slots would be missed. No persistent disk. Schedules are disabled after 60 days without repo activity. Secrets and data would have to go through GitHub. |
| **Render / Railway / Fly.io / Koyeb free tiers** | Web services that sleep; no free always-on background worker (2026) | **No** | A sleeping or credit-limited service misses slots. Paid workers are excluded by the no-purchase rule. |
| **Neon / Supabase free Postgres** | ~0.5 GB. Supabase pauses projects after 1 week of inactivity. | **No as primary store**; possible as an off-site copy of the small tables | Fills within weeks (see §3). Pausing would break the scheduler. |

Sources: Oracle docs (docs.oracle.com/iaas/Content/FreeTier/freetier.htm, not reachable from
here); InfoQ, "Oracle Cloud free tier limits" (2026-07); GCP free tier page and agentdeals.dev
summary; GitHub community discussions #210625 and #211839 on delayed and dropped schedules;
flaviocopes.com and saaspricepulse.com free-tier surveys; dev.to and automationatlas.io
Neon/Supabase comparisons. Free tiers change often; re-check before choosing.

## 5. Recommended setup: run it on your own computer

Use a computer you already own that can stay on, with SQLite on its local disk.
- It is free and has no reclaim or sleep policies.
- It keeps PropLine data private, as the terms require.
- It has enough disk for several seasons (§3).

**Fallback if no machine can stay on:** an Oracle Always Free VM with the Linux steps below
and SQLite on its block volume. It requires a card at sign-up and has reclaim risk, so it needs
your explicit decision. No account or VM has been created.

### 5.1 One-time install (all systems)

Requires Python 3.11 or newer (tested on 3.13) and git.

```bash
git clone -b claude/festive-turing-darhx2 https://github.com/vpimrale/nba-prop-command-center.git   # drop -b once merged
cd nba-prop-command-center/backend
python3 -m venv .venv                       # Windows: py -3 -m venv .venv
.venv/bin/pip install -r requirements.lock  # Windows: .venv\Scripts\pip install -r requirements.lock
cp .env.example .env                        # Windows: copy .env.example .env
```

Edit `backend/.env`:
- Set `PROPLINE_API_KEY=` to your free key from prop-line.com.
- Optionally set `HEALTHCHECK_PING_URL=`.
- Leave `NBA_EDGE_DATABASE_URL` empty to use `backend/data/nba_edge.sqlite3`.

On Linux or macOS, also run `chmod 600 .env`. The file is git-ignored; never commit it.

Then create the database and check one tick by hand:

```bash
.venv/bin/python -m nba_edge init-db
set -a; . ./.env; set +a                    # Windows: the wrapper script does this for the service
.venv/bin/python -m nba_edge run-scheduler --once
.venv/bin/python -m nba_edge health         # exit 0 ok / 1 warn / 2 critical
```

On the very first tick, health shows `critical` or `warn` until each job has run once. That
is expected.

### 5.2 Keep it running

Pick the section for your system. Each template restarts the scheduler after a crash and starts
it again after a reboot. A restart after a crash is never refused by the lock (§2).

**Linux** (desktop, laptop, Raspberry Pi, or the Oracle VM):
1. Copy the service file:
   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/systemd/nba-edge-scheduler.service ~/.config/systemd/user/
   ```
2. If the repo is not at `~/nba-prop-command-center`, edit the paths in that file.
3. Enable and start it, and keep it running when you are logged out:
   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now nba-edge-scheduler
   loginctl enable-linger "$USER"
   ```
4. Watch the logs with `journalctl --user -u nba-edge-scheduler -f`.

**Windows 10/11:**
1. Run once in PowerShell:
   ```powershell
   powershell -ExecutionPolicy Bypass -File backend\deploy\windows\register-task.ps1
   ```
   This registers a per-user task that starts at logon and restarts on failure. It runs
   only while you are signed in, so after a reboot it starts when you sign in.
2. Set Settings › System › Power › Sleep to **Never** while plugged in.
3. Logs are in `backend\data\logs\scheduler.log` (rotated at 10 MB).
4. To remove the task: `Unregister-ScheduledTask -TaskName "NBA Edge Scheduler"`.

**macOS:**
1. Create the log folder: `mkdir -p backend/data/logs`.
2. In `deploy/macos/com.nbaedge.scheduler.plist`, replace `/Users/YOU` with your home folder.
3. Install and load it:
   ```bash
   cp deploy/macos/com.nbaedge.scheduler.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.nbaedge.scheduler.plist
   ```
4. Turn on System Settings › Battery › Options › *Prevent automatic sleeping when the display
   is off* (while on power).

**Docker** (any OS with Docker):
- Start it with `docker compose -f deploy/docker-compose.yml up -d --build`.
- The Docker files have not been built here, because Docker is not available in this
  environment.

### 5.3 Daily routine (first week), then weekly

- `nba-edge health`, or `nba-edge serve` and open http://127.0.0.1:8765/ (Data freshness tab).
  The dashboard binds to localhost only; do not expose it, since PropLine data may not be
  redistributed.
- `nba-edge paper` shows the REAL paper-trading report and ledger integrity.
- Copy `backend/data/backups/` off the machine now and then, e.g. to a USB drive or a private
  cloud folder. Backups contain PropLine data, so keep them private.

## 6. The paper-trading record (what "evidence" means here)

There are three tracks. They are never combined: separate tables, separate API endpoints
(`/api/paper` vs `/api/simulated`), separate dashboard tabs, and the `nba-edge paper` vs
`nba-edge backtest` commands.

| Track | What it is | Tables | Metrics |
|---|---|---|---|
| **Paper ledger** (REAL) | Every quote the model flagged PAPER_CANDIDATE, frozen the first time it qualified | `paper_recommendations`, `paper_outcomes` (insert-only; DB triggers reject UPDATE/DELETE; SHA-256 per row, rechecked daily) | Record, ROI with 95% CI at the recommendation price, Brier vs the market no-vig price, calibration, CLV, by market, book, probability bucket, side, main/alt and model version |
| **All-quotes calibration** (REAL) | Each quote's last pre-tip evaluation, graded | `prop_evaluations` + `prop_grades` (grades insert-only) | Calibration and CLV only, no ROI: it is not a strategy |
| **Simulated validation** | Walk-forward backtest on historical box scores with SIMULATED lines | `backtest_runs`, `backtest_rows` | Projection calibration only |

**What each recommendation stores:**
- The exact line and price, and the `odds_snapshots` row holding that price.
- Projection mean, median and SD; minutes; `p_play`.
- Model probability, its uncertainty and conservative bound; no-vig probability; fair odds.
- EV and conservative EV; confidence; every gate result.
- Model version, model run ID and timestamp.

**How grading works:**
- Each recommendation is graded once against ESPN's final box score at the recommendation
  price, with a notional 1-unit stake.
- A player who doesn't play is void (refunded).
- The closing price is the quote's last stored state at or before tip. It is flagged if the
  book pulled the quote before tip.
- CLV is recorded only when an odds capture ran after the recommendation and before tip.
  Otherwise it is "unavailable", never a misleading 0.

**Guardrails:**
- NO BET remains the default; most slates produce no candidates.
- `real_money_betting: disabled` is a label: no code anywhere places bets.
- Reports state `profitability_claim: none`.
- Reaching 300 decided paper results only permits a *review* of the evidence. It establishes
  nothing by itself.

## 7. Commands

```bash
nba-edge run-scheduler            # the loop (what the service templates run)
nba-edge run-due                  # one tick, for cron/timers (refuses if a loop holds the lock)
nba-edge health                   # exit 0 ok / 1 warn / 2 critical
nba-edge paper [--all-quotes] [--full]   # REAL paper-trading report + ledger integrity
nba-edge grade                    # grade now (the scheduler does this automatically)
nba-edge maintenance              # prune + hash check + backup now
nba-edge serve                    # local dashboard, 127.0.0.1 only
```
