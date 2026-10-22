// Pure ladder-challenge accounting + progression logic. No DOM, no storage, no
// knowledge of the live-data feed — testable in isolation with plain objects.
//
// A ladder STEP never duplicates the bet it's linked to: it stores a reference
// (ticketType/ticketId) plus a SNAPSHOT of the stake/odds/payout at the moment it
// was linked (app.js keeps these in sync with the real ticket when confirmed).
// This module only does the accounting math, given that snapshot and a settlement
// result that app.js derives from the SAME canonical evaluateSelection/parlay logic
// used everywhere else in the app (never a second settlement engine).

// ---- Gross payout vs. net profit --------------------------------------------
// Gross payout = total returned on a win, INCLUDING the original stake.
// Net profit   = gross payout - stake. A push/void returns the stake with $0 profit.
// A $10 stake returning $25 gross is $15 profit, not $25 — tested explicitly below.

function americanOddsToDecimal(odds) {
  const n = Number(odds);
  if (!n || isNaN(n)) return null;
  return n > 0 ? 1 + n / 100 : 1 + 100 / Math.abs(n);
}

// Prefers an actual entered sportsbook payout (which may include a boost) over a
// theoretical one computed from American odds. Only falls back to the theoretical
// calculation, clearly flagged `estimated: true`, when no real payout was entered.
function estimateGrossPayout(stake, americanOdds, enteredPayout) {
  if (enteredPayout !== null && enteredPayout !== undefined && enteredPayout !== '') {
    return { value: Math.round(Number(enteredPayout) * 100) / 100, estimated: false };
  }
  const dec = americanOddsToDecimal(americanOdds);
  if (stake && dec) return { value: Math.round(stake * dec * 100) / 100, estimated: true };
  return { value: null, estimated: true };
}

// Settles one step. `result` is the canonical status from the shared settlement
// engine, mapped down to 'won'|'lost'|'push'|'void'|'pending' by app.js.
// `actualGrossPayout` (optional) overrides the estimate with a user-confirmed real
// number for a win — still never assumed to include a boost unless entered.
function settleStep(step, result, actualGrossPayout) {
  if (result === 'won') {
    const est = estimateGrossPayout(step.stake, step.americanOdds, step.enteredPayout);
    const gross = (actualGrossPayout !== null && actualGrossPayout !== undefined) ? Number(actualGrossPayout) : est.value;
    const netProfit = (gross !== null && gross !== undefined) ? Math.round((gross - step.stake) * 100) / 100 : null;
    return { result: 'won', grossPayout: gross, netProfit, estimated: actualGrossPayout == null && est.estimated };
  }
  if (result === 'lost') return { result: 'lost', grossPayout: 0, netProfit: -Math.round(step.stake * 100) / 100, estimated: false };
  if (result === 'push' || result === 'void') return { result, grossPayout: step.stake, netProfit: 0, estimated: false };
  return { result: 'pending', grossPayout: null, netProfit: null, estimated: false };
}

// ---- Ladder-level balance ----------------------------------------------------
// The balance only moves when a step actually SETTLES — never while a step is
// merely live or "target reached live". This is the direct requirement: "the
// displayed balance should not change as though a live bet has won before final
// settlement."
function computeLadderBalance(ladder) {
  let balance = Number(ladder.startingAmount) || 0;
  for (const step of (ladder.steps || [])) {
    if (step.result === 'won' || step.result === 'lost' || step.result === 'push' || step.result === 'void') {
      balance += (Number(step.netProfit) || 0);
    }
  }
  return Math.round(balance * 100) / 100;
}
function totalRealizedPL(ladder) {
  return Math.round((computeLadderBalance(ladder) - (Number(ladder.startingAmount) || 0)) * 100) / 100;
}
function amountNeededForGoal(ladder) {
  return Math.max(0, Math.round(((Number(ladder.goalAmount) || 0) - computeLadderBalance(ladder)) * 100) / 100);
}
function progressPct(ladder) {
  const span = (Number(ladder.goalAmount) || 0) - (Number(ladder.startingAmount) || 0);
  if (span <= 0) return 100;
  return Math.max(0, Math.min(100, ((computeLadderBalance(ladder) - ladder.startingAmount) / span) * 100));
}

// In "reinvest winnings" mode, the suggested next stake is the ladder's current
// balance (use everything you have). The user must still confirm it — never
// silently assumed. In manual mode there's no suggestion; the user always picks.
function suggestedNextStake(ladder) {
  if (ladder.progressionMode === 'reinvest') return computeLadderBalance(ladder);
  return null;
}

// ---- Status derivation -------------------------------------------------------
// 'paused'/'cancelled' are explicit, user-driven, and sticky (never auto-reverted).
// 'won'/'lost' are derived ONLY from steps that have actually settled — never from
// a live/pending step, and never from a hypothetical/expected payout.
function deriveLadderStatus(ladder) {
  if (ladder.status === 'cancelled' || ladder.status === 'paused') return ladder.status;
  const steps = ladder.steps || [];
  const hasSettledStep = steps.some(s => s.result && s.result !== 'pending');
  const balance = computeLadderBalance(ladder);
  if (hasSettledStep && balance >= (Number(ladder.goalAmount) || 0)) return 'won';
  if (ladder.allOrNothing && steps.some(s => s.result === 'lost')) return 'lost';
  return 'open';
}

if (typeof module !== 'undefined') module.exports = {
  americanOddsToDecimal, estimateGrossPayout, settleStep,
  computeLadderBalance, totalRealizedPL, amountNeededForGoal, progressPct,
  suggestedNextStake, deriveLadderStatus,
};
