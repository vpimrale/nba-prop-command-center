// Pure-logic tests for the pre-existing modules (rebuilt v6.1 — the v6 README listed
// these test files but they were not in the repository).
const test = require('node:test');
const assert = require('node:assert/strict');
const { evaluateOverUnderLine, lineHelperText } = require('../../line-rules.js');
const { evaluatePropLeg, getStatForMarket, componentsForMarket, marketLabel } = require('../../prop-status.js');
const { evaluateTeamBet, evaluateParlay } = require('../../settlement.js');
const L = require('../../ladder.js');
const A = require('../../archive.js');
const { parseBetSlipText, detectOverUnder, detectOddsToken, detectMarket } = require('../../parse-slip.js');
const { NBA_TEAMS } = require('../../teams.js');

test('line rules: whole-number lines push exactly on the number, half-point lines never push', () => {
  const f = (current, direction, line) => evaluateOverUnderLine({ current, direction, line, isFinal: true }).status;
  assert.equal(f(17, 'over', 17), 'push'); assert.equal(f(18, 'over', 17), 'won'); assert.equal(f(16, 'over', 17), 'lost');
  assert.equal(f(17, 'under', 17), 'push'); assert.equal(f(16, 'under', 17), 'won'); assert.equal(f(18, 'under', 17), 'lost');
  assert.equal(f(18, 'over', 17.5), 'won'); assert.equal(f(17, 'over', 17.5), 'lost');
  assert.equal(f(17, 'under', 17.5), 'won'); assert.equal(f(18, 'under', 17.5), 'lost');
});
test('line rules: live never reports won; an Over that is already reached is target_reached_live', () => {
  assert.equal(evaluateOverUnderLine({ current: 20, direction: 'over', line: 17.5, isFinal: false }).status, 'target_reached_live');
  assert.equal(evaluateOverUnderLine({ current: 10, direction: 'over', line: 17.5, isFinal: false }).status, 'live');
  assert.equal(evaluateOverUnderLine({ current: 10, direction: 'under', line: 17.5, isFinal: false }).status, 'live');
});
test('line helper text describes win/push precisely', () => {
  assert.match(lineHelperText('over', 17), /wins at 18 or higher, pushes .* at exactly 17/);
  assert.match(lineHelperText('under', 17.5), /wins at 17 or lower — a half-point line never pushes/);
  assert.equal(lineHelperText('over', 'abc'), '');
});

test('prop status: combos, upcoming, unmatched, final', () => {
  const stats = { PTS: 20, REB: 6, AST: 5, '3PT': 3, STL: 1, BLK: 2, TO: 4, PF: 3 };
  assert.equal(getStatForMarket(stats, 'PRA'), 31);
  assert.equal(getStatForMarket(stats, 'PR'), 26); assert.equal(getStatForMarket(stats, 'PA'), 25); assert.equal(getStatForMarket(stats, 'RA'), 11);
  assert.equal(getStatForMarket(stats, '3PM'), 3);
  assert.deepEqual(componentsForMarket('PRA'), ['PTS', 'REB', 'AST']);
  assert.equal(marketLabel('PR'), 'PTS + REB');
  assert.equal(evaluatePropLeg({ market: 'PTS', target: 10, direction: 'over' }, null, 'pre').status, 'upcoming');
  assert.equal(evaluatePropLeg({ market: 'PTS', target: 10, direction: 'over' }, null, 'in').status, 'unmatched');
  assert.equal(evaluatePropLeg({ market: 'PRA', target: 30.5, direction: 'over' }, stats, 'post').status, 'won');
  assert.equal(evaluatePropLeg({ market: 'PRA', target: 31, direction: 'over' }, stats, 'post').status, 'push');
});

test('team bets: totals, spread (incl. push), moneyline, unmatched', () => {
  const g = { state: 'post', homeAbbr: 'BOS', awayAbbr: 'NYK', homeScore: '110', awayScore: '100' };
  assert.equal(evaluateTeamBet({ marketType: 'gameTotal', direction: 'over', line: 209.5 }, g).status, 'won');
  assert.equal(evaluateTeamBet({ marketType: 'teamTotal', team: 'NYK', direction: 'under', line: 100 }, g).status, 'push');
  assert.equal(evaluateTeamBet({ marketType: 'spread', team: 'BOS', line: -10 }, g).status, 'push');
  assert.equal(evaluateTeamBet({ marketType: 'spread', team: 'BOS', line: -10.5 }, g).status, 'lost');
  assert.equal(evaluateTeamBet({ marketType: 'spread', team: 'NYK', line: 10.5 }, g).status, 'won');
  assert.equal(evaluateTeamBet({ marketType: 'moneyline', team: 'NYK', direction: 'opponent' }, g).status, 'won');
  assert.equal(evaluateTeamBet({ marketType: 'teamTotal', team: 'MIA', direction: 'over', line: 1 }, g).status, 'unmatched');
  assert.equal(evaluateTeamBet({ marketType: 'gameTotal', direction: 'over', line: 1 }, null).status, 'unmatched');
  assert.equal(evaluateTeamBet({ marketType: 'gameTotal', direction: 'over', line: 1 }, { ...g, state: 'pre' }).status, 'upcoming');
  assert.equal(evaluateTeamBet({ marketType: 'spread', team: 'BOS', line: -5 }, { ...g, state: 'in' }).status, 'live');
});

