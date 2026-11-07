// End-to-end regression tests for the v6.1 settlement fix, in a real browser against
// the real app, with a SYNTHETIC (mocked) ESPN feed.
const test = require('node:test');
const assert = require('node:assert/strict');
const { openApp, makeEvent, makeSummary, emptyState, propBet, teamBet, etDateString, H, D } = require('./harness.js');

const TATUM = { id: '123', name: 'Jayson Tatum', team: 'BOS' };

test('REGRESSION: old bets and old parlay legs are never settled against a newer same-team game', async () => {
  const now = Date.now();
  const today = makeEvent({ id: '9001', home: 'BOS', away: 'NYK', homeScore: 110, awayScore: 100, start: now - 3 * H });
  const old = now - 3 * D;
  const seed = emptyState({
    straightBets: [
      propBet({ id: 'old-prop', opponent: 'MIA', playerId: '123', createdAt: old }),
      propBet({ id: 'old-prop-noopp', createdAt: old }),
      teamBet({ id: 'old-team', createdAt: old, line: 250 }),
    ],
    parlays: [{ id: 'old-parlay', name: 'Old parlay', createdAt: old, stake: 5, combinedOdds: '+200', potentialPayout: null, legs: [
      { legId: 'l1', kind: 'prop', player: 'Jayson Tatum', team: 'BOS', opponent: null, market: 'PTS', target: 20.5, direction: 'over', odds: null, voided: false, playerId: '123' },
      { legId: 'l2', kind: 'team', marketType: 'gameTotal', teamAbbr: 'BOS', oppAbbr: null, line: 200.5, direction: 'over', odds: null, voided: false },
    ] }],
  });
  const app = await openApp({ seed, feed: { events: [today], summaries: { 9001: makeSummary(today, [{ ...TATUM, pts: 30 }]) } } });
  try {
    const rows = await app.historyRows();
    assert.equal(rows.length, 4);
    for (const r of rows) assert.equal(r.status, 'Needs review', `${r.label} must not settle from an unrelated newer game`);
    const s = await app.stored();
    for (const b of s.straightBets) { assert.equal(b.gameId, null); assert.equal(b.settlement, null); }
    for (const l of s.parlays[0].legs) { assert.equal(l.gameId, null); assert.equal(l.settlement, null); assert.equal(l.createdAt, old, 'leg inherits its ticket createdAt'); }
  } finally { await app.close(); }
});

test('a bet settles against its own game and the result is persisted with game id, ET date and settledAt; survives the game leaving the feed and a newer game appearing', async () => {
  const now = Date.now();
  const mine = makeEvent({ id: '7001', home: 'BOS', away: 'MIA', homeScore: 112, awayScore: 104, start: now - 4 * H });
  const feed = { events: [mine], summaries: { 7001: makeSummary(mine, [{ ...TATUM, pts: 31, reb: 7, ast: 4 }]) } };
  const seed = emptyState({ straightBets: [propBet({ id: 'p1', playerId: '123', createdAt: now - 6 * H })] });
  const app = await openApp({ seed, feed });
  try {
    assert.equal((await app.historyRows())[0].status, 'WON');
    let b = (await app.stored()).straightBets[0];
    assert.equal(b.gameId, '7001');
    assert.equal(b.gameDate, etDateString(Date.parse(mine.date)));
    assert.equal(b.settlement.status, 'won');
    assert.equal(b.settlement.gameId, '7001');
    assert.equal(b.settlement.current, 31);
    assert.equal(typeof b.settlement.settledAt, 'number');
    assert.equal(b.settlement.snapshot.athlete.stats.PTS, 31);
    const settledAt = b.settlement.settledAt;

    // The game leaves the feed; a NEWER BOS game (still inside the bet's plausibility
    // window, with a losing stat line for the same player) appears. Reload.
    const newer = makeEvent({ id: '7002', home: 'NYK', away: 'BOS', homeScore: 99, awayScore: 101, start: now - 1 * H });
    feed.events = [newer];
    feed.summaries = { 7002: makeSummary(newer, [{ ...TATUM, pts: 8 }]) };
    await app.reload();
    assert.equal((await app.historyRows())[0].status, 'WON', 'persisted result must not change');
    b = (await app.stored()).straightBets[0];
    assert.equal(b.gameId, '7001');
    assert.equal(b.settlement.gameId, '7001');
    assert.equal(b.settlement.settledAt, settledAt, 'never re-settled');
  } finally { await app.close(); }
});

test('a bound-but-unsettled bet whose game is no longer on the scoreboard settles from that exact game (loaded by event id)', async () => {
  const now = Date.now();
  const boundStart = now - 60 * H;
  const bound = makeEvent({ id: '5550', home: 'BOS', away: 'PHI', homeScore: 118, awayScore: 111, start: boundStart });
  const todays = makeEvent({ id: '5560', home: 'BOS', away: 'ATL', homeScore: 90, awayScore: 95, start: now - 2 * H });
  const seed = emptyState({ straightBets: [propBet({ id: 'p2', playerId: '123', createdAt: boundStart - 5 * H, gameId: '5550', gameDate: etDateString(boundStart), gameStart: new Date(boundStart).toISOString() })] });
  const feed = { events: [todays], summaries: { 5550: makeSummary(bound, [{ ...TATUM, pts: 27 }]), 5560: makeSummary(todays, [{ ...TATUM, pts: 2 }]) } };
  const app = await openApp({ seed, feed });
  try {
    assert.ok(app.requests.some(u => u.includes('summary?event=5550')), 'bound game fetched by id');
    assert.equal((await app.historyRows())[0].status, 'WON');
    const b = (await app.stored()).straightBets[0];
    assert.equal(b.settlement.gameId, '5550');
    assert.equal(b.settlement.current, 27);
  } finally { await app.close(); }
});

