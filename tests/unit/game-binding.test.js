// Regression + unit tests for the v6.1 settlement fix (game-binding.js).
// The central guarantee under test: a bet can NEVER be matched to, or settled from,
// an unrelated newer game for the same team/player.
const test = require('node:test');
const assert = require('node:assert/strict');
const B = require('../../game-binding.js');

const H = 3600e3, D = 24 * H;
const NOW = Date.UTC(2026, 10, 7, 23, 0); // 2026-11-07 18:00 ET
const game = (id, home, away, startTs, extra = {}) => ({ id, home, away, homeScore: '110', awayScore: '100', state: 'post', completed: true, startTimestamp: startTs, gameDate: B.etDateString(startTs), players: [], ...extra });
const prop = (extra = {}) => ({ kind: 'prop', player: 'Jayson Tatum', team: 'BOS', opponent: null, market: 'PTS', target: 25.5, direction: 'over', createdAt: NOW, gameId: null, settlement: null, ...extra });
const teamBet = (extra = {}) => ({ kind: 'team', marketType: 'gameTotal', teamAbbr: 'BOS', oppAbbr: null, line: 205.5, direction: 'over', createdAt: NOW, gameId: null, settlement: null, ...extra });

test('REGRESSION: a 3-day-old bet never sees today\'s game for the same team', () => {
  const todays = game('9001', 'BOS', 'NYK', NOW - 3 * H);
  for (const sel of [prop({ createdAt: NOW - 3 * D, opponent: 'MIA' }), prop({ createdAt: NOW - 3 * D }), teamBet({ createdAt: NOW - 3 * D })]) {
    assert.deepEqual(B.candidateGamesForSelection(sel, [todays]), []);
  }
});

test('REGRESSION: a bound bet only ever sees its own bound game', () => {
  const mine = game('1111', 'BOS', 'MIA', NOW - 50 * H);
  const newer = game('2222', 'BOS', 'NYK', NOW - 2 * H);
  const sel = prop({ createdAt: NOW - 52 * H, gameId: '1111' });
  assert.deepEqual(B.candidateGamesForSelection(sel, [newer, mine]).map(g => g.id), ['1111']);
  assert.deepEqual(B.candidateGamesForSelection(sel, [newer]), [], 'bound game not loaded -> no candidates, never a substitute');
});

test('REGRESSION: settlement is never persisted from a game other than the bound one', () => {
  const sel = prop({ gameId: '1111' });
  const ev = { status: 'won', current: 30, g: game('2222', 'BOS', 'NYK', NOW - 3 * H), a: { name: 'Jayson Tatum', team: 'BOS', stats: { PTS: 30 } } };
  assert.equal(B.shouldPersistSettlement(sel, ev), false);
});

test('plausibility window: 30h before / 30h after the bet was recorded', () => {
  assert.equal(B.isPlausibleGameForBet(game('1', 'BOS', 'NYK', NOW + 29 * H), NOW), true);
  assert.equal(B.isPlausibleGameForBet(game('1', 'BOS', 'NYK', NOW + 31 * H), NOW), false);
  assert.equal(B.isPlausibleGameForBet(game('1', 'BOS', 'NYK', NOW - 29 * H), NOW), true);
  assert.equal(B.isPlausibleGameForBet(game('1', 'BOS', 'NYK', NOW - 31 * H), NOW), false);
  assert.equal(B.isPlausibleGameForBet(game('1', 'BOS', 'NYK', NOW), undefined), false, 'no createdAt -> never guessed');
});

test('ranking prefers the game that had not started yet when the bet was recorded (back-to-back)', () => {
  const yesterday = game('y', 'BOS', 'NYK', NOW - 24 * H);
  const tonight = game('t', 'BOS', 'MIA', NOW + 1 * H, { state: 'pre' });
  assert.equal(B.candidateGamesForSelection(prop(), [yesterday, tonight])[0].id, 't');
});

