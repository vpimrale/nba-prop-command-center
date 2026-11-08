# Operations: free hosting, automation and the paper-trading record

_Last updated 2026-11-08. Nothing described here has been deployed. No hosting, subscription or
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
  scheduler from running on the same database, which would double-spend requests. A crashed
  holder's lock is taken over after 15 minutes, and the takeover is logged. `run-due` (cron
  mode) refuses with exit 3 while a loop holds the lock.
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

## 5. Recommended setup

1. **Run it on a computer you already own that stays on**, using SQLite on its local disk.
   - It is free.
   - It keeps PropLine data private, as the terms require.
   - It has no reclaim or sleep policies.
   - It has enough disk for several seasons.
2. **Install the matching template**:
   - Linux: `backend/deploy/systemd/nba-edge-scheduler.service` (user service, `Restart=always`, plus `loginctl enable-linger`).
   - Windows: `backend/deploy/windows/register-task.ps1` (starts at logon, restarts on failure).
   - macOS: `backend/deploy/macos/com.nbaedge.scheduler.plist`.
   - Docker: `backend/deploy/docker-compose.yml`. The Docker files have not been built here
     (no Docker in this environment).
3. **Put secrets in `backend/.env`** (git-ignored, `chmod 600`): `PROPLINE_API_KEY` and,
   optionally, `HEALTHCHECK_PING_URL`.
4. **Disable sleep while plugged in.**
5. **Copy `backend/data/backups/` off the machine** now and then, e.g. to a USB drive or a
   private cloud folder. Backups contain PropLine data, so keep them private.
6. **Check `nba-edge health`** or the dashboard's Data freshness tab daily for the first week.

**Fallback if no machine can stay on:** an Oracle Always Free VM with the systemd template
and SQLite on its block volume. It requires a card at sign-up and has reclaim risk, so it
needs your explicit decision. I have not created an account or VM.

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
