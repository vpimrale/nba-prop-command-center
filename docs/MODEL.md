# NBA prop research & probability engine — model v1.2

_Last updated 2026-11-08. Model id `v1.2-shrunk-nb-copula-empmin`._

**Status:** runs end to end on live data (real FanDuel/Fanatics prices via PropLine, real
ESPN box scores). It has been validated on historical outcomes with **simulated lines**.
The first real lines + real outcomes (one night, 7 games) are in §4b: on them the **market's
no-vig price was more accurate than the model**. Every output is **paper only**, **NO BET** is
the default decision, and the paper-trading ledger (docs/OPERATIONS.md §6) holds 0
recommendations so far because no quote has passed every gate.

## 1. Data (all free)
| Source | Use | Notes |
|---|---|---|
| ESPN public feed (`site.api.espn.com`) | schedules, final box scores for every player incl. bench/DNPs (minutes, FGA/FTA, OREB/DREB, TO, starter flag), injury list (All-Star/non-NBA exhibition games are excluded at ingestion) | Undocumented public feed, no published API terms; used at low volume with caching (each final box fetched once, ≥0.25 s between calls). 2025-26: 1,322 of 1,326 box scores (ESPN returned 404 for 4); 2026-27 to date: all finals. |
| PropLine free tier | live odds (FanDuel, Fanatics + DraftKings/Pinnacle as references), injury list (ESPN-sourced, with ESPN↔NBA ids), player game logs (cross-check) | 1,000 requests/day. Terms forbid redistribution, so nothing from it is committed or published. |

Historical injury reports and pre-game lineups are **not** available for free, so backtests
run without injury inputs (see §6).

## 2. Projection (per player, per game)
1. **Availability `p_play`** = injury factor × (1 − shrunk recent DNP rate) × recency.
   - Injury factor: Out 0, Doubtful 0.15, Questionable 0.5, Day-To-Day 0.65, Probable 0.9, none 0.98.
     These values are heuristic.
   - Status: ESPN status, refined by keywords in a fresh (<36 h) comment, e.g. "ruled out" or
     "questionable".
   - Recency: 0.2 if the player has been absent from the team's last 3 or more box scores.
   - Props are void if the player doesn't play, so all distributions below are *conditional
     on playing*. `p_play` enters only the EV.
2. **Minutes mean.** A weighted current-season mean, shrunk toward last season's mean
   (k = 4 games).
   - Weights: ×5 for the last 10 games, ×3 for games 11–20, ×1 for older games. This is the
     season / L20 / L10 blend.
   - Known absences of recently active rotation players hand their minutes to available
     rotation teammates, capped at +10%.
   - Back-to-back: ×0.97 for players averaging 28+ minutes.
   - Blowout risk: starters lose 0.25 min per point of expected margin beyond 9 points.
3. **Minutes distribution.** Projected mean × a draw from the league's **empirical
   minutes-to-own-mean ratio** for the player's role (starter / rotation / bench), estimated
   only from games before the cutoff. This reproduces early exits (injury, fouls, ejection)
   and blowout benching. In 2025-26, rotation players had a ratio SD of 0.245, and 5% of
   their games came in below 0.58.
4. **Per-minute rates** for points, rebounds, assists, threes, steals, blocks and turnovers.
   Each is a Gamma posterior:
   - Data: the current season with the same 5/3/1 weights, scaled to its effective sample size.
   - Prior: last season's rate, itself shrunk toward the league positional rate (G/F/C).
   - Prior strength, in minutes: points 120, rebounds/assists 150, threes 225,
     steals/blocks 350, turnovers 250.
5. **Context multipliers.**
   - Pace: opponent pace ÷ league pace, with team pace shrunk (k = 10 games).
   - Opponent allowance: per possession, per stat, shrunk (k = 12), applied at half strength.
   - Home/away: √ of the league's home/away scoring ratio, estimated at 1.012.
   - Usage: when high-usage teammates are out, boosted ≤ 1.08 (heuristic).