test('parlay aggregation: any loss loses; pushes drop out; all-push pushes; unmatched needs review', () => {
  assert.equal(evaluateParlay(['won', 'lost', 'live']), 'lost');
  assert.equal(evaluateParlay(['won', 'push']), 'won');
  assert.equal(evaluateParlay(['push', 'push']), 'push');
  assert.equal(evaluateParlay(['won', 'unmatched']), 'needs_review');
  assert.equal(evaluateParlay(['won', 'live']), 'live');
});

test('ladder math: odds conversion, gross vs net, balance moves only on settlement', () => {
  assert.equal(L.americanOddsToDecimal(150), 2.5);
  assert.ok(Math.abs(L.americanOddsToDecimal(-110) - 1.909090909) < 1e-6);
  assert.equal(L.americanOddsToDecimal('x'), null);
  assert.deepEqual(L.estimateGrossPayout(10, '+150', null), { value: 25, estimated: true });
  assert.deepEqual(L.estimateGrossPayout(10, '+150', 27), { value: 27, estimated: false });
  const step = { stake: 10, americanOdds: '+150', enteredPayout: null };
  assert.equal(L.settleStep(step, 'won').netProfit, 15);
  assert.equal(L.settleStep(step, 'lost').netProfit, -10);
  assert.equal(L.settleStep(step, 'push').grossPayout, 10);
  const ladder = { startingAmount: 10, goalAmount: 100, steps: [{ result: 'won', netProfit: 15 }, { result: 'pending', netProfit: 999 }], progressionMode: 'reinvest', allOrNothing: true };
  assert.equal(L.computeLadderBalance(ladder), 25);
  assert.equal(L.suggestedNextStake(ladder), 25);
  assert.equal(L.amountNeededForGoal(ladder), 75);
  assert.equal(L.deriveLadderStatus(ladder), 'open');
  assert.equal(L.deriveLadderStatus({ ...ladder, steps: [{ result: 'lost', netProfit: -10 }] }), 'lost');
  assert.equal(L.deriveLadderStatus({ ...ladder, goalAmount: 20 }), 'won');
});

test('archive: 30-minute window from first final observation; never reverted by a transient unmatched state', () => {
  const t0 = 1_000_000;
  assert.equal(A.computeFinalizedAt(['post', 'in'], null, t0), null);
  assert.equal(A.computeFinalizedAt(['post', null], null, t0), null);
  assert.equal(A.computeFinalizedAt([], null, t0), null);
  const fin = A.computeFinalizedAt(['post', 'post'], null, t0);
  assert.equal(fin, t0);
  assert.equal(A.computeFinalizedAt([null], fin, t0 + 5), t0, 'reload with unmatched games keeps the timestamp');
  assert.equal(A.isArchived(fin, t0 + A.ARCHIVE_DELAY_MS - 1), false);
  assert.equal(A.isArchived(fin, t0 + A.ARCHIVE_DELAY_MS), true);
  assert.equal(A.minutesRemaining(fin, t0 + 60_000), 29);
});

test('bet-slip parsing: parlay header, odds, stake, legs', () => {
  assert.deepEqual(detectOverUnder('Over 24.5'), { direction: 'over', line: 24.5 });
  assert.deepEqual(detectOverUnder('U 7'), { direction: 'under', line: 7 });
  assert.equal(detectOddsToken('Odds -115 here'), '-115');
  assert.equal(detectMarket('Pts + Reb + Ast'), 'PRA');
  const players = [{ name: 'Jayson Tatum', team: 'BOS' }, { name: 'Jalen Brunson', team: 'NYK' }];
  const r = parseBetSlipText('2 Leg Parlay\nJayson Tatum\nPoints\nOver 24.5\n-115\nJalen Brunson\nAssists\nOver 6.5\n+105\nWager $10.00\nTo Win $25.50', { playerIndex: players, teamIndex: [] });
  assert.equal(r.isParlay, true); assert.equal(r.legHintCount, 2);
  assert.equal(r.stake, 10); assert.equal(r.potentialPayout, 25.5);
  assert.equal(r.legs.length, 2);
  assert.equal(r.legs[0].name, 'Jayson Tatum'); assert.equal(r.legs[0].marketType, 'PTS'); assert.equal(r.legs[0].line, 24.5); assert.equal(r.legs[0].odds, '-115');
  assert.equal(r.legs[1].marketType, 'AST'); assert.equal(r.legs[1].needsReview, false);
});

test('teams: 30 unique teams', () => {
  assert.equal(NBA_TEAMS.length, 30);
  assert.equal(new Set(NBA_TEAMS.map(t => t.abbr)).size, 30);
});
