// End-to-end checks that the tracker's existing features still work after v6.1
// (SYNTHETIC mocked ESPN feed; real app in a real browser).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const { openApp, makeEvent, makeSummary, emptyState, propBet, H, STORAGE_KEY } = require('./harness.js');

test('v6.0 (schema 4) data migrates to schema 5 without losing anything', async () => {
  const now = Date.now();
  const v6 = {
    schemaVersion: 4, auditLog: [{ ts: 1, ticketType: 'straight', ticketId: 'a', from: 'computed', to: 'won', note: 'manual correction via Bet History' }], oddsHistory: {},
    straightBets: [{ id: 'a', kind: 'prop', player: 'Old Player', team: 'BOS', market: 'PTS', target: 10, direction: 'over', createdAt: now - 10 * 24 * H, odds: '-110', stake: 5, voided: false, manualStatus: 'won', playerId: null, finalizedAt: now - 9 * 24 * H, ladderId: null, ladderStepId: null }],
    parlays: [{ id: 'p', name: 'Old parlay', createdAt: now - 10 * 24 * H, stake: 2, legs: [{ legId: 'l', kind: 'team', marketType: 'gameTotal', teamAbbr: 'BOS', line: 200, direction: 'over', odds: null, voided: false }] }],
    ladders: [],
  };
  const app = await openApp({ seed: v6 });
  try {
    const s = await app.stored();
    assert.equal(s.schemaVersion, 5);
    assert.equal(s.straightBets.length, 1); assert.equal(s.parlays.length, 1); assert.equal(s.auditLog.length, 1);
    const b = s.straightBets[0];
    assert.equal(b.manualStatus, 'won', 'manual corrections preserved');
    for (const k of ['gameId', 'gameDate', 'gameStart', 'settlement', 'gameDateHint']) assert.ok(k in b, `${k} backfilled`);
    assert.equal(s.parlays[0].legs[0].createdAt, v6.parlays[0].createdAt);
    assert.equal((await app.historyRows()).find(r => r.label.includes('Old Player')).status, 'WON');
  } finally { await app.close(); }
});

test('adding a straight prop and a parlay through the forms still works; new bets bind to tonight\'s game', async () => {
  const now = Date.now();
  const tonight = makeEvent({ id: '2001', home: 'BOS', away: 'MIA', start: now + 2 * H, state: 'pre' });
  const app = await openApp({ feed: { events: [tonight], summaries: {} } });
  const { page } = app;
  try {
    await page.click('[data-tab="straight"]');
    await page.fill('#s-player', 'Jayson Tatum');
    await page.fill('#s-team', 'BOS');
    await page.fill('#s-target', '25.5');
    await page.fill('#s-odds', '-110');
    await page.fill('#s-stake', '10');
    await page.click('#straightForm button[type="submit"]');
    await page.waitForFunction(k => (JSON.parse(localStorage.getItem(k)).straightBets[0] || {}).gameId === '2001', STORAGE_KEY, { timeout: 10000 });
    let s = await app.stored();
    assert.equal(s.straightBets.length, 1);
    assert.equal(s.straightBets[0].settlement, null, 'pre-game: bound, not settled');

    await page.click('[data-tab="parlays"]');
    await page.fill('#p-name', 'Test parlay');
    for (const [player, target] of [['Jayson Tatum', '20'], ['Jaylen Brown', '15']]) {
      await page.fill('#l-player', player); await page.fill('#l-team', 'BOS'); await page.fill('#l-target', target);
      await page.click('#addLegBtn');
    }
    await page.fill('#p-stake', '5');
    await page.click('#createParlayBtn');
    s = await app.stored();
    assert.equal(s.parlays.length, 1);
    assert.equal(s.parlays[0].legs.length, 2);
    for (const l of s.parlays[0].legs) assert.equal(l.createdAt, s.parlays[0].createdAt);
  } finally { await app.close(); }
});

test('backup export includes settlement fields; re-importing it with Merge creates no duplicates; integrity check is clean', async () => {
  const now = Date.now();
  const g = makeEvent({ id: '1001', home: 'BOS', away: 'MIA', homeScore: 100, awayScore: 90, start: now - 4 * H });
  const app = await openApp({ seed: emptyState({ straightBets: [propBet({ id: 'z1', playerId: '123', createdAt: now - 5 * H })] }), feed: { events: [g], summaries: { 1001: makeSummary(g, [{ id: '123', name: 'Jayson Tatum', team: 'BOS', pts: 30 }]) } } });
  const { page } = app;
  try {
    const [dl] = await Promise.all([page.waitForEvent('download'), page.click('#exportBtn')]);
    const file = await dl.path();
    const backup = JSON.parse(fs.readFileSync(file, 'utf8'));
    assert.equal(backup.straightBets[0].settlement.status, 'won');
    assert.equal(backup.straightBets[0].settlement.gameId, '1001');
    await page.setInputFiles('#importFile', { name: 'backup.json', mimeType: 'application/json', buffer: fs.readFileSync(file) });
    await page.click('#importMergeBtn');
    const s = await app.stored();
    assert.equal(s.straightBets.length, 1, 'no duplicates after merge');
    assert.equal(s.straightBets[0].settlement.status, 'won');
    await page.click('[data-tab="settings"]');
    await page.click('#settingsIntegrityBtn');
    assert.match(await page.textContent('#settingsIntegrityResult'), /No problems found/);
  } finally { await app.close(); }
});

test('Game Center still shows recent games and a finished game\'s full box score', async () => {
  const now = Date.now();
  const g = makeEvent({ id: '1501', home: 'DEN', away: 'LAL', homeScore: 121, awayScore: 115, start: now - 5 * H });
  const app = await openApp({ feed: { events: [g], summaries: { 1501: makeSummary(g, [{ id: '1', name: 'Nikola Jokic', team: 'DEN', pts: 31, reb: 12, ast: 9 }]) } } });
  try {
    const text = await app.page.textContent('#ov-games');
    assert.match(text, /DEN/); assert.match(text, /LAL/);
    await app.page.click('[data-gccard="1501"]');
    await app.page.waitForFunction(() => /Nikola Jokic/.test(document.getElementById('ov-games').textContent), null, { timeout: 5000 });
  } finally { await app.close(); }
});

test('OCR: the pinned, vendored tesseract.js bundle loads without any CDN request for the library itself', async () => {
  const app = await openApp();
  try {
    await app.page.evaluate(() => loadTesseract());
    assert.equal(await app.page.evaluate(() => typeof window.Tesseract.recognize), 'function');
    assert.deepEqual(app.cdnRequests, [], 'no jsDelivr request just to load the library');
    const src = await app.page.evaluate(() => [...document.scripts].map(s => s.getAttribute('src')).find(s => /tesseract/.test(s || '')));
    assert.equal(src, 'vendor/tesseract.js-5.1.1/tesseract.min.js');
  } finally { await app.close(); }
});
