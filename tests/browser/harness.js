// Shared Playwright harness for browser tests: opens the REAL app (index.html, file://)
// in Chromium with ESPN's network endpoints answered by a fake, test-controlled feed.
// No real network access is used or needed — every ESPN response here is SYNTHETIC
// test data built by the helpers below, never real game data.
const { chromium } = require('playwright');
const path = require('path');
const http = require('http');
const fs = require('fs');
const { etDateParam, etDateString } = require('../../game-binding.js');

const ROOT = path.join(__dirname, '..', '..');
// The app is served over a throwaway local HTTP server (like its real HTTPS host), not
// file:// — Chromium can drop localStorage writes made just before a reload of a file://
// page, which made reload-persistence tests nondeterministic.
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.png': 'image/png', '.md': 'text/markdown' };
function startStaticServer() {
  return new Promise(resolve => {
    const server = http.createServer((req, res) => {
      const rel = decodeURIComponent(new URL(req.url, 'http://x').pathname).replace(/^\/+/, '') || 'index.html';
      const file = path.join(ROOT, rel);
      if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) { res.writeHead(404); return res.end(); }
      res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
      fs.createReadStream(file).pipe(res);
    });
    server.listen(0, '127.0.0.1', () => resolve(server));
  });
}
const STORAGE_KEY = 'nbaCC.v5';
const H = 3600e3, D = 24 * H;
const BOX_LABELS = ['MIN', 'FG', '3PT', 'FT', 'REB', 'AST', 'STL', 'BLK', 'TO', 'PF', 'PTS'];

// A synthetic ESPN scoreboard event.
function makeEvent({ id, home, away, homeScore = 0, awayScore = 0, start, state = 'post', completed }) {
  const done = completed ?? (state === 'post');
  return {
    id: String(id), date: new Date(start).toISOString(),
    status: { type: { state, completed: done, name: done ? 'STATUS_FINAL' : 'STATUS_SCHEDULED', description: done ? 'Final' : (state === 'in' ? 'In Progress' : 'Scheduled') }, period: state === 'pre' ? 0 : 4, displayClock: '0:00' },
    competitions: [{ competitors: [
      { homeAway: 'home', team: { abbreviation: home }, score: String(homeScore) },
      { homeAway: 'away', team: { abbreviation: away }, score: String(awayScore) },
    ] }],
  };
}
// A synthetic ESPN per-event summary (header + box score) for an event.
// players: [{ id, name, team, pts, reb, ast, min, starter }]
function makeSummary(ev, players = []) {
  const byTeam = new Map();
  for (const p of players) { if (!byTeam.has(p.team)) byTeam.set(p.team, []); byTeam.get(p.team).push(p); }
  return {
    header: { id: ev.id, competitions: [{ date: ev.date, competitors: ev.competitions[0].competitors, status: ev.status }] },
    boxscore: { players: [...byTeam.entries()].map(([team, ps]) => ({
      team: { abbreviation: team },
      statistics: [{ labels: BOX_LABELS, athletes: ps.map(p => ({
        athlete: { id: String(p.id), displayName: p.name }, starter: p.starter ?? true,
        stats: [String(p.min ?? 30), '5-10', '1-3', '2-2', String(p.reb ?? 0), String(p.ast ?? 0), '0', '0', '0', '0', String(p.pts ?? 0)],
      })) }],
    })) },
  };
}

// feed = { events: [...], summaries: { [id]: summary } } — mutable between reloads.
async function openApp({ seed = null, feed = { events: [], summaries: {} } } = {}) {
  const server = await startStaticServer();
  const APP_URL = `http://127.0.0.1:${server.address().port}/index.html`;
  const browser = await chromium.launch();
  // Service workers blocked: a SW would sit between the page and Playwright's routing.
  const context = await browser.newContext({ acceptDownloads: true, serviceWorkers: 'block' });
  const page = await context.newPage();
  const requests = [];
  const cdnRequests = [];
  page.on('dialog', d => d.accept());
  await page.route('**/site.api.espn.com/**', route => {
    const url = route.request().url();
    requests.push(url);
    if (url.includes('/scoreboard')) {
      const date = (url.match(/dates=(\d{8})/) || [])[1];
      const events = feed.events.filter(ev => !date || etDateParam(Date.parse(ev.date), 0) === date);
      return route.fulfill({ json: { events } });
    }
    if (url.includes('/summary')) {
      const id = decodeURIComponent((url.match(/event=([^&]+)/) || [])[1] || '');
      const s = feed.summaries[id];
      return s ? route.fulfill({ json: s }) : route.fulfill({ status: 404, json: {} });
    }
    return route.fulfill({ json: { athletes: [] } }); // rosters
  });
  await page.route('**/a.espncdn.com/**', r => r.fulfill({ status: 404, body: '' }));
  await page.route('**/cdn.jsdelivr.net/**', r => { cdnRequests.push(r.request().url()); return r.abort(); });
  if (seed) {
    // Seed localStorage ONCE per browser context, so a reload tests what the app itself
    // persisted rather than re-seeding.
    await page.addInitScript(([key, json]) => {
      if (!localStorage.getItem('__testSeeded')) { localStorage.setItem(key, json); localStorage.setItem('__testSeeded', '1'); }
    }, [STORAGE_KEY, JSON.stringify(seed)]);
  }
  await page.goto(APP_URL);
  await waitForRefresh(page);
  return {
    browser, context, page, feed, requests, cdnRequests,
    stored: () => page.evaluate(k => JSON.parse(localStorage.getItem(k)), STORAGE_KEY),
    reload: async () => { await page.reload(); await waitForRefresh(page); },
    refresh: async () => { await page.click('#refreshBtn'); await waitForRefresh(page); },
    historyRows: () => page.$$eval('#historyList .history-row', els => els.map(e => ({ label: e.querySelector('.hr-main div')?.textContent.trim(), status: e.querySelector('.pill')?.textContent.trim() }))),
    close: async () => { await browser.close(); await new Promise(r => server.close(r)); },
    url: APP_URL,
  };
}
async function waitForRefresh(page) {
  await page.waitForFunction(() => /^Updated /.test(document.getElementById('lastUpdate')?.textContent || '') && !document.getElementById('refreshBtn').disabled, null, { timeout: 15000 });
}

function emptyState(extra = {}) {
  return { schemaVersion: 5, straightBets: [], parlays: [], ladders: [], auditLog: [], oddsHistory: {}, ...extra };
}
function propBet(extra = {}) {
  return { id: 'b-' + Math.random().toString(36).slice(2), kind: 'prop', player: 'Jayson Tatum', team: 'BOS', opponent: null, market: 'PTS', target: 25.5, direction: 'over', odds: '-110', stake: 10, bookmaker: null, game: null, voided: false, playerId: null, createdAt: Date.now(), finalizedAt: null, manualStatus: null, ladderId: null, ladderStepId: null, ...extra };
}
function teamBet(extra = {}) {
  return { id: 't-' + Math.random().toString(36).slice(2), kind: 'team', marketType: 'gameTotal', teamAbbr: 'BOS', oppAbbr: null, line: 205.5, direction: 'over', odds: '-110', stake: 10, bookmaker: null, game: null, voided: false, createdAt: Date.now(), finalizedAt: null, manualStatus: null, ladderId: null, ladderStepId: null, ...extra };
}

module.exports = { openApp, makeEvent, makeSummary, emptyState, propBet, teamBet, etDateString, H, D, STORAGE_KEY };