test('explicit opponent is a hard constraint', () => {
  const g = game('1', 'BOS', 'NYK', NOW + H);
  assert.deepEqual(B.candidateGamesForSelection(prop({ opponent: 'MIA' }), [g]), []);
  assert.equal(B.candidateGamesForSelection(prop({ opponent: 'Knicks' }), [g]).length, 1, 'nickname resolves to NYK');
  assert.equal(B.candidateGamesForSelection(teamBet({ oppAbbr: 'NY' }), [g]).length, 1, 'ESPN code resolves to NYK');
});

test('explicit ET game date (gameDateHint) overrides the window and disambiguates', () => {
  const a = game('a', 'BOS', 'NYK', Date.UTC(2026, 10, 3, 0, 30)); // Nov 2 ET evening
  const b = game('b', 'BOS', 'MIA', Date.UTC(2026, 10, 4, 0, 30)); // Nov 3 ET evening
  const sel = prop({ createdAt: NOW - 10 * D, gameDateHint: '2026-11-02' });
  assert.deepEqual(B.candidateGamesForSelection(sel, [a, b]).map(g => g.id), ['a']);
});

test('canonical team codes: app codes, ESPN codes, names and nicknames agree', () => {
  for (const [x, want] of [['GS', 'GSW'], ['GSW', 'GSW'], ['Warriors', 'GSW'], ['Golden State Warriors', 'GSW'], ['NY', 'NYK'], ['SA', 'SAS'], ['NO', 'NOP'], ['UTAH', 'UTA'], ['WSH', 'WAS'], ['LAL', 'LAL'], ['LA', 'LAC'], ['bos', 'BOS']]) {
    assert.equal(B.canonicalTeam(x), want, x);
  }
  assert.equal(B.canonicalTeam(''), null);
  assert.equal(B.sameTeam('GSW', 'GS'), true);
  assert.equal(B.sameTeam('LAL', 'LAC'), false);
});

test('ET dates: late-night West Coast time still maps to the Eastern schedule day', () => {
  // 2026-11-08 06:30 UTC = 01:30 ET Nov 8 = 22:30 PT Nov 7
  assert.equal(B.etDateString(Date.UTC(2026, 10, 8, 6, 30)), '2026-11-08');
  assert.equal(B.etDateString(Date.UTC(2026, 10, 8, 3, 30)), '2026-11-07'); // 22:30 ET Nov 7
  assert.equal(B.etDateParam(Date.UTC(2026, 10, 8, 3, 30), 0), '20261107');
  assert.equal(B.etDateParam(Date.UTC(2026, 10, 8, 3, 30), -1), '20261106');
  assert.equal(B.etDateParam(Date.UTC(2026, 2, 1, 12), -1), '20260228', 'month rollover');
  assert.equal(B.etDateString(null), null);
});

test('settlement record captures game id, ET date, settledAt and the evidence; status re-derives from it', () => {
  const g = game('777', 'BOS', 'NYK', Date.UTC(2026, 10, 7, 0, 30));
  const a = { espnId: '123', name: 'Jayson Tatum', team: 'BOS', minutes: 36, stats: { PTS: 30, REB: 8, AST: 5 } };
  const sel = prop({ gameId: '777' });
  const ev = { status: 'won', current: 30, g, a };
  assert.equal(B.shouldPersistSettlement(sel, ev), true);
  const st = B.buildSettlement(sel, ev, NOW);
  assert.equal(st.gameId, '777');
  assert.equal(st.gameDate, '2026-11-06');
  assert.equal(st.settledAt, NOW);
  assert.equal(st.status, 'won');
  assert.deepEqual(st.snapshot.athlete.stats, { PTS: 30, REB: 8, AST: 5 });
  // a retroactive line edit is re-judged from the SAVED stat line
  assert.equal(B.evaluateFromSettlement({ ...sel, target: 31.5 }, st).status, 'lost');
  assert.equal(B.evaluateFromSettlement({ ...sel, target: 30 }, st).status, 'push');
  assert.equal(B.evaluateFromSettlement({ ...sel, market: 'PRA', target: 40.5 }, st).status, 'won');
});