6. **Simulation (10,000 per player).** Each draw combines:
   - Minutes, as in step 3.
   - A rate posterior draw per batch (20 batches × 500). The spread between batches gives
     the probability's *parameter uncertainty*.
   - A shared per-game "form" shock (lognormal σ 0.2).
   - Each stat ~ negative binomial with variance = φ × mean. φ is estimated from data: points
     2.2; other stats ≈ 1.0–1.06.
   - Stats are joined by a Gaussian copula fitted to league residuals (points–threes 0.62).

   Combos (PRA, PR, PA, RA) are sums of jointly drawn components, never products of
   independent probabilities.

## 3. Pricing and decisions (NO BET by default)
- **Implied probability:** 1/decimal.
- **No-vig, standard lines:** the two sides of the same book's line, normalized.
- **No-vig, one-sided milestones:** an approximation (implied ÷ the book's median overround),
  flagged with `*`.
- **EV per unit:** `p_play × [P(win|play)·(dec−1) − P(loss|play)]`. Pushes and voids return 0.
- **Fair odds:** from P(win) among decided outcomes.
- **Conservative probability:** estimate − parameter uncertainty − calibration margin
  (0.02 for main lines, 0.04 for alternates).
- **PAPER_CANDIDATE** requires **every** gate to pass:
  - Book is FanDuel or Fanatics.
  - Quote is fresh (not stale, seen ≤ 20 min ago).
  - `p_play` ≥ 0.90.
  - At least 10 games of history, including 3 this season.
  - Confidence is not low.
  - Price is 1.25–5.0 decimal (alternates: ≤ 3.0).
  - Model is within 12 points of the no-vig probability. A larger gap is treated as model
    error, not edge.
  - Alternates only: the model also agrees within 5 points on that player's main line.
  - EV at the conservative probability ≥ 3% (main) / 6% (alternates).
- **WATCH:** positive point-estimate EV that fails a gate, at a sane price.
- **NO_BET:** everything else.
- Candidates are labeled **paper-trade only** until at least 300 live props have been graded.
- Raw hit rates are shown only as descriptive statistics, with sample size and a Wilson 95%
  interval. They are never used as probabilities.

## 4. Validation results (2026-11-07)
**SIMULATED LINES.** No historical sportsbook prices exist yet, so the line for each
player-game is `floor(prior L10 mean) + 0.5`, built only from information before the game.
These results measure projection calibration. They say nothing about profitability against
real prices.

| Evaluation | Rows | Model Brier | Raw L10 hit-rate Brier | Constant Brier | Model ECE | Hit-rate ECE |
|---|---|---|---|---|---|---|
| **Out-of-sample**: 2026-27 season to date (16 dates, never used for tuning) | 26,158 | **0.2207** | 0.2401 | 0.2360 | **0.021** | 0.071 |
| 2025-26, every 4th date (overlaps the tuning sample) | 54,439 | 0.2332 | 0.2450 | 0.2450 | 0.022 | 0.067 |

Out of sample, the model beats both baselines in **every** market.

How v1.2 was chosen: 2025-26, every 9th date, 25,179 rows, controlled variants.

| Variant | Brier | ECE | Points bias |
|---|---|---|---|
| v1.1 | 0.2362 | 0.037 | −0.43 |
| no blanket 240-min rescale | 0.2349 | 0.031 | −0.03 |
| + empirical minutes | 0.2350 | 0.030 | |
| + form noise 0.2 | 0.2342 | 0.026 | |
| + half-strength prior | 0.2334 | | |
| + 5/3/1 recency | 0.2338 | 0.022 | |
| **combined (v1.2)** | **0.2328** | **0.018** | |

Raw hit rates are badly overconfident. When the L10 hit rate said 80%, the over hit about
42% of the time. This is the reason the project never treats them as probabilities.

**Live market comparison (real prices, no outcomes yet).** On 1,133 two-sided main lines
from 7 games (run #4), the model's probability differs from the no-vig price by 7.4 points on
average. Its mean tilt is −2.7 points on overs: the model rates overs lower than the market
does. Two explanations fit, the model running low or books shading overs (a common
practice), and real outcomes are needed to tell them apart. That run produced 0
PAPER_CANDIDATEs, so **NO BET**.

## 4b. First real outcomes (2026-11-07 slate, graded 2026-11-08) — REAL lines, one night

Each FanDuel/Fanatics quote's last pre-tip evaluation, graded against ESPN final box scores.
7 games, 88 players, 10,858 decided quotes. The quotes are heavily correlated (alternate
ladders of the same players; both sides of each main line), so the effective sample is
far smaller than the row count. **One night is not evidence of anything either way.**

| Slice | n decided | Model Brier | Market no-vig Brier | Model mean prob | Observed |
|---|---|---|---|---|---|
| All quotes | 10,858 | 0.1871 | **0.1731** | 0.410 avg | 0.410 base rate |
| Main lines, two-sided (both sides) | 2,478 | 0.2587 | **0.2463** | — | — |
| Main-line overs | 1,239 | | | 0.463 (market 0.492) | **0.449** |
| Alternates / milestones | 8,380 | 0.1659 | **0.1514** | 0.405 | 0.384 |
| WATCH decisions | 1,308 | 0.2514 | **0.2403** | 0.545 | **0.440** |

Read-out:
- The market beat the model on Brier in almost every market and both books. The model was
  only level or slightly better on 3PM, RA and STL main lines, on small counts.
- Overs hit less often than the market implied (44.9% vs 49.2%). The model's under-tilt
  (§4) pointed the right way that night, but its probabilities were noisier than the
  market's, so its Brier was worse.
- WATCH quotes (positive point-estimate EV, failed a gate) won 44.0% against the model's
  54.5%. That is below even the market's 47.6%. The gates that kept them from becoming
  candidates look justified, and a positive point-estimate EV is not an edge.
- CLV is **unavailable** for this slate. No odds were captured after the last model run
  (no scheduler was running overnight), so the "close" would equal the evaluated price. It
  is recorded as missing, not as 0.

## 5. Known weaknesses (what to watch)
- **Less accurate than the market on the first real slate** (§4b): until many slates say
  otherwise, assume the no-vig price is the better probability.
- **Early-season over-projection:** points bias +0.78 and PRA +1.05 in the 2026-27 sample.
  Last-season priors carry too much weight in the first weeks.
- **Combos lag the singles:** PRA, PR and PA beat a constant clearly out of sample, but only
  match it on 2025-26.
- **Under-tilt shows up in WATCH:** the WATCH list is dominated by combo **unders**, which
  matches the −2.7-point tilt. Treat it as a model signature, not an opportunity.
- **Heuristics not yet validated against real outcomes:** the injury-status → `p_play`
  mapping, usage boost, blowout and back-to-back adjustments, the calibration margins and the
  approximate milestone no-vig.
- **No position-specific defensive matchup modeling:** the data is too thin to trust it.
- **Unmatched odds players are reported, never guessed:** e.g. players with no NBA box-score
  history yet.

## 6. Leakage controls
- Every projection for date D uses only box scores of games that started before the first
  tip-off on D. This covers league context too (pace, φ, correlations, minutes ratios).
- A test verifies that changing D's results doesn't change D's projections.
- Backtests use no injury information, because it isn't available historically. They score
  only players who actually played, which matches void rules.

## 7. Running it
```bash
nba-edge ingest-espn --start 2025-10-21 --end 2026-06-20   # one-time history backfill (~10 min)
nba-edge ingest-espn --days 2 && nba-edge ingest-injuries   # daily (the scheduler does this)
nba-edge snapshot && nba-edge project                       # odds + slate model
nba-edge grade                                              # after games finish (the scheduler does this)
nba-edge paper --all-quotes                                 # REAL paper ledger + all-quotes calibration
nba-edge backtest --start 2026-10-21 --end 2026-11-06 --every 1
nba-edge serve                                              # http://127.0.0.1:8765
nba-edge run-scheduler                                      # everything, automatically (docs/OPERATIONS.md)
nba-edge health                                             # exit 0 ok / 1 warn / 2 critical
```