test('app team codes (GSW) match ESPN codes (GS) for props and team bets', async () => {
  const now = Date.now();
  const g = makeEvent({ id: '8001', home: 'GS', away: 'NY', homeScore: 120, awayScore: 100, start: now - 4 * H });
  const seed = emptyState({ straightBets: [
    propBet({ id: 'c1', player: 'Stephen Curry', team: 'GSW', target: 29.5, createdAt: now - 5 * H }),
    teamBet({ id: 'c2', teamAbbr: 'GSW', oppAbbr: 'NYK', marketType: 'spread', line: -5.5, direction: null, createdAt: now - 5 * H }),
  ] });
  const app = await openApp({ seed, feed: { events: [g], summaries: { 8001: makeSummary(g, [{ id: '9', name: 'Stephen Curry', team: 'GS', pts: 35 }]) } } });
  try {
    const rows = await app.historyRows();
    assert.deepEqual(rows.map(r => r.status), ['WON', 'WON']);
    const s = await app.stored();
    assert.deepEqual(s.straightBets.map(b => b.settlement?.gameId), ['8001', '8001']);
  } finally { await app.close(); }
});

test('editing a settled bet: a line change re-judges from the saved stat line; a player change clears the saved result', async () => {
  const now = Date.now();
  const g = makeEvent({ id: '6001', home: 'BOS', away: 'MIA', homeScore: 100, awayScore: 90, start: now - 4 * H });
  const feed = { events: [g], summaries: { 6001: makeSummary(g, [{ ...TATUM, pts: 30 }]) } };
  const app = await openApp({ seed: emptyState({ straightBets: [propBet({ id: 'e1', playerId: '123', createdAt: now - 5 * H })] }), feed });
  try {
    assert.equal((await app.historyRows())[0].status, 'WON');
    feed.events = []; feed.summaries = {}; // the saved evidence must be enough on its own
    await app.reload();
    await app.page.click('[data-tab="history"]');
    await app.page.click('[data-histedit^="straight:"]');
    await app.page.fill('#edit-f-target', '31.5');
    await app.page.click('#editModalSave');
    assert.equal((await app.historyRows())[0].status, 'LOST');
    let b = (await app.stored()).straightBets[0];
    assert.equal(b.settlement.status, 'lost'); assert.equal(b.settlement.gameId, '6001');
    await app.page.click('[data-histedit^="straight:"]');
    await app.page.fill('#edit-f-player', 'Jaylen Brown');
    await app.page.click('#editModalSave');
    b = (await app.stored()).straightBets[0];
    assert.equal(b.settlement, null); assert.equal(b.gameId, null); assert.equal(b.playerId, null);
    assert.equal((await app.historyRows())[0].status, 'Needs review');
    const audit = (await app.stored()).auditLog.filter(a => a.note === 'retroactive edit');
    assert.equal(audit.length, 2);
  } finally { await app.close(); }
});

test('ladder step linked to a bet settles once and keeps its accounting after the game leaves the feed', async () => {
  const now = Date.now();
  const g = makeEvent({ id: '4001', home: 'BOS', away: 'MIA', homeScore: 100, awayScore: 90, start: now - 4 * H });
  const feed = { events: [g], summaries: { 4001: makeSummary(g, [{ ...TATUM, pts: 30 }]) } };
  const seed = emptyState({
    straightBets: [propBet({ id: 'lb1', playerId: '123', createdAt: now - 5 * H, stake: 10, odds: '+150', ladderId: 'L1', ladderStepId: 'S1' })],
    ladders: [{ id: 'L1', name: 'Test ladder', createdAt: now - 5 * H, startingAmount: 10, goalAmount: 1000, progressionMode: 'manual', allOrNothing: true, status: 'open', notes: null, targetDate: null,
      steps: [{ id: 'S1', ticketType: 'straight', ticketId: 'lb1', stake: 10, americanOdds: '+150', enteredPayout: null, result: 'pending', createdAt: now - 5 * H, settledAt: null }] }],
  });
  const app = await openApp({ seed, feed });
  try {
    let step = (await app.stored()).ladders[0].steps[0];
    assert.equal(step.result, 'won'); assert.equal(step.netProfit, 15);
    feed.events = []; feed.summaries = {};
    await app.reload();
    const s = await app.stored();
    step = s.ladders[0].steps[0];
    assert.equal(step.result, 'won'); assert.equal(step.netProfit, 15);
    assert.equal(s.straightBets[0].settlement.status, 'won');
  } finally { await app.close(); }
});

test('History CSV export carries game id, ET game date, settled-at and result source', async () => {
  const now = Date.now();
  const g = makeEvent({ id: '3001', home: 'BOS', away: 'MIA', homeScore: 100, awayScore: 90, start: now - 4 * H });
  const app = await openApp({ seed: emptyState({ straightBets: [propBet({ id: 'x1', playerId: '123', createdAt: now - 5 * H })] }), feed: { events: [g], summaries: { 3001: makeSummary(g, [{ ...TATUM, pts: 30 }]) } } });
  try {
    await app.page.click('[data-tab="history"]');
    const [dl] = await Promise.all([app.page.waitForEvent('download'), app.page.click('#histExportBtn')]);
    const csv = require('fs').readFileSync(await dl.path(), 'utf8');
    const [header, row] = csv.split('\n');
    assert.match(header, /"?Game ID"?,"?Game date \(ET\)"?,"?Settled at"?,"?Result source"?/);
    assert.ok(row.includes('"3001"'));
    assert.ok(row.includes(`"${etDateString(Date.parse(g.date))}"`));
    assert.ok(row.includes('"auto (final box score)"'));
    assert.ok(row.includes('"won"'));
  } finally { await app.close(); }
});