test('team-bet settlement evidence works with ESPN vs app team codes', () => {
  const g = game('888', 'GS', 'NY', NOW - 5 * H, { homeScore: '120', awayScore: '101' });
  const sel = teamBet({ teamAbbr: 'GSW', marketType: 'spread', line: -10.5, gameId: '888' });
  const st = B.buildSettlement(sel, { status: 'won', current: 19, g, a: null }, NOW);
  assert.equal(B.evaluateFromSettlement(sel, st).status, 'won');
  assert.equal(B.evaluateFromSettlement({ ...sel, line: -19 }, st).status, 'push');
  assert.equal(B.evaluateFromSettlement({ ...sel, marketType: 'moneyline', direction: 'opponent' }, st).status, 'lost');
});

test('never settles: live game, postponed (post but not completed), unmatched athlete, or non-final status', () => {
  const a = { name: 'X', team: 'BOS', stats: { PTS: 30 } };
  const sel = prop();
  assert.equal(B.shouldPersistSettlement(sel, { status: 'target_reached_live', g: game('1', 'BOS', 'NYK', NOW, { state: 'in' }), a }), false);
  assert.equal(B.shouldPersistSettlement(sel, { status: 'won', g: game('1', 'BOS', 'NYK', NOW, { completed: false }), a }), false);
  assert.equal(B.shouldPersistSettlement(sel, { status: 'won', g: game('1', 'BOS', 'NYK', NOW), a: null }), false);
  assert.equal(B.shouldPersistSettlement(sel, { status: 'unmatched', g: game('1', 'BOS', 'NYK', NOW), a }), false);
  assert.equal(B.shouldPersistSettlement({ ...sel, voided: true }, { status: 'won', g: game('1', 'BOS', 'NYK', NOW), a }), false);
});

test('identity edits clear the binding; line edits do not', () => {
  const before = prop({ gameId: '1', settlement: { status: 'won' } });
  assert.equal(B.identityChanged(before, { ...before, target: 30.5 }), false);
  assert.equal(B.identityChanged(before, { ...before, player: 'Jaylen Brown' }), true);
  assert.equal(B.identityChanged(before, { ...before, opponent: 'MIA' }), true);
  assert.equal(B.identityChanged(before, { ...before, gameDateHint: '2026-11-01' }), true);
  const s = { ...before }; B.clearBinding(s);
  assert.equal(s.gameId, null); assert.equal(s.settlement, null); assert.equal(s.gameDate, null);
});

test('parses ESPN scoreboard events and per-event summaries into the same shape', () => {
  const comp = { competitors: [{ homeAway: 'home', team: { abbreviation: 'BOS' }, score: '110' }, { homeAway: 'away', team: { abbreviation: 'NY' }, score: '99' }] };
  const status = { type: { state: 'post', completed: true, name: 'STATUS_FINAL', description: 'Final' }, period: 4, displayClock: '0:00' };
  const fromBoard = B.gameFromScoreboardEvent({ id: '401', date: '2026-11-07T00:30Z', status, competitions: [comp] });
  const fromSummary = B.gameFromSummary({ header: { id: '401', competitions: [{ ...comp, date: '2026-11-07T00:30Z', status }] } });
  for (const g of [fromBoard, fromSummary]) {
    assert.equal(g.id, '401'); assert.equal(g.home, 'BOS'); assert.equal(g.away, 'NY');
    assert.equal(g.homeScore, '110'); assert.equal(g.state, 'post'); assert.equal(g.completed, true);
    assert.equal(g.gameDate, '2026-11-06');
  }
  const half = B.gameFromScoreboardEvent({ id: '5', date: '2026-11-07T00:30Z', status: { type: { state: 'in', description: 'Halftime' } }, competitions: [comp] });
  assert.equal(half.state, 'halftime');
  assert.equal(B.gameFromScoreboardEvent({ id: '6' }), null);
  assert.equal(B.gameFromSummary({}), null);
});
