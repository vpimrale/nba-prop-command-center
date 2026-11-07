// Browser checks for documented v6 behaviors: save status, offline banner, safe History
// removal, standardized status colors, and team autocomplete aliases.
const test = require('node:test');
const assert = require('node:assert/strict');
const { openApp, makeEvent, makeSummary, emptyState, propBet, H } = require('./harness.js');

function settledRecord(status, gameId, startMs, pts) {
  return { status, current: pts, gameId, gameDate: null, gameStart: new Date(startMs).toISOString(), settledAt: startMs + 3 * H, source: 'auto-final-boxscore',
    snapshot: { home: 'BOS', away: 'MIA', homeScore: '100', awayScore: '90', athlete: { espnId: '123', name: 'Jayson Tatum', team: 'BOS', minutes: 36, stats: { PTS: pts } } } };
}

test('save-status indicator reports a real local save', async () => {
  const app = await openApp();
  try { assert.match(await app.page.textContent('#saveStatus'), /^Saved locally/); } finally { await app.close(); }
});

test('offline banner follows the browser connectivity signal', async () => {
  const app = await openApp();
  try {
    assert.equal(await app.page.$eval('#offlineBanner', el => el.classList.contains('hide')), true);
    await app.context.setOffline(true);
    await app.page.waitForFunction(() => !document.getElementById('offlineBanner').classList.contains('hide'));
    await app.context.setOffline(false);
    await app.page.waitForFunction(() => document.getElementById('offlineBanner').classList.contains('hide'));
  } finally { await app.close(); }
});

test('History: only archived (settled + 30 min) tickets can be removed; active ones never get a checkbox; ladder accounting untouched', async () => {
  const now = Date.now();
  const start = now - 6 * H;
  const archived = propBet({ id: 'arch', createdAt: start - H, gameId: 'g1', settlement: settledRecord('won', 'g1', start, 30), finalizedAt: now - 2 * H, ladderId: 'L1', ladderStepId: 'S1', odds: '+100' });
  const active = propBet({ id: 'act', createdAt: now - H });
  const seed = emptyState({ straightBets: [archived, active], ladders: [{ id: 'L1', name: 'L', createdAt: start, startingAmount: 10, goalAmount: 1000, progressionMode: 'manual', allOrNothing: true, status: 'open', notes: null, targetDate: null,
    steps: [{ id: 'S1', ticketType: 'straight', ticketId: 'arch', stake: 10, americanOdds: '+100', enteredPayout: null, result: 'won', grossPayout: 20, netProfit: 10, estimated: true, createdAt: start, settledAt: start + 3 * H }] }] });
  const app = await openApp({ seed });
  const { page } = app;
  try {
    await page.click('[data-tab="history"]');
    assert.equal(await page.$$eval('[data-histcheck]', els => els.length), 1);
    assert.equal(await page.$eval('[data-histcheck]', el => el.dataset.histcheck), 'straight:arch');
    await page.check('[data-histcheck]');
    await page.click('#histRemoveSelectedBtn');
    const s = await app.stored();
    assert.deepEqual(s.straightBets.map(b => b.id), ['act']);
    assert.equal(s.ladders[0].steps[0].result, 'won');
    assert.equal(s.ladders[0].steps[0].netProfit, 10);
    assert.match(s.ladders[0].steps[0].ticketLabelSnapshot, /Jayson Tatum/);
  } finally { await app.close(); }
});

test('status colors: confirmed WON is green (hit), LOST is red (miss), live target-reached is its own class', async () => {
  const now = Date.now();
  const live = makeEvent({ id: 'live1', home: 'BOS', away: 'MIA', homeScore: 50, awayScore: 48, start: now - H, state: 'in' });
  const seed = emptyState({ straightBets: [
    propBet({ id: 'w', createdAt: now - 30 * H, gameId: 'gw', settlement: settledRecord('won', 'gw', now - 28 * H, 30) }),
    propBet({ id: 'l', createdAt: now - 30 * H, target: 35.5, gameId: 'gl', settlement: settledRecord('lost', 'gl', now - 28 * H, 30) }),
    propBet({ id: 'r', createdAt: now - 2 * H, target: 10.5, playerId: '123' }),
  ] });
  const app = await openApp({ seed, feed: { events: [live], summaries: { live1: makeSummary(live, [{ id: '123', name: 'Jayson Tatum', team: 'BOS', pts: 14 }]) } } });
  try {
    await app.page.click('[data-tab="history"]');
    const pills = await app.page.$$eval('#historyList .pill', els => els.map(e => ({ cls: e.className, txt: e.textContent.trim() })));
    const byTxt = t => pills.find(p => p.txt === t);
    assert.match(byTxt('WON').cls, /\bhit\b/);
    assert.match(byTxt('LOST').cls, /\bmiss\b/);
    const live1 = byTxt('TARGET REACHED — LIVE');
    assert.ok(live1, 'live target reached shown');
    assert.ok(live1.cls.split(/\s+/).includes('live-hit'));
    assert.ok(!live1.cls.split(/\s+/).includes('hit'), 'never the confirmed-win class while live');
    const css = await app.page.evaluate(() => [getComputedStyle(document.querySelector('.pill.hit')).color, getComputedStyle(document.querySelector('.pill.live-hit')).color]);
    assert.notEqual(css[0], css[1]);
  } finally { await app.close(); }
});

test('team autocomplete resolves nickname / city / alias to the team code', async () => {
  const app = await openApp();
  const { page } = app;
  try {
    await page.click('[data-tab="straight"]');
    await page.click('[data-bettype="team"]');
    for (const [q, abbr] of [['Warriors', 'GSW'], ['Golden State', 'GSW'], ['Knicks', 'NYK'], ['Lakers', 'LAL']]) {
      await page.fill('#t-team', '');
      await page.type('#t-team', q);
      await page.waitForSelector('.autocomplete-item');
      await page.dispatchEvent('.autocomplete-item >> nth=0', 'mousedown');
      assert.equal(await page.inputValue('#t-team'), abbr, q);
    }
  } finally { await app.close(); }
});
