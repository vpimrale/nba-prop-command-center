'use strict';
/* global NBA_TEAMS, parseBetSlipText, evaluateTeamBet, evaluateParlay, evaluatePropLeg, componentsForMarket, marketLabel, rawStat,
   americanOddsToDecimal, estimateGrossPayout, settleStep, computeLadderBalance, totalRealizedPL, amountNeededForGoal,
   progressPct, suggestedNextStake, deriveLadderStatus, ARCHIVE_DELAY_MS, computeFinalizedAt, isArchived, minutesRemaining,
   canonicalTeam, sameTeam, etDateString, etDateParam, gameFromScoreboardEvent, gameFromSummary, gameInvolves,
   candidateGamesForSelection, bindingFor, shouldPersistSettlement, buildSettlement, evaluateFromSettlement,
   identityChanged, clearBinding */

const API_SCOREBOARD = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard';
const API_SUMMARY = 'https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event=';
const API_ROSTER = abbr => `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/${abbr.toLowerCase()}/roster`;
// ESPN's scoreboard endpoint defaults to one calendar day ("today"), so a game that
// finished late last night can silently vanish from Game Center the moment the date
// rolls over, even though it's well within the last 24 real hours. To cover "games
// finished in the past 24 hours" properly, every refresh fetches TWO calendar days —
// today's and yesterday's, via ESPN's own `dates=YYYYMMDD` param — and merges them.
// v6.1: the calendar day is the US-EASTERN day (the NBA's/ESPN's schedule day), not
// this device's local day — a West-Coast device late at night previously asked for
// the wrong day (see game-binding.js etDateParam()).
const scoreboardUrl = offsetDays => `${API_SCOREBOARD}?dates=${etDateParam(Date.now(), offsetDays)}`;
// A finished game has no explicit "ended at" timestamp in this feed, only a start
// time + status. 30 hours past tipoff is used as a generous stand-in for "already
// over and well past the 24-hour window we care about" (an NBA game plus any
// realistic delay is always well under that), so this intentionally keeps a few
// extra hours of margin rather than risk dropping a game that actually finished
// within the last real 24 hours.
const RECENT_FINAL_WINDOW_MS = 30 * 60 * 60 * 1000;
const STORAGE_KEY = 'nbaCC.v5';
const LEGACY_STORAGE_KEYS = ['nbaCC.v4', 'nbaCC.v3']; // read-only fallback for migration, newest first
const SCHEMA_VERSION = 5; // v6.1: + per-selection game binding (gameId/gameDate/gameStart) + persisted settlement
const APP_VERSION = 'v6.1.0';
const ROSTER_KEY = 'nbaCC.rosterCache.v2';
const ROSTER_MAX_AGE_MS = 12 * 60 * 60 * 1000; // 12 hours

const $ = id => document.getElementById(id);
const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now() + Math.random()));
const fmt = n => (n === null || n === undefined || isNaN(n)) ? '—' : (Number.isInteger(n) ? String(n) : n.toFixed(1));

// ---------------------------------------------------------------------------
// Persistent state + schema migration
// ---------------------------------------------------------------------------
// v4 data model (shared by straight bets AND parlay legs — see README):
//   prop selection:  { kind:'prop', player, team, opponent, market, target, direction,
//                       odds, voided, createdAt, ...(straight-only: id, stake, bookmaker, game) }
//   team selection:  { kind:'team', marketType, teamAbbr, oppAbbr, line, direction,
//                       odds, voided, createdAt, ...(straight-only: id, stake, bookmaker, game) }
//   parlay:          { id, name, legs:[{legId, ...selection fields}], combinedOdds, stake,
//                       potentialPayout, boost, createdAt }
// Every selection (straight or leg) is evaluated by the SAME evaluateSelection() function
// below, which is the direct fix for the reported bug: a parlay leg now gets the exact
// same prop-status.js / settlement.js evaluation, and the exact same rich card renderer,
// as a straight bet. There is no second, thinner code path anywhere in this file.
function migrateSelection(sel) {
  // Backfill fields that may be missing on records created by older versions.
  if (sel.voided === undefined) sel.voided = false;
  if (sel.odds === undefined) sel.odds = null;
  if (sel.ladderId === undefined) sel.ladderId = null;       // v5: ladder link (straight bets + parlays only, not legs)
  if (sel.ladderStepId === undefined) sel.ladderStepId = null;
  if (sel.finalizedAt === undefined) sel.finalizedAt = null;  // v5: persisted 30-min-archival timestamp
  if (sel.manualStatus === undefined) sel.manualStatus = null; // v5: manual settlement correction override
  if (sel.playerId === undefined) sel.playerId = null; // v6: canonical ESPN athlete id when resolved via autocomplete
  migrateBindingFields(sel);
  return sel;
}
// v6.1 (schema 5): every selection — straight bet OR parlay leg — carries its own game
// binding and, once final, its own persisted settlement (see game-binding.js). Records
// from older versions get empty bindings: they are matched afresh, but ONLY against a
// game inside the plausibility window around their createdAt, never a newer game.
function migrateBindingFields(sel) {
  if (sel.gameId === undefined) sel.gameId = null;
  if (sel.gameDate === undefined) sel.gameDate = null;
  if (sel.gameStart === undefined) sel.gameStart = null;
  if (sel.boundAt === undefined) sel.boundAt = null;
  if (sel.settlement === undefined) sel.settlement = null;
  if (sel.gameDateHint === undefined) sel.gameDateHint = null; // optional user-supplied ET game date (YYYY-MM-DD)
  return sel;
}
function migrateLeg(l, parentCreatedAt) {
  l.voided = l.voided ?? false; l.odds = l.odds ?? null; l.playerId = l.playerId ?? null;
  // A leg's plausibility window is anchored to when its ticket was recorded.
  if (l.createdAt === undefined || l.createdAt === null) l.createdAt = parentCreatedAt ?? null;
  return migrateBindingFields(l);
}
function migrateLadder(l) {
  if (l.allOrNothing === undefined) l.allOrNothing = true;
  if (l.notes === undefined) l.notes = null;
  if (l.targetDate === undefined) l.targetDate = null;
  if (l.steps === undefined) l.steps = [];
  return l;
}
// Turns a raw parsed JSON blob (however it got here — the current storage key, an older
// legacy key, an IndexedDB backup copy, or an imported backup file) into a fully-migrated,
// schema-current state object. Pulled out as its own function (rather than inlined in
// loadState()) so the IndexedDB recovery path below can run the exact same migration logic
// a normal boot does, instead of a second, easier-to-drift-out-of-sync copy of it.
function buildStateFromRaw(raw) {
  if (!raw) return { schemaVersion: SCHEMA_VERSION, straightBets: [], parlays: [], ladders: [], auditLog: [], oddsHistory: {} };
  const straightBets = (raw.straightBets || []).map(migrateSelection);
  // Only straight bets and whole parlay tickets carry a ladder link / finalizedAt / manualStatus — legs don't
  // (a ladder step is linked to the ticket, never to one leg of a parlay).
  const parlays = (raw.parlays || []).map(p => migrateSelection({ ...p, legs: (p.legs || []).map(l => migrateLeg(l, p.createdAt)) }));
  const ladders = (raw.ladders || []).map(migrateLadder);
  return { schemaVersion: SCHEMA_VERSION, straightBets, parlays, ladders, auditLog: raw.auditLog || [], oddsHistory: raw.oddsHistory || {} };
}
function loadState() {
  let raw = null;
  try { raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null'); } catch (e) { raw = null; }
  if (!raw) {
    // No v5 record yet — look for a v4 or v3 record to migrate forward rather than starting empty.
    for (const key of LEGACY_STORAGE_KEYS) {
      try {
        const legacy = JSON.parse(localStorage.getItem(key) || 'null');
        if (legacy && (Array.isArray(legacy.straightBets) || Array.isArray(legacy.parlays))) { raw = legacy; break; }
      } catch (e) { /* ignore and keep looking */ }
    }
  }
  return buildStateFromRaw(raw);
}
let state = loadState();

// ---------------------------------------------------------------------------
// Save-status indicator + a best-effort IndexedDB mirror.
//
// localStorage.setItem is synchronous, so the UI below reflects its REAL,
// already-known outcome — it never claims "Saved" before the write actually
// succeeded. A quota error (storage full) or a write blocked by the browser
// (e.g. private-browsing storage restrictions in some browsers) is shown to
// the user rather than swallowed silently, per the "never silently lose data"
// requirement. IndexedDB is used here only as a secondary, best-effort backup
// mirror of the exact same data already saved to localStorage — not a second
// source of truth and not a sync mechanism — so a localStorage quota problem
// or accidental clear has a second local copy to recover from on this same
// device/browser. It is still local-only: nothing here reaches a network or
// another device.
let lastSaveAt = null;
let lastSaveError = null;
function updateSaveStatusUI(kind, note) {
  const dot = $('saveDot'); const label = $('saveStatus');
  if (!dot || !label) return;
  dot.className = 'dot' + (kind === 'failed' ? ' bad' : kind === 'saving' ? ' warn' : '');
  if (kind === 'saving') { label.textContent = 'Saving…'; return; }
  if (kind === 'failed') {
    label.textContent = 'Save failed — ' + (lastSaveError || 'unknown error') + '. Your latest change may not be saved — try freeing up space or exporting a backup.';
    return;
  }
  const when = lastSaveAt ? new Date(lastSaveAt).toLocaleTimeString() : '';
  label.textContent = note || ('Saved locally' + (when ? ' · ' + when : ''));
}
const IDB_NAME = 'nbaCC-backup', IDB_STORE = 'state', IDB_KEY = 'latest';
function withIndexedDB(cb) {
  if (typeof indexedDB === 'undefined') return;
  try {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = () => { try { req.result.createObjectStore(IDB_STORE); } catch (e) { /* already exists */ } };
    req.onsuccess = () => { try { cb(req.result); } catch (e) { /* best-effort only */ } };
    req.onerror = () => { /* best-effort only — localStorage remains the real save */ };
  } catch (e) { /* best-effort only */ }
}
function mirrorStateToIndexedDB() {
  withIndexedDB(db => {
    try {
      const tx = db.transaction(IDB_STORE, 'readwrite');
      tx.objectStore(IDB_STORE).put({ json: JSON.stringify(state), savedAt: Date.now() }, IDB_KEY);
    } catch (e) { /* best-effort only */ }
  });
}
// Runs once at boot. Only acts if THIS device's localStorage looks genuinely empty (no
// bets, parlays, or ladders at all) — never overwrites data that is already there, and
// never runs again after the first check. If an IndexedDB backup copy exists and has
// real records in it, that almost certainly means localStorage was wiped or hit a quota
// problem on a previous save; recovering it is strictly safer than silently starting over.
function tryRecoverFromIndexedDB() {
  if (state.straightBets.length || state.parlays.length || state.ladders.length) return;
  withIndexedDB(db => {
    try {
      const tx = db.transaction(IDB_STORE, 'readonly');
      const req = tx.objectStore(IDB_STORE).get(IDB_KEY);
      req.onsuccess = () => {
        const rec = req.result;
        if (!rec || !rec.json) return;
        let parsed = null;
        try { parsed = JSON.parse(rec.json); } catch (e) { return; }
        if (!parsed || !((parsed.straightBets || []).length || (parsed.parlays || []).length || (parsed.ladders || []).length)) return;
        state = buildStateFromRaw(parsed);
        saveState();
        if (typeof renderAll === 'function') renderAll();
        updateSaveStatusUI('saved', 'Recovered a local backup copy — your localStorage save looked empty, but a saved IndexedDB copy on this device had data.');
      };
    } catch (e) { /* best-effort only */ }
  });
}
function saveState() {
  updateSaveStatusUI('saving');
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    lastSaveAt = Date.now();
    lastSaveError = null;
    updateSaveStatusUI('saved');
    mirrorStateToIndexedDB();
    return true;
  } catch (e) {
    const isQuota = e && (e.name === 'QuotaExceededError' || e.code === 22 || /quota/i.test(e.message || ''));
    lastSaveError = isQuota ? "your browser's local storage is full" : ((e && e.message) || 'an unknown error occurred');
    updateSaveStatusUI('failed');
    return false;
  }
}
saveState(); // persist a v3/v4->v5 migration immediately, so the old key is never the only copy

// ---------------------------------------------------------------------------
// Live data: scoreboard + box scores (per-game, fetched only when needed)
// ---------------------------------------------------------------------------
let games = [];            // [{id, home, away, homeScore, awayScore, state, period, clock, startTime, players:[], teams:[]}]
let busy = false;
let everSucceeded = false;
let lastSuccessAt = null;

async function fetchJson(url) {
  const r = await fetch(url, { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

// Shooting-stat labels ESPN's box score commonly uses as "made-attempted" strings
// (e.g. "10-15"), as opposed to plain counting stats. Kept as a small lookup so
// Game Center can show "10/15 FG" instead of just the makes.
const SHOOTING_LABELS = { FG: 'FG', '3PT': '3P', FT: 'FT' };
function extractAthletes(summary) {
  const out = [];
  const teams = summary?.boxscore?.players || [];
  for (const team of teams) {
    const teamAbbr = team?.team?.abbreviation || team?.team?.shortDisplayName || '';
    for (const group of (team?.statistics || [])) {
      const labels = (group?.labels || group?.names || []).map(x => String(x).toUpperCase());
      for (const athlete of (group?.athletes || [])) {
        const a = athlete?.athlete || athlete;
        const statsArr = athlete?.stats || [];
        const vals = {};
        const shooting = {};
        labels.forEach((label, i) => {
          const raw = statsArr[i];
          if (typeof raw === 'string' && raw.includes('-')) {
            const [made, att] = raw.split('-').map(n => Number(n) || 0);
            vals[label] = made; // prop-status.js/settlement.js only ever need the made count
            if (SHOOTING_LABELS[label]) shooting[SHOOTING_LABELS[label]] = { m: made, a: att };
          } else {
            vals[label] = Number(raw) || 0;
          }
        });
        const name = a?.displayName || a?.fullName || athlete?.displayName || '';
        const espnId = a?.id || null;
        // "Did not play" detection: ESPN sometimes gives a `reason` (DNP-CD, etc.) with
        // no stats line at all. This is a verifiable "played vs. didn't" split based on
        // MIN>0 or an explicit reason — a box-score fact, not a live-tracking claim.
        const minutes = vals.MIN ?? 0;
        const dnpReason = athlete?.reason || (athlete?.didNotPlay ? 'DNP' : null);
        // "starter" is ESPN's own box-score designation of who started the game — a
        // historical/roster fact recorded at tipoff, NOT a live "currently on the floor"
        // claim. Only trust it when the feed actually provides it; otherwise leave it
        // undefined rather than guessing, so the UI can fall back to one plain list.
        const starter = typeof athlete?.starter === 'boolean' ? athlete.starter : undefined;
        if (name) out.push({ name, team: teamAbbr, stats: vals, shooting, espnId, minutes, dnpReason, starter });
      }
    }
  }
  return out;
}
// Team-level box score totals, derived by summing the player rows we already have
// (ESPN's team-level summary block has an inconsistent shape across seasons, so
// summing verified per-player numbers is the more reliable path here).
function teamTotals(players) {
  const sum = (k) => players.reduce((s, p) => s + (Number(p.stats[k]) || 0), 0);
  const sumShoot = (k) => players.reduce((acc, p) => {
    const s = p.shooting[k];
    if (!s) return acc;
    return { m: acc.m + s.m, a: acc.a + s.a };
  }, { m: 0, a: 0 });
  return {
    PTS: sum('PTS'), REB: sum('REB'), AST: sum('AST'), STL: sum('STL'), BLK: sum('BLK'), TO: sum('TO'), PF: sum('PF'),
    FG: sumShoot('FG'), '3P': sumShoot('3P'), FT: sumShoot('FT'),
  };
}

function needsBoxScoreForTrackedBet(gameEventId, homeAbbr, awayAbbr) {
  // True if some tracked, NOT-YET-SETTLED player prop could belong to this game. A prop
  // whose result is already persisted (v6.1) never needs a live box score again, and a
  // prop bound to a different game id can't belong to this one. Team codes are compared
  // canonically, so an app code ("GSW") matches ESPN's ("GS").
  const propBets = [
    ...state.straightBets.filter(b => b.kind === 'prop'),
    ...state.parlays.flatMap(p => p.legs.filter(l => l.kind === 'prop')),
  ].filter(p => !p.voided && !p.settlement);
  return propBets.some(p => {
    if (p.gameId) return String(p.gameId) === String(gameEventId);
    if (!p.team) return true; // no team given — can't pre-filter, so check everywhere
    return sameTeam(p.team, homeAbbr) || sameTeam(p.team, awayAbbr);
  });
}
// A game's box score is fetched when EITHER a tracked prop bet could belong to it
// (the original request-saving rule, still applied to live/upcoming games) OR the
// game is already final and within the last ~24-30 hours — Game Center always shows
// a complete box score (every player, every team's totals) for any recently-finished
// game, regardless of whether you have a bet tracked on it.
function needsBoxScore(gameEventId, homeAbbr, awayAbbr, state_, startTimestamp) {
  if (needsBoxScoreForTrackedBet(gameEventId, homeAbbr, awayAbbr)) return true;
  if (state_ === 'post' && startTimestamp && (Date.now() - startTimestamp) <= RECENT_FINAL_WINDOW_MS) return true;
  return false;
}
// Finished games never change again, so once a final game's box score has been
// fetched this session it's kept here and reused on every later 15-second refresh
// tick instead of being re-downloaded for no reason — this is what keeps "always
// fetch every recent final's full box score" from turning into needless repeat
// requests for data that can no longer change. Cleared only by a full page reload.
const finalBoxScoreCache = new Map(); // gameEventId -> players[]

function setDot(cls) { $('dot').className = 'dot' + (cls ? ' ' + cls : ''); }

// Live scoreboard/box-score polling cadence. This is this app's OWN refresh rate — how
// often it asks ESPN's feed for anything new — not a claim that ESPN itself updates its
// numbers that quickly; a game's actual stats still only change as fast as the provider
// publishes them. Kept as one named constant + start/stop pair (rather than a bare
// setInterval call) so page-visibility pausing below can cleanly stop and restart it
// without ever creating a second, overlapping timer.
const REFRESH_INTERVAL_MS = 15000;
let refreshTimerId = null;
function startRefreshTimer() {
  if (refreshTimerId !== null) return; // already running — never create a second overlapping timer
  refreshTimerId = setInterval(refresh, REFRESH_INTERVAL_MS);
}
function stopRefreshTimer() {
  if (refreshTimerId === null) return;
  clearInterval(refreshTimerId);
  refreshTimerId = null;
}

function dayLabelFor(startTimestamp) {
  if (!startTimestamp) return '';
  const d = new Date(startTimestamp), now = new Date();
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  if (sameDay(d, now)) return 'Today';
  const yest = new Date(now); yest.setDate(yest.getDate() - 1);
  if (sameDay(d, yest)) return 'Yesterday';
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

async function fetchScoreboardEvents() {
  // Today's + yesterday's calendar day, merged and de-duplicated by event id — see
  // the RECENT_FINAL_WINDOW_MS / dateParam comment above for why both are needed.
  const [todayData, yestData] = await Promise.all([
    fetchJson(scoreboardUrl(0)),
    fetchJson(scoreboardUrl(-1)).catch(() => ({ events: [] })), // yesterday's fetch is a bonus, not required for the refresh to count as a success
  ]);
  const seen = new Map();
  for (const ev of (todayData.events || [])) seen.set(ev.id, ev);
  for (const ev of (yestData.events || [])) if (!seen.has(ev.id)) seen.set(ev.id, ev);
  return Array.from(seen.values());
}

// Adds the app's display-only fields to a parsed game (see game-binding.js for the core shape).
function decorateGame(parsed) {
  return {
    ...parsed,
    startTime: parsed.startTimestamp ? new Date(parsed.startTimestamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '',
    dayLabel: dayLabelFor(parsed.startTimestamp),
    players: [], teams: [parsed.home, parsed.away].filter(Boolean),
    feedError: false,
  };
}

// v6.1: a selection that is BOUND to a game (gameId) but not yet settled — e.g. the app
// wasn't opened until that game had already dropped out of the today+yesterday scoreboard
// — has its exact game loaded directly by event id from ESPN's per-event summary, so it
// can still settle against the RIGHT game. These games are kept separately from `games`
// (Game Center's list is unchanged) and are only ever used for their bound selections.
const boundGameCache = new Map(); // gameId -> decorated game (with players)
const BOUND_GAME_FETCH_LIMIT = 8;   // per refresh, to stay polite to the feed
function unsettledBoundGameIds() {
  const ids = new Set();
  const consider = s => { if (s && s.gameId && !s.settlement && !s.voided) ids.add(String(s.gameId)); };
  state.straightBets.forEach(consider);
  state.parlays.forEach(p => p.legs.forEach(consider));
  return [...ids];
}
async function loadBoundGamesOutsideFeed() {
  const missing = unsettledBoundGameIds().filter(id => !games.some(g => String(g.id) === id));
  for (const id of missing.slice(0, BOUND_GAME_FETCH_LIMIT)) {
    const cached = boundGameCache.get(id);
    if (cached && cached.state === 'post' && cached.completed !== false && cached.players.length) continue; // final — can't change
    try {
      const summary = await fetchJson(API_SUMMARY + encodeURIComponent(id));
      const parsed = gameFromSummary(summary, id);
      if (!parsed) continue;
      const g = decorateGame(parsed);
      g.players = extractAthletes(summary);
      recordSeenPlayers(g.players);
      boundGameCache.set(id, g);
    } catch (e) { /* stays unsettled; retried on the next refresh */ }
  }
}
// Every game the evaluator may use: the live scoreboard window plus bound games loaded by id.
function knownGames() {
  const out = [...games];
  for (const [id, g] of boundGameCache) if (!games.some(x => String(x.id) === id)) out.push(g);
  return out;
}

async function refresh() {
  if (busy) return;
  busy = true;
  $('refreshBtn').disabled = true;
  setDot('warn');
  try {
    const events = await fetchScoreboardEvents();
    const newGames = [];
    const results = await Promise.allSettled(events.map(async ev => {
      // Parsing is shared with the per-event summary path (game-binding.js), so a
      // game loaded either way has exactly the same shape (incl. ESPN's `completed` flag).
      const parsed = gameFromScoreboardEvent(ev);
      if (!parsed) return null;
      const { startTimestamp } = parsed;
      const homeAbbr = parsed.home, awayAbbr = parsed.away;
      // A finished game from a day other than today is only relevant to Game Center
      // within the ~24-hour window this app cares about — anything older is dropped
      // here rather than shown as a stale "Final" card from days ago.
      if (parsed.state === 'post' && startTimestamp && (Date.now() - startTimestamp) > RECENT_FINAL_WINDOW_MS) return null;
      const g = decorateGame(parsed);
      // Finished games never change again, so a previously-fetched final box score is
      // reused instead of re-downloaded every 15-second tick (see finalBoxScoreCache).
      if (g.state === 'post' && finalBoxScoreCache.has(ev.id)) {
        g.players = finalBoxScoreCache.get(ev.id);
      } else if (needsBoxScore(ev.id, homeAbbr, awayAbbr, g.state, startTimestamp)) {
        try {
          g.players = extractAthletes(await fetchJson(API_SUMMARY + encodeURIComponent(ev.id)));
          recordSeenPlayers(g.players); // feeds autocomplete even if the roster fetch failed/is stale
          if (g.state === 'post') finalBoxScoreCache.set(ev.id, g.players);
        }
        catch (e) { g.feedError = true; }
      }
      return g;
    }));
    results.forEach(r => { if (r.status === 'fulfilled' && r.value) newGames.push(r.value); });
    // Only replace `games` wholesale when the scoreboard fetch itself succeeded (it did,
    // we're in the try branch) — per-game box-score failures are flagged (feedError) on
    // that single game rather than discarding everything.
    games = newGames;
    await loadBoundGamesOutsideFeed();
    everSucceeded = true;
    lastSuccessAt = Date.now();
    $('lastUpdate').textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' });
    setDot('');
    $('notice').classList.remove('bad');
  } catch (e) {
    // Network/scoreboard failure: keep the last successful `games` snapshot as-is
    // (never zero it out) and just mark the feed as stale.
    $('lastUpdate').textContent = (lastSuccessAt ? 'Last updated ' + new Date(lastSuccessAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) + ' — ' : '') + 'feed unavailable (' + e.message + '), retrying automatically';
    setDot('bad');
    if (!everSucceeded) {
      $('notice').classList.add('bad');
      $('notice').innerHTML = '<b>Live feed is not reachable right now:</b> ' + esc(e.message) + '. Your tracked bets are still saved — this just means current scores/stats can\'t load. Try "Refresh now" in a minute.';
    }
  }
  busy = false;
  $('refreshBtn').disabled = false;
  renderAll();
}

function gameClockLabel(g) {
  if (!g) return '';
  if (g.state === 'post') return 'Final';
  if (g.state === 'halftime') return 'Halftime';
  if (g.state === 'in') return [g.period ? `Q${g.period}` : 'Live', g.clock || ''].filter(Boolean).join(' · ') || 'Live';
  return g.startTime || 'Scheduled';
}
function isStaleFeed() {
  return everSucceeded && lastSuccessAt && (Date.now() - lastSuccessAt) > 90000; // >3 missed refresh cycles
}

// ---------------------------------------------------------------------------
// Browser online/offline state (distinct from a single feed-fetch failure above —
// this is the browser's OWN connectivity signal, so it fires even before any
// refresh attempt happens). Viewing and editing saved bets never depends on this:
// that always works from the locally-saved copy. All this controls is a clear,
// honest banner — never presenting cached scores as live, and never implying
// offline access syncs anything between devices.
// ---------------------------------------------------------------------------
function updateOnlineBanner() {
  const banner = $('offlineBanner');
  if (!banner) return;
  const offline = typeof navigator !== 'undefined' && navigator.onLine === false;
  banner.classList.toggle('hide', !offline);
}
if (typeof window !== 'undefined') {
  window.addEventListener('online', () => {
    updateOnlineBanner();
    refresh(); // connection just came back — fetch fresh data right away rather than waiting for the next 15s tick
  });
  window.addEventListener('offline', updateOnlineBanner);
}
// ---------------------------------------------------------------------------
// Player-prop matching — resolves a prop selection to {g, a} = the specific game +
// athlete box-score row it refers to. v6.1: the search is restricted to the
// selection's CANDIDATE games (game-binding.js candidateGamesForSelection): its bound
// game if it has one, otherwise only games that tip off within the plausibility window
// around when the bet was recorded (and against the stated opponent, if any). This is
// what makes it impossible for an old bet to match a newer, unrelated game.
// ---------------------------------------------------------------------------
function namesMatch(target, candidate) {
  const pn = norm(target), an = norm(candidate);
  if (!pn || !an) return false;
  if (pn === an) return true;
  if (an.includes(pn) || pn.includes(an)) return true;
  const bits = target.toLowerCase().split(/\s+/).filter(x => x.length > 2);
  return bits.filter(x => candidate.toLowerCase().includes(x)).length >= 2;
}
function findPlayerInGames(p, gameList) {
  const candidates = [];
  for (const g of gameList) {
    for (const a of (g.players || [])) {
      const pn = norm(p.player), an = norm(a.name);
      if (!pn || !an) continue;
      let score = 0;
      if (pn === an) score = 100;
      else if (an.includes(pn) || pn.includes(an)) score = 70;
      else {
        const bits = p.player.toLowerCase().split(/\s+/).filter(x => x.length > 2);
        const matches = bits.filter(x => a.name.toLowerCase().includes(x)).length;
        if (matches >= 2) score = matches * 20;
      }
      if (score <= 0) continue;
      if (p.team && !sameTeam(p.team, a.team)) score -= 35;
      if (p.opponent && g.teams?.length) {
        const other = g.teams.find(t => !sameTeam(t, a.team));
        if (other && !sameTeam(p.opponent, other)) score -= 15;
      }
      if (score > 0) candidates.push({ a, g, score });
    }
  }
  candidates.sort((x, y) => y.score - x.score);
  return candidates[0] || null;
}
// Returns { g, a, bindable }. `bindable` = this match is specific enough to permanently
// bind the selection to `g` (an athlete was found in it, or the bet named its team).
function resolvePropSelection(sel) {
  const candidates = candidateGamesForSelection(sel, knownGames());
  if (!candidates.length) return { g: null, a: null, bindable: false };
  // 1. A selection picked from autocomplete carries a canonical ESPN athlete id — an
  //    exact match, tried first (candidate games only, best-ranked first).
  if (sel.playerId) {
    for (const g of candidates) {
      const a = (g.players || []).find(p => p.espnId && String(p.espnId) === String(sel.playerId));
      if (a) return { g, a, bindable: true };
    }
  }
  // 2. Name match within the candidate games, on the stated team when one was given.
  for (const g of candidates) {
    const a = (g.players || []).find(p => namesMatch(sel.player, p.name) && (!sel.team || sameTeam(sel.team, p.team)));
    if (a) return { g, a, bindable: true };
  }
  // 3. Fuzzy scoring across the candidate games (v3 behavior, now window-restricted).
  const found = findPlayerInGames(sel, candidates);
  if (found) return { g: found.g, a: found.a, bindable: true };
  // 4. No athlete row yet (e.g. pre-game: no box score) — the bet's own team still
  //    identifies its game, so it can show as "upcoming" and bind early.
  if (sel.team) return { g: candidates[0], a: null, bindable: true };
  return { g: null, a: null, bindable: false };
}

// ---------------------------------------------------------------------------
// ONE shared evaluator for every selection — straight prop, straight team bet,
// or a leg nested inside a parlay. A parlay leg is evaluated by the exact same
// function, using the exact same prop-status.js / settlement.js logic, as a
// straight bet.
//
// v6.1: a selection with a PERSISTED settlement is evaluated from its saved
// evidence only (final scores / final stat line of its bound game) — never from
// the live feed — so it can't change when its game leaves the feed window, and
// can never be re-settled against a different game. Unsettled selections are
// evaluated live, restricted to their bound/candidate game(s).
// ---------------------------------------------------------------------------
function gameFromSettlementRecord(st) {
  const snap = st.snapshot || {};
  const ts = st.gameStart ? Date.parse(st.gameStart) : null;
  return {
    id: st.gameId, home: snap.home, away: snap.away, homeScore: snap.homeScore, awayScore: snap.awayScore,
    state: 'post', completed: true, startTimestamp: Number.isFinite(ts) ? ts : null, gameDate: st.gameDate,
    dayLabel: dayLabelFor(Number.isFinite(ts) ? ts : null), teams: [snap.home, snap.away].filter(Boolean),
    players: [], startTime: '', fromSettlement: true,
  };
}
function evaluateLive(sel) {
  if (sel.kind === 'prop') {
    const { g, a, bindable } = resolvePropSelection(sel);
    const gameState = g ? g.state : undefined; // no game found at all -> prop-status.js treats athleteStats=null as 'unmatched'
    const athleteStats = a ? a.stats : null;
    const r = evaluatePropLeg(sel, athleteStats, gameState);
    return { ...r, g, a, bindable };
  }
  const gi = candidateGamesForSelection(sel, knownGames())[0] || null;
  // Team codes are canonicalized on both sides ("GSW" from autocomplete vs "GS" from ESPN).
  const gameForSettlement = gi ? { state: gi.state, homeAbbr: canonicalTeam(gi.home), awayAbbr: canonicalTeam(gi.away), homeScore: gi.homeScore, awayScore: gi.awayScore } : null;
  const r = evaluateTeamBet({ marketType: sel.marketType, team: canonicalTeam(sel.teamAbbr), direction: sel.direction, line: sel.line }, gameForSettlement);
  return { ...r, g: gi, a: null, bindable: !!gi };
}
function evaluateSelection(sel) {
  if (sel.voided) return { status: 'void', current: null, g: null, a: null };
  let ev;
  if (sel.settlement) {
    const r = evaluateFromSettlement(sel, sel.settlement);
    ev = { ...r, g: gameFromSettlementRecord(sel.settlement), a: sel.settlement.snapshot?.athlete || null, settled: true };
  } else {
    ev = evaluateLive(sel);
  }
  // A manual settlement correction (Bet History tab) always overrides the computed
  // status — it keeps the live game/current-stat context for display, but the
  // status itself is whatever the user explicitly set, with an audit-log entry.
  if (sel.manualStatus) ev = { ...ev, status: sel.manualStatus, corrected: true };
  return ev;
}

// Binds + settles one selection (persisted on the selection itself). Returns true if
// anything changed. Never rebinds a bound selection and never re-settles from the feed
// once a settlement exists — a saved settlement's status is only ever re-derived from
// its own saved evidence (so an edit to line/direction/market is still honored).
function syncSelectionSettlement(sel, now) {
  if (sel.voided) return false;
  if (sel.settlement) {
    const r = evaluateFromSettlement(sel, sel.settlement);
    const cur = r.current ?? null;
    if (r.status !== sel.settlement.status || cur !== sel.settlement.current) {
      sel.settlement.status = r.status; sel.settlement.current = cur; sel.settlement.recomputedAt = now;
      return true;
    }
    return false;
  }
  const ev = evaluateLive(sel);
  let changed = false;
  if (!sel.gameId && ev.bindable && ev.g && ev.g.id) { Object.assign(sel, bindingFor(ev.g, now)); changed = true; }
  if (shouldPersistSettlement(sel, ev)) { sel.settlement = buildSettlement(sel, ev, now); changed = true; }
  return changed;
}
function runSettlementSweep() {
  const now = Date.now();
  let changed = false;
  const visit = sel => { if (syncSelectionSettlement(sel, now)) changed = true; };
  state.straightBets.forEach(visit);
  state.parlays.forEach(p => p.legs.forEach(visit));
  if (changed) saveState();
}
// gameStates(): one game-state slot per game a ticket actually depends on, for the
// 30-minute archival rule (archive.js). A manually-voided selection counts as its
// own already-settled slot ('post'), matching archive.js's documented contract.
function gameStatesForStraight(sel) {
  const ev = evaluateSelection(sel);
  if (sel.voided) return ['post'];
  return [ev.g ? ev.g.state : null];
}
function gameStatesForParlay(p) {
  return p.legs.map(l => {
    if (l.voided) return 'post';
    const ev = evaluateSelection(l);
    return ev.g ? ev.g.state : null;
  });
}

const STATUS_INFO = {
  unmatched: { txt: 'Needs review', cls: 'review' },
  upcoming: { txt: 'Upcoming', cls: 'wait' },
  live: { txt: 'LIVE', cls: 'live' },
  halftime: { txt: 'Halftime', cls: 'half' },
  // Deliberately NOT the same color as a confirmed win (cls 'hit' = green): a target
  // reached while the game is still live can still end in a push/loss/different number
  // by the final whistle, so it stays in the "live" color family (blue) until the
  // ticket's canonical result is actually final — never presented as if already won.
  target_reached_live: { txt: 'TARGET REACHED — LIVE', cls: 'live-hit' },
  won: { txt: 'WON', cls: 'hit' },
  lost: { txt: 'LOST', cls: 'miss' },
  push: { txt: 'PUSH', cls: 'push' },
  void: { txt: 'VOID', cls: 'void' },
};
function statusDisplay(ev) {
  // 'halftime' isn't a prop-status.js/settlement.js status — those correctly treat
  // halftime as just another live moment — but it reads better surfaced distinctly.
  if ((ev.status === 'live') && ev.g && ev.g.state === 'halftime') return STATUS_INFO.halftime;
  return STATUS_INFO[ev.status] || { txt: ev.status, cls: '' };
}
function isSettled(status) { return status === 'won' || status === 'lost' || status === 'push' || status === 'void'; }

function selectionLabel(sel) {
  return sel.kind === 'prop'
    ? `${sel.player}${sel.team ? ' (' + sel.team + ')' : ''} — ${marketLabel(sel.market)} ${sel.direction === 'over' ? 'O' : 'U'} ${fmt(sel.target)}`
    : `${sel.teamAbbr} — ${teamMarketLabel(sel)}`;
}
function teamMarketLabel(bet) {
  switch (bet.marketType) {
    case 'moneyline': return `Moneyline · ${bet.direction === 'team' ? bet.teamAbbr : bet.oppAbbr || 'opponent'} to win`;
    case 'spread': return `Spread ${bet.line > 0 ? '+' : ''}${fmt(bet.line)}`;
    case 'teamTotal': return `Team total ${bet.direction === 'over' ? 'Over' : 'Under'} ${fmt(bet.line)}`;
    case 'gameTotal': return `Game total ${bet.direction === 'over' ? 'Over' : 'Under'} ${fmt(bet.line)}`;
    default: return bet.marketType;
  }
}

// ---------------------------------------------------------------------------
// Visual helpers: team logos / player headshots (ESPN CDN, best-effort, with a
// graceful fallback so a missing/blocked image never breaks layout or function).
// ---------------------------------------------------------------------------
function teamLogoHtml(abbr) {
  if (!abbr) return `<span class="team-logo-fallback">—</span>`;
  const url = `https://a.espncdn.com/i/teamlogos/nba/500/${abbr.toLowerCase()}.png`;
  return `<img class="team-logo" src="${esc(url)}" alt="${esc(abbr)}" loading="lazy" onerror="this.outerHTML='&lt;span class=&quot;team-logo-fallback&quot;&gt;${esc(abbr)}&lt;/span&gt;'">`;
}
function playerAvatarHtml(sel, a) {
  const initials = esc((sel.player || '?').split(/\s+/).map(w => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase());
  if (a && a.espnId) {
    const url = `https://a.espncdn.com/i/headshots/nba/players/full/${a.espnId}.png`;
    return `<img class="avatar" src="${esc(url)}" alt="${esc(sel.player)}" loading="lazy" onerror="this.outerHTML='&lt;span class=&quot;avatar-fallback&quot;&gt;${initials}&lt;/span&gt;'">`;
  }
  return `<span class="avatar-fallback">${initials}</span>`;
}
// ---------------------------------------------------------------------------
// ONE shared card renderer for a selection (prop or team), used identically by
// the Player Props board, Team Markets board, every parlay leg, and Overview.
// This is the actual fix for "parlay legs only show a generic LIVE badge":
// there is now exactly one rendering function, not two.
// ---------------------------------------------------------------------------
function renderSelectionCard(sel, opts = {}) {
  const ev = evaluateSelection(sel);
  const info = statusDisplay(ev);
  // The matched game's day is shown whenever it isn't today, so it's always visible
  // WHICH game a bet was judged against (v6.1 — never silently a different day's game).
  const dayTag = ev.g && ev.g.dayLabel && ev.g.dayLabel !== 'Today' ? ` · ${esc(ev.g.dayLabel)}` : '';
  const scoreLine = ev.g ? `${esc(ev.g.away)} ${esc(String(ev.g.awayScore))} — ${esc(ev.g.home)} ${esc(String(ev.g.homeScore))} · ${esc(gameClockLabel(ev.g))}${dayTag}` : (sel.game ? esc(sel.game) : 'Matchup not found yet');
  const removeBtn = opts.onRemove ? `<button type="button" class="danger" data-selaction="remove" style="padding:3px 9px;font-size:11px">Remove</button>` : '';
  // Void is a manual, user-driven override (e.g. the sportsbook voided the bet for a
  // reason our feed can't see — an injury scratch, a postponed game) and always wins
  // over the computed status, so it's offered regardless of whether settlement already
  // computed won/lost/push.
  const voidBtn = opts.onToggleVoid ? `<button type="button" class="void-btn" data-selaction="void">${sel.voided ? 'Un-void' : 'Mark void'}</button>` : '';
  // Retroactive edit — corrects the bet/leg's own recorded facts (name, line, odds,
  // stake…), distinct from the Void toggle above and from Bet History's manual
  // result correction: this changes what was actually entered, not the outcome.
  const editBtn = opts.onEdit ? `<button type="button" class="secondary" data-selaction="edit" style="font-size:11px">Edit</button>` : '';
  // Ladder controls only apply to a straight bet (has `.id`), never to a leg inside
  // a parlay — a parlay links to a ladder as ONE ticket, not leg-by-leg.
  const ladderHtml = (opts.onLadder && sel.id) ? ladderControlsHtml('straight', sel.id, sel) : '';
  const footActions = (removeBtn || voidBtn || editBtn || ladderHtml) ? `<div class="btnrow" style="margin-top:8px">${voidBtn}${editBtn}${removeBtn}${ladderHtml}</div>` : '';

  if (sel.kind === 'prop') {
    const { current, a } = ev;
    const comps = componentsForMarket(sel.market);
    const compStr = a ? comps.map(c => `${c} ${fmt(rawStat(a.stats, c))}`).join(' · ') : 'Waiting for live box score';
    const target = sel.target;
    let pct = 0;
    if (a && current !== null && current !== undefined) {
      pct = sel.direction === 'over'
        ? Math.min(100, Math.max(0, target > 0 ? (current / target) * 100 : 0))
        : (current <= target ? Math.min(100, (current / Math.max(target, 1)) * 100) : 100);
    }
    const underFailing = sel.direction === 'under' && current !== null && current >= target && !isSettled(ev.status);
    const remaining = current === null ? null : (sel.direction === 'over' ? Math.max(0, target - current) : Math.max(0, current - target));
    const progressText = current === null ? 'No live data yet'
      : (sel.direction === 'over' ? (current >= target ? 'Target reached' : `${fmt(remaining)} to go`) : (current < target ? 'Under — on pace' : `${fmt(remaining)} over the line`));
    return `<article class="prop selcard" data-sel="${sel._uid}">
      <div class="prop-top">
        <div class="player-head">${playerAvatarHtml(sel, a)}<div><div class="player">${esc(sel.player)}</div><div class="market">${esc(marketLabel(sel.market))} · ${sel.direction === 'over' ? 'OVER' : 'UNDER'}</div></div></div>
        <div class="target">${esc(fmt(target))}</div>
      </div>
      <div class="numbers"><div><div class="current">${current === null ? '—' : fmt(current)} <span>/ ${fmt(target)}</span></div><div class="subtle" style="font-size:11px">${esc(compStr)}</div></div><div class="remaining">${esc(progressText)}</div></div>
      <div class="bar"><div class="fill${underFailing ? ' under-fail' : ''}" style="width:${pct}%"></div></div>
      <div class="prop-foot"><span class="pill ${info.cls}">${esc(info.txt)}</span><span>${scoreLine}${sel.odds ? ' · ' + esc(sel.odds) : ''}</span></div>
      ${footActions}
    </article>`;
  }

  // team selection
  const { current } = ev;
  let currentLine;
  if (current === null || current === undefined) currentLine = 'Waiting for live score';
  else if (sel.marketType === 'spread') currentLine = `Margin: ${current > 0 ? '+' : ''}${fmt(current)}${ev.adjustedMargin !== undefined ? ` (vs. line ${sel.line > 0 ? '+' : ''}${fmt(sel.line)})` : ''}`;
  else if (sel.marketType === 'moneyline') currentLine = `Score: ${current}${ev.leading !== undefined ? (ev.leading ? ' (leading)' : ' (trailing)') : ''}`;
  else currentLine = `Current: ${fmt(current)} (line ${fmt(sel.line)})`;
  return `<article class="prop selcard" data-sel="${sel._uid}">
    <div class="prop-top">
      <div class="player-head">${teamLogoHtml(sel.teamAbbr)}<div><div class="player">${esc(sel.teamAbbr)}${sel.oppAbbr ? ' vs ' + esc(sel.oppAbbr) : ''}</div><div class="market">${esc(teamMarketLabel(sel))}</div></div></div>
    </div>
    <div class="numbers"><div class="current" style="font-size:16px">${esc(currentLine)}</div></div>
    <div class="prop-foot"><span class="pill ${info.cls}">${esc(info.txt)}</span><span>${scoreLine}${sel.odds ? ' · ' + esc(sel.odds) : ''}</span></div>
    ${footActions}
  </article>`;
}
function bindSelectionCardActions(container, sel, afterChange) {
  const card = container.querySelector(`[data-sel="${sel._uid}"]`);
  if (!card) return;
  card.querySelectorAll('[data-selaction]').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.selaction === 'void') { sel.voided = !sel.voided; saveState(); afterChange(); }
      if (btn.dataset.selaction === 'remove') { afterChange('remove'); }
      if (btn.dataset.selaction === 'edit') { openSelectionEditModal(sel, afterChange); }
    });
  });
}

// ---------------------------------------------------------------------------
// RETROACTIVE EDITING — lets you go back and fix a mistake after the fact: a
// typo'd player/team name, the wrong line/odds, a stake entered wrong, a
// ladder's own name/goal, a ladder step's stake/odds, or a parlay ticket's
// name/stake/payout. This is deliberately NOT the same thing as Bet History's
// "Force WON/LOST…" correction above (that overrides a RESULT) — editing
// corrects the bet's own recorded FACTS, and settlement still always
// re-evaluates live from stats/scores using whatever facts are on file after
// you save. Every edit is written to the audit log, the same place manual
// result corrections go, so a past change is never silent. Editing a ladder
// step's stake/odds after it has already settled also recomputes that one
// step's payout/profit (via the same settleStep() used everywhere else) so
// the ladder's own running balance never goes stale after an edit.
// ---------------------------------------------------------------------------
const MARKET_OPTIONS = [
  { value: 'PRA', label: 'PRA' }, { value: 'PR', label: 'Points + Rebounds' },
  { value: 'PA', label: 'Points + Assists' }, { value: 'PTS', label: 'Points' },
  { value: 'REB', label: 'Rebounds' }, { value: 'AST', label: 'Assists' },
  { value: '3PM', label: '3-pointers made' }, { value: 'STL', label: 'Steals' },
  { value: 'BLK', label: 'Blocks' }, { value: 'TO', label: 'Turnovers' },
];
const DIRECTION_OPTIONS = [{ value: 'over', label: 'Over' }, { value: 'under', label: 'Under' }];
const TEAM_MARKET_OPTIONS = [
  { value: 'moneyline', label: 'Moneyline' }, { value: 'spread', label: 'Spread' },
  { value: 'teamTotal', label: 'Team total' }, { value: 'gameTotal', label: 'Game total' },
];

function closeEditModal() {
  const overlay = $('editModalOverlay');
  overlay.classList.add('hide');
  overlay.innerHTML = '';
}
function editFieldHtml(f) {
  const id = `edit-f-${f.key}`;
  const val = f.value;
  if (f.type === 'select') {
    return `<div class="field${f.full ? ' full' : ''}"><label>${esc(f.label)}</label><select id="${id}">${f.options.map(o => `<option value="${esc(o.value)}" ${String(val ?? '') === String(o.value) ? 'selected' : ''}>${esc(o.label)}</option>`).join('')}</select></div>`;
  }
  const type = f.type === 'number' ? 'number' : (f.type === 'date' ? 'date' : 'text');
  const step = f.type === 'number' ? ` step="${f.step || '0.01'}"` : '';
  return `<div class="field${f.full ? ' full' : ''}"><label>${esc(f.label)}</label><input id="${id}" type="${type}"${step} value="${esc(val ?? '')}" placeholder="${esc(f.placeholder || '')}"></div>`;
}
// `fields`: [{key, label, type:'text'|'number'|'select'|'date', value, options?, full?, required?}]
// `onSave(values)`: receives a {key: parsedValue} map (numbers already converted);
// responsible for applying the change, logging it, saveState(), and re-rendering.
function openEditModal({ title, hint, fields, onSave }) {
  const overlay = $('editModalOverlay');
  overlay.innerHTML = `<div class="modal-box">
    <h3>${esc(title)}</h3>
    ${hint ? `<p>${esc(hint)}</p>` : ''}
    <div class="formgrid">${fields.map(editFieldHtml).join('')}</div>
    <p class="err" id="editModalErr" style="display:none"></p>
    <div class="btnrow">
      <button type="button" class="secondary" id="editModalCancel">Cancel</button>
      <button type="button" class="primary" id="editModalSave">Save changes</button>
    </div>
  </div>`;
  overlay.classList.remove('hide');
  $('editModalCancel').addEventListener('click', closeEditModal);
  $('editModalSave').addEventListener('click', () => {
    const values = {};
    for (const f of fields) {
      const el = document.getElementById(`edit-f-${f.key}`);
      let v = el.value;
      if (typeof v === 'string') v = v.trim();
      if (f.required && v === '') {
        $('editModalErr').textContent = `${f.label} is required.`;
        $('editModalErr').style.display = 'block';
        return;
      }
      if (f.type === 'number') v = v === '' ? null : Number(v);
      else v = v === '' ? null : v;
      values[f.key] = v;
    }
    onSave(values);
    closeEditModal();
  });
}
// Builds a plain-language summary of exactly what changed, for the audit log —
// the same honesty standard as a manual result correction: never a silent edit.
function editDiffSummary(before, values) {
  const parts = [];
  for (const k in values) {
    const oldV = before[k] !== undefined ? before[k] : null;
    const newV = values[k];
    if (String(oldV ?? '') !== String(newV ?? '')) parts.push(`${k}: ${JSON.stringify(oldV ?? null)} → ${JSON.stringify(newV ?? null)}`);
  }
  return parts.length ? parts.join('; ') : 'no changes';
}
function logEdit(entityType, entityId, summary) {
  state.auditLog.push({ ts: Date.now(), ticketType: entityType, ticketId: entityId, from: 'edit', to: summary, note: 'retroactive edit' });
}

// --- Straight bet / parlay leg edit (shared shape — a leg is everything a
// straight bet is, minus its own stake/bookmaker/matchup label, which only
// exist at the whole-ticket level) ---
function selectionEditFields(sel) {
  const isTopLevel = !!sel.id; // top-level straight bet vs. a leg nested in a parlay
  let fields;
  if (sel.kind === 'prop') {
    fields = [
      { key: 'player', label: 'Player name', value: sel.player, full: true, required: true },
      { key: 'team', label: 'Team', value: sel.team },
      { key: 'opponent', label: 'Opponent', value: sel.opponent },
      { key: 'market', label: 'Stat / combo', type: 'select', options: MARKET_OPTIONS, value: sel.market },
      { key: 'target', label: 'Target (your line)', type: 'number', step: '0.5', value: sel.target, required: true },
      { key: 'direction', label: 'Direction', type: 'select', options: DIRECTION_OPTIONS, value: sel.direction },
      { key: 'odds', label: 'Odds (American, optional)', value: sel.odds },
    ];
  } else {
    fields = [
      { key: 'teamAbbr', label: 'Team', value: sel.teamAbbr, required: true },
      { key: 'oppAbbr', label: 'Opponent', value: sel.oppAbbr },
      { key: 'marketType', label: 'Market type', type: 'select', options: TEAM_MARKET_OPTIONS, value: sel.marketType },
      { key: 'line', label: 'Line', type: 'number', step: '0.5', value: sel.line },
      { key: 'direction', label: 'Direction', type: 'select', options: DIRECTION_OPTIONS, value: sel.direction },
      { key: 'odds', label: 'Odds (American, optional)', value: sel.odds },
    ];
  }
  // v6.1: optional explicit game date — only needed to disambiguate (e.g. a bet entered
  // after the fact while the same team also plays again within a day). Changing it, or
  // the player/team/opponent, re-matches the bet to a game from scratch.
  fields.push({ key: 'gameDateHint', label: 'Game date (ET, optional)', type: 'date', value: sel.gameDateHint });
  if (isTopLevel) fields.push(
    { key: 'bookmaker', label: 'Bookmaker (optional)', value: sel.bookmaker },
    { key: 'stake', label: 'Stake (optional)', type: 'number', value: sel.stake },
    { key: 'game', label: 'Matchup label (optional)', value: sel.game },
  );
  return fields;
}
function applySelectionEdit(sel, values) {
  const before = { ...sel };
  if (sel.kind === 'prop') {
    // A renamed player can no longer be trusted to be the same autocomplete-picked athlete.
    if (String(values.player ?? '') !== String(sel.player ?? '')) sel.playerId = null;
    sel.player = values.player;
    sel.team = values.team ? String(values.team).toUpperCase() : null;
    sel.opponent = values.opponent ? String(values.opponent).toUpperCase() : null;
    sel.market = values.market;
    sel.target = Number(values.target);
    sel.direction = values.direction;
    sel.odds = values.odds || null;
  } else {
    sel.teamAbbr = values.teamAbbr ? String(values.teamAbbr).toUpperCase() : sel.teamAbbr;
    sel.oppAbbr = values.oppAbbr ? String(values.oppAbbr).toUpperCase() : null;
    sel.marketType = values.marketType;
    sel.line = (values.line === null || values.line === undefined) ? null : Number(values.line);
    sel.direction = values.marketType === 'moneyline' ? 'team' : values.direction;
    sel.odds = values.odds || null;
  }
  if ('bookmaker' in values) sel.bookmaker = values.bookmaker || null;
  if ('stake' in values) sel.stake = (values.stake === null || values.stake === undefined) ? null : Number(values.stake);
  if ('game' in values) sel.game = values.game || null;
  if ('gameDateHint' in values) sel.gameDateHint = values.gameDateHint || null;
  // Identity change (different player/team/opponent/game date) -> the old game binding and
  // saved settlement no longer apply; the selection is matched afresh. A change to only the
  // line/direction/market/odds keeps the binding, and a saved settlement re-derives its
  // status from its own saved final stat line (see syncSelectionSettlement()).
  if (identityChanged(before, sel)) clearBinding(sel);
}
function openSelectionEditModal(sel, afterChange) {
  const isTopLevel = !!sel.id;
  const before = { ...sel };
  openEditModal({
    title: isTopLevel ? 'Edit bet' : 'Edit parlay leg',
    hint: sel.settlement
      ? `Already settled from the final box score of ${sel.settlement.gameDate || 'its game'}. Changing the line/direction/stat re-evaluates against that saved final stat line; changing the player/team/opponent/game date clears the saved result and re-matches the bet to a game.`
      : 'Corrects the bet\'s own recorded facts (name, line, odds, stake…) — settlement will re-evaluate live from stats using whatever is on file after you save.',
    fields: selectionEditFields(sel),
    onSave: (values) => {
      applySelectionEdit(sel, values);
      logEdit(isTopLevel ? 'straight' : 'parlayLeg', sel.id || sel._uid, editDiffSummary(before, values));
      saveState();
      afterChange();
    },
  });
}

// ---------------------------------------------------------------------------
// Roster index (for autocomplete) — dynamic, cached, graceful on failure
// ---------------------------------------------------------------------------
let rosterIndex = { players: [], builtAt: 0, failedTeams: [], building: false };
function loadRosterCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(ROSTER_KEY) || 'null');
    if (raw && raw.players) rosterIndex = raw;
  } catch (e) { /* ignore */ }
}
function saveRosterCache() {
  try { localStorage.setItem(ROSTER_KEY, JSON.stringify(rosterIndex)); } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// "Seen players" cache — fixes the exact bug the spec called out: a player can
// be typed manually and match correctly against a LIVE box score (resolvePropSelection
// searches games[].players directly), but not show up in autocomplete, because
// autocomplete only ever searched the separately-fetched roster list (rosterIndex),
// which can fail, go stale, or simply not have loaded yet. Every player this app
// has ever actually seen in a successfully-loaded box score is cached here, keyed
// by a canonical id (their ESPN athlete id when known), and merged into every
// autocomplete search alongside the roster list — so "matched live but missing
// from autocomplete" can no longer happen once a player has appeared in any game
// this app has fetched. This is also where a canonical player ID comes from.
// ---------------------------------------------------------------------------
const SEEN_PLAYERS_KEY = 'nbaCC.seenPlayers.v1';
let seenPlayers = {}; // canonicalId -> { name, team, espnId, lastSeenAt }
function playerCanonicalId(p) { return p.espnId ? `espn:${p.espnId}` : `name:${norm(p.team || '')}:${norm(p.name)}`; }
function loadSeenPlayers() {
  try { seenPlayers = JSON.parse(localStorage.getItem(SEEN_PLAYERS_KEY) || 'null') || {}; } catch (e) { seenPlayers = {}; }
}
function saveSeenPlayers() {
  try { localStorage.setItem(SEEN_PLAYERS_KEY, JSON.stringify(seenPlayers)); } catch (e) { /* ignore */ }
}
function recordSeenPlayers(players) {
  if (!players || !players.length) return;
  let changed = false;
  for (const p of players) {
    if (!p.name) continue;
    const id = playerCanonicalId(p);
    const existing = seenPlayers[id];
    if (!existing || existing.team !== p.team || existing.name !== p.name) changed = true;
    seenPlayers[id] = { name: p.name, team: p.team, espnId: p.espnId || null, lastSeenAt: Date.now() };
  }
  if (changed) saveSeenPlayers();
}
// The combined, deduplicated pool every autocomplete search runs against: the
// fetched roster list UNION every player ever actually seen in a live box score.
// Deduplicated by canonical id so the same person never appears twice.
function combinedPlayerPool() {
  const byId = new Map();
  for (const p of rosterIndex.players) byId.set(playerCanonicalId(p), p);
  for (const id in seenPlayers) { if (!byId.has(id)) byId.set(id, seenPlayers[id]); }
  return [...byId.values()];
}
async function fetchTeamRoster(team) {
  const data = await fetchJson(API_ROSTER(team.abbr));
  const athletes = data?.athletes || [];
  // ESPN's roster shape has varied between a flat list and grouped-by-position; handle both.
  const flat = Array.isArray(athletes) && athletes.length && athletes[0].items
    ? athletes.flatMap(g => g.items || [])
    : athletes;
  return flat.map(a => ({ name: a?.displayName || a?.fullName, team: team.abbr, espnId: a?.id || null })).filter(p => p.name);
}
async function buildRosterIndex(force) {
  if (rosterIndex.building) return;
  if (!force && rosterIndex.players.length && (Date.now() - rosterIndex.builtAt) < ROSTER_MAX_AGE_MS) return;
  rosterIndex.building = true;
  const players = [];
  const failed = [];
  // Fetched in small concurrent batches rather than one-by-one (faster for the
  // user) or all 30 at once (politer to an endpoint with no published rate limit).
  const BATCH_SIZE = 6;
  for (let i = 0; i < NBA_TEAMS.length; i += BATCH_SIZE) {
    const batch = NBA_TEAMS.slice(i, i + BATCH_SIZE);
    const results = await Promise.allSettled(batch.map(fetchTeamRoster));
    results.forEach((res, j) => {
      if (res.status === 'fulfilled') players.push(...res.value);
      else failed.push(batch[j].abbr);
    });
    // Let the UI reflect progress incrementally instead of waiting for all 30 teams.
    rosterIndex = { players: [...players], builtAt: Date.now(), failedTeams: [...failed], building: true };
  }
  rosterIndex = { players, builtAt: Date.now(), failedTeams: failed, building: false };
  saveRosterCache();
  renderRosterStatus();
}
function renderRosterStatus() {
  const el = $('rosterStatus');
  if (!el) return;
  const seenCount = Object.keys(seenPlayers).length;
  const extra = seenCount ? ` Autocomplete also includes ${seenCount} player${seenCount === 1 ? '' : 's'} seen in live box scores.` : '';
  if (rosterIndex.building) { el.textContent = `Loading current rosters from ESPN… (${rosterIndex.players.length} players so far).${extra}`; return; }
  if (!rosterIndex.players.length) {
    el.textContent = (rosterIndex.failedTeams.length
      ? `Couldn't load rosters from ESPN (no internet, or the feed is unreachable).`
      : `Rosters not loaded yet.`) + ` You can still type any player/team name manually.${extra}`;
    return;
  }
  const asOf = rosterIndex.builtAt ? new Date(rosterIndex.builtAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : '';
  el.textContent = (rosterIndex.failedTeams.length
    ? `Rosters loaded from ESPN for ${30 - rosterIndex.failedTeams.length}/30 teams as of ${asOf} (${rosterIndex.failedTeams.join(', ')} failed — type those players manually). Not claiming full roster completeness.`
    : `Rosters loaded from ESPN for all 30 teams as of ${asOf}.`) + extra;
}

// Generic autocomplete attach: input element -> list of {label, sub, onSelect(item)}
function attachAutocomplete(inputEl, getItems) {
  let box = null, items = [], activeIdx = -1;
  function close() { if (box) { box.remove(); box = null; } activeIdx = -1; }
  function open(list) {
    close();
    if (!list.length) return;
    items = list;
    box = document.createElement('div');
    box.className = 'autocomplete-list';
    items.forEach((it, i) => {
      const row = document.createElement('div');
      row.className = 'autocomplete-item';
      row.innerHTML = `<span>${esc(it.label)}</span><small>${esc(it.sub || '')}</small>`;
      row.addEventListener('mousedown', e => { e.preventDefault(); it.onSelect(); close(); });
      box.appendChild(row);
    });
    inputEl.parentElement.appendChild(box);
  }
  let debounceTimer = null;
  inputEl.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      const q = inputEl.value.trim();
      if (q.length < 2) { close(); return; }
      open(getItems(q));
    }, 120);
  });
  inputEl.addEventListener('blur', () => setTimeout(close, 150));
  inputEl.addEventListener('keydown', e => {
    if (!box) return;
    if (e.key === 'ArrowDown') { activeIdx = Math.min(items.length - 1, activeIdx + 1); updateActive(); e.preventDefault(); }
    if (e.key === 'ArrowUp') { activeIdx = Math.max(0, activeIdx - 1); updateActive(); e.preventDefault(); }
    if (e.key === 'Enter' && activeIdx >= 0) { items[activeIdx].onSelect(); close(); e.preventDefault(); }
    if (e.key === 'Escape') close();
  });
  function updateActive() {
    [...box.children].forEach((c, i) => c.classList.toggle('active', i === activeIdx));
  }
}
function searchPlayers(query) {
  const nq = norm(query);
  if (!nq) return [];
  // Combined pool: the fetched roster list UNION every player ever actually seen
  // in a live box score, deduplicated by canonical id (fixes "matched live but
  // missing from autocomplete"). Also dedupes by name+team as a final safety net
  // in case the same person ended up with two different ids somehow.
  const pool = combinedPlayerPool();
  const byNameTeam = new Map();
  for (const p of pool) byNameTeam.set(`${norm(p.name)}|${norm(p.team || '')}`, p);
  const scored = [...byNameTeam.values()].map(p => {
    const nn = norm(p.name);
    // Partial first-name / last-name matching: split the full name into parts and
    // match the query against any individual part's prefix, not just the whole
    // string — so "curr" matches "Stephen Curry" and "steph" also matches it.
    const parts = p.name.toLowerCase().split(/[\s.'-]+/).filter(Boolean).map(norm);
    let score = 0;
    if (nn === nq) score = 120;
    else if (nn.startsWith(nq)) score = 100;
    else if (parts.some(part => part.startsWith(nq))) score = 80;
    else if (nn.includes(nq)) score = 60;
    else {
      const team = NBA_TEAMS.find(t => t.abbr === p.team);
      if (team && (nq.length >= 3) && team.aliases.some(a => norm(a).includes(nq))) score = 30;
    }
    return { p, score };
  }).filter(x => x.score > 0).sort((a, b) => b.score - a.score || a.p.name.localeCompare(b.p.name)).slice(0, 8);
  return scored.map(({ p }) => ({ label: p.name, sub: p.team, onSelect: null, _p: p }));
}
function searchTeams(query) {
  const nq = norm(query);
  if (!nq) return [];
  return NBA_TEAMS.filter(t => norm(t.name).includes(nq) || t.aliases.some(a => norm(a).includes(nq)) || norm(t.abbr) === nq)
    .slice(0, 8).map(t => ({ label: t.name, sub: t.abbr, _t: t }));
}
function setupPlayerAutocomplete(playerInput, teamInput) {
  attachAutocomplete(playerInput, q => searchPlayers(q).map(item => ({
    ...item,
    // Never silently replaces a manually entered player while they're still typing —
    // this only fires when the user actively picks a suggestion from the dropdown.
    // Picking a suggestion also resolves this selection to a canonical ESPN athlete
    // id (when one is known) rather than leaving it as free text only — stashed on
    // the input itself and read at submit time.
    onSelect: () => {
      playerInput.value = item._p.name;
      if (teamInput) teamInput.value = item._p.team;
      playerInput.dataset.playerId = item._p.espnId ? String(item._p.espnId) : '';
    }
  })));
  // Typing further after a pick means the text may no longer refer to that exact
  // player — clear the stashed id so a stale id never silently sticks to new text.
  playerInput.addEventListener('input', () => { playerInput.dataset.playerId = ''; });
}
function setupTeamAutocomplete(teamInput) {
  attachAutocomplete(teamInput, q => searchTeams(q).map(item => ({
    ...item,
    onSelect: () => { teamInput.value = item._t.abbr; }
  })));
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.tabpanel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('tab-' + btn.dataset.tab).classList.add('active');
  });
});

// ---------------------------------------------------------------------------
// Straight bet form
// ---------------------------------------------------------------------------
let straightBetType = 'prop';
document.querySelectorAll('[data-bettype]').forEach(btn => {
  btn.addEventListener('click', () => {
    straightBetType = btn.dataset.bettype;
    document.querySelectorAll('[data-bettype]').forEach(b => b.classList.toggle('active', b === btn));
    $('propFields').classList.toggle('hide', straightBetType !== 'prop');
    $('teamFields').classList.toggle('hide', straightBetType !== 'team');
  });
});
$('t-markettype').addEventListener('change', () => {
  const mt = $('t-markettype').value;
  $('t-direction-field').classList.toggle('hide', mt === 'moneyline' || mt === 'spread');
  $('t-line-field').querySelector('label').textContent = mt === 'spread' ? 'Spread (e.g. -5.5 or +5.5)' : (mt === 'moneyline' ? 'Line (not used)' : 'Line (total)');
  $('t-line-field').classList.toggle('hide', mt === 'moneyline');
  updateTLineHelp();
});
setupPlayerAutocomplete($('s-player'), $('s-team'));
setupTeamAutocomplete($('t-team'));

// ---------------------------------------------------------------------------
// Line-interpretation helper text — shows exactly how the entered Over/Under (or
// spread) line will be judged, using the SAME evaluateOverUnderLine() rule the
// rest of the app settles with (line-rules.js). This is explanation only — it
// never changes the number the user typed.
// ---------------------------------------------------------------------------
function bindLineHelper(targetId, directionId, helpId) {
  const targetEl = $(targetId), helpEl = $(helpId);
  const directionEl = directionId ? $(directionId) : null;
  const update = () => {
    const n = targetEl.value;
    helpEl.textContent = n ? lineHelperText(directionEl ? directionEl.value : 'over', n) : '';
  };
  targetEl.addEventListener('input', update);
  if (directionEl) directionEl.addEventListener('change', update);
  return update;
}
bindLineHelper('s-target', 's-direction', 's-line-help');
bindLineHelper('l-target', 'l-direction', 'l-line-help');
function updateTLineHelp() {
  const mt = $('t-markettype').value;
  const n = $('t-line').value;
  if (!n) { $('t-line-help').textContent = ''; return; }
  if (mt === 'spread') {
    $('t-line-help').textContent = Number.isInteger(Number(n))
      ? `A whole-number spread (${n}) can push — the game lands exactly on the number.`
      : `A half-point spread (${n}) never pushes — there's always a winner against this line.`;
    return;
  }
  if (mt === 'moneyline') { $('t-line-help').textContent = ''; return; }
  $('t-line-help').textContent = lineHelperText($('t-direction').value, n);
}
$('t-line').addEventListener('input', updateTLineHelp);
$('t-direction').addEventListener('change', updateTLineHelp);
function updateLtLineHelp() {
  const mt = $('lt-markettype').value;
  const n = $('lt-line').value;
  if (!n) { $('lt-line-help').textContent = ''; return; }
  if (mt === 'spread') {
    $('lt-line-help').textContent = Number.isInteger(Number(n))
      ? `A whole-number spread (${n}) can push — the game lands exactly on the number.`
      : `A half-point spread (${n}) never pushes — there's always a winner against this line.`;
    return;
  }
  if (mt === 'moneyline') { $('lt-line-help').textContent = ''; return; }
  $('lt-line-help').textContent = lineHelperText($('lt-direction').value, n);
}
$('lt-line').addEventListener('input', updateLtLineHelp);
$('lt-direction').addEventListener('change', updateLtLineHelp);
$('lt-markettype').addEventListener('change', updateLtLineHelp);

$('straightForm').addEventListener('submit', e => {
  e.preventDefault();
  const errEl = $('straightErr');
  errEl.style.display = 'none';
  const shared = {
    id: uid(), createdAt: Date.now(), voided: false,
    odds: $('s-odds').value.trim() || null,
    bookmaker: $('s-book').value.trim() || null,
    stake: $('s-stake').value ? Number($('s-stake').value) : null,
    game: $('s-game').value.trim() || null,
  };
  if (straightBetType === 'prop') {
    const player = $('s-player').value.trim();
    const target = Number($('s-target').value);
    if (!player) { errEl.textContent = 'Enter a player name.'; errEl.style.display = 'block'; return; }
    if (!(target > 0)) { errEl.textContent = 'Enter a target greater than 0.'; errEl.style.display = 'block'; return; }
    state.straightBets.push({
      ...shared, kind: 'prop', player,
      team: $('s-team').value.trim().toUpperCase() || null,
      opponent: $('s-opponent').value.trim().toUpperCase() || null,
      market: $('s-market').value, target, direction: $('s-direction').value,
      // Canonical ESPN athlete id when the player was picked from autocomplete —
      // null for free-typed text (resolved by name/team matching at evaluation
      // time instead, same as before).
      playerId: $('s-player').dataset.playerId || null,
    });
  } else {
    const teamAbbr = $('t-team').value.trim().toUpperCase();
    const mt = $('t-markettype').value;
    if (!teamAbbr) { errEl.textContent = 'Enter a team.'; errEl.style.display = 'block'; return; }
    if (mt !== 'moneyline' && !$('t-line').value) { errEl.textContent = 'Enter a line.'; errEl.style.display = 'block'; return; }
    state.straightBets.push({
      ...shared, kind: 'team', marketType: mt, teamAbbr,
      oppAbbr: $('t-opponent').value.trim().toUpperCase() || null,
      line: $('t-line').value ? Number($('t-line').value) : null,
      direction: mt === 'moneyline' ? 'team' : $('t-direction').value,
    });
  }
  saveState();
  e.target.reset();
  $('t-direction-field').classList.remove('hide');
  renderAll();
  refresh(); // a newly added bet may need a game/box score we haven't fetched yet
});

// ---------------------------------------------------------------------------
// Parlay builder
// ---------------------------------------------------------------------------
let pendingLegs = [];
let legType = 'prop';
document.querySelectorAll('[data-legtype]').forEach(btn => {
  btn.addEventListener('click', () => {
    legType = btn.dataset.legtype;
    document.querySelectorAll('[data-legtype]').forEach(b => b.classList.toggle('active', b === btn));
    $('legPropFields').classList.toggle('hide', legType !== 'prop');
    $('legTeamFields').classList.toggle('hide', legType !== 'team');
  });
});
setupPlayerAutocomplete($('l-player'), $('l-team'));
setupTeamAutocomplete($('lt-team'));

$('addLegBtn').addEventListener('click', () => {
  const errEl = $('parlayErr'); errEl.style.display = 'none';
  let leg;
  if (legType === 'prop') {
    const player = $('l-player').value.trim();
    const target = Number($('l-target').value);
    if (!player || !(target > 0)) { errEl.textContent = 'Enter a player and a target for this leg.'; errEl.style.display = 'block'; return; }
    leg = {
      legId: uid(), kind: 'prop', player, voided: false,
      team: $('l-team').value.trim().toUpperCase() || null,
      opponent: $('l-opponent').value.trim().toUpperCase() || null,
      market: $('l-market').value, target, direction: $('l-direction').value,
      odds: $('leg-odds').value.trim() || null,
      playerId: $('l-player').dataset.playerId || null,
    };
    $('l-player').value = ''; $('l-team').value = ''; $('l-opponent').value = ''; $('l-target').value = ''; $('l-player').dataset.playerId = '';
  } else {
    const teamAbbr = $('lt-team').value.trim().toUpperCase();
    const mt = $('lt-markettype').value;
    if (!teamAbbr) { errEl.textContent = 'Enter a team for this leg.'; errEl.style.display = 'block'; return; }
    leg = {
      legId: uid(), kind: 'team', marketType: mt, teamAbbr, voided: false,
      oppAbbr: $('lt-opponent').value.trim().toUpperCase() || null,
      line: $('lt-line').value ? Number($('lt-line').value) : null,
      direction: mt === 'moneyline' ? 'team' : $('lt-direction').value,
      odds: $('leg-odds').value.trim() || null,
    };
    $('lt-team').value = ''; $('lt-opponent').value = ''; $('lt-line').value = '';
  }
  $('leg-odds').value = '';
  pendingLegs.push(leg);
  renderPendingLegs();
});
function renderPendingLegs() {
  const el = $('pendingLegs');
  if (!pendingLegs.length) { el.innerHTML = ''; return; }
  el.innerHTML = pendingLegs.map(leg => {
    const label = selectionLabel(leg);
    return `<div class="leg-row"><span>${esc(label)}${leg.odds ? ' · ' + esc(leg.odds) : ''}</span><button type="button" class="danger" data-rmleg="${leg.legId}" style="padding:3px 9px;font-size:11px">Remove</button></div>`;
  }).join('');
  el.querySelectorAll('[data-rmleg]').forEach(b => b.addEventListener('click', () => {
    pendingLegs = pendingLegs.filter(l => l.legId !== b.dataset.rmleg);
    renderPendingLegs();
  }));
}
$('createParlayBtn').addEventListener('click', () => {
  const errEl = $('parlayErr'); errEl.style.display = 'none';
  const name = $('p-name').value.trim() || `Parlay ${new Date().toLocaleDateString()}`;
  if (pendingLegs.length < 2) { errEl.textContent = 'Add at least two legs to make a parlay.'; errEl.style.display = 'block'; return; }
  const parlayCreatedAt = Date.now();
  state.parlays.push({
    id: uid(), name, legs: pendingLegs.map(l => migrateLeg(l, parlayCreatedAt)), createdAt: parlayCreatedAt,
    combinedOdds: $('p-combined-odds').value.trim() || null,
    stake: $('p-stake').value ? Number($('p-stake').value) : null,
    potentialPayout: $('p-payout').value ? Number($('p-payout').value) : null,
  });
  saveState();
  pendingLegs = [];
  renderPendingLegs();
  $('p-name').value = ''; $('p-combined-odds').value = ''; $('p-stake').value = ''; $('p-payout').value = '';
  renderAll();
  refresh();
});

// ---------------------------------------------------------------------------
// Parlay-level aggregation — shared by Parlays tab and Overview
// ---------------------------------------------------------------------------
function parlayOverallStatus(p) {
  if (p.manualStatus) return p.manualStatus; // ticket-level manual correction always wins
  const statuses = p.legs.map(l => evaluateSelection(l).status);
  const nonVoid = statuses.filter(s => s !== 'void'); // a voided leg drops out, like a push
  if (!nonVoid.length) return 'void'; // every leg voided
  const mapped = nonVoid.map(s => s === 'target_reached_live' ? 'live' : s); // not won until the leg itself is settled
  return evaluateParlay(mapped); // 'won'|'lost'|'push'|'live'|'needs_review'
}
function parlayOverallPill(overall) {
  return {
    won: { txt: 'TICKET WON', cls: 'hit' },
    lost: { txt: 'TICKET LOST', cls: 'miss' },
    push: { txt: 'PUSH', cls: 'push' },
    void: { txt: 'VOID', cls: 'void' },
    live: { txt: 'LIVE', cls: 'live' },
    needs_review: { txt: 'Needs review', cls: 'review' },
  }[overall] || { txt: overall, cls: '' };
}

// ---------------------------------------------------------------------------
// LADDER CHALLENGES — core data operations (ladder.js has the pure accounting
// math; everything here is just wiring it to the app's tickets/storage).
// ---------------------------------------------------------------------------
function findTicket(ticketType, ticketId) {
  return ticketType === 'straight' ? state.straightBets.find(b => b.id === ticketId) : state.parlays.find(p => p.id === ticketId);
}
function ticketStakeOddsPayout(ticket) {
  // Straight bets and parlays both carry stake/odds/potentialPayout under slightly
  // different field names — normalized here once for the Add-to-Ladder prefill.
  if (ticket.kind) return { stake: ticket.stake, odds: ticket.odds, payout: null }; // straight bet
  return { stake: ticket.stake, odds: ticket.combinedOdds, payout: ticket.potentialPayout }; // parlay
}
function createLadder({ name, startingAmount, goalAmount, progressionMode, allOrNothing, notes, targetDate }) {
  const ladder = {
    id: uid(), name: name || `Ladder ${new Date().toLocaleDateString()}`,
    createdAt: Date.now(), startingAmount: Number(startingAmount) || 0, goalAmount: Number(goalAmount) || 0,
    progressionMode: progressionMode || 'manual', allOrNothing: !!allOrNothing, notes: notes || null,
    targetDate: targetDate || null, status: 'open', steps: [],
  };
  state.ladders.push(ladder);
  saveState();
  return ladder;
}
// Links an EXISTING straight bet or parlay to a ladder as its next step — never
// creates a duplicate bet, and never lets the same ticket be linked twice (its
// stake is counted once, on one ladder, no matter how many tabs show the ticket).
function linkTicketToLadder(ticketType, ticketId, ladderId, stepFields) {
  const ticket = findTicket(ticketType, ticketId);
  const ladder = state.ladders.find(l => l.id === ladderId);
  if (!ticket || !ladder) return { ok: false, reason: 'Ticket or ladder not found.' };
  if (ticket.ladderId) return { ok: false, reason: 'This ticket is already linked to a ladder — unlink it first.' };
  const step = {
    id: uid(), stepNum: ladder.steps.length + 1, ticketType, ticketId,
    stake: Number(stepFields.stake) || 0, americanOdds: stepFields.americanOdds || null,
    enteredPayout: (stepFields.enteredPayout !== '' && stepFields.enteredPayout != null) ? Number(stepFields.enteredPayout) : null,
    actualGrossPayout: null, result: 'pending', grossPayout: null, netProfit: null, estimated: false,
    createdAt: Date.now(), settledAt: null,
  };
  ladder.steps.push(step);
  ticket.ladderId = ladder.id;
  ticket.ladderStepId = step.id;
  saveState();
  return { ok: true, ladder, step };
}
function unlinkTicketFromLadder(ticketType, ticketId) {
  const ticket = findTicket(ticketType, ticketId);
  if (!ticket || !ticket.ladderId) return { ok: false, reason: 'Not linked to a ladder.' };
  const ladder = state.ladders.find(l => l.id === ticket.ladderId);
  if (!ladder) { ticket.ladderId = null; ticket.ladderStepId = null; saveState(); return { ok: true }; }
  const step = ladder.steps.find(s => s.id === ticket.ladderStepId);
  if (step && step.result && step.result !== 'pending') {
    return { ok: false, reason: `This step already settled (${step.result.toUpperCase()}) and is part of the ladder's permanent history — it can't be unlinked. You can still delete the whole ladder if you created it by mistake.` };
  }
  ladder.steps = ladder.steps.filter(s => s.id !== ticket.ladderStepId);
  ladder.steps.forEach((s, i) => { s.stepNum = i + 1; });
  ticket.ladderId = null; ticket.ladderStepId = null;
  saveState();
  return { ok: true };
}
function deleteLadder(ladderId) {
  const ladder = state.ladders.find(l => l.id === ladderId);
  if (!ladder) return;
  // Clear the link on every ticket that pointed here (the tickets themselves are
  // never touched/deleted — only the ladder record and its link disappear).
  [...state.straightBets, ...state.parlays].forEach(t => { if (t.ladderId === ladderId) { t.ladderId = null; t.ladderStepId = null; } });
  state.ladders = state.ladders.filter(l => l.id !== ladderId);
  saveState();
}

// --- "Add to Ladder" control, rendered on every straight bet row/card and every
// parlay ticket header. Never silently links — always shows which ladder was
// picked and lets the user confirm/edit the stake, odds and expected payout first.
function ladderControlsHtml(ticketType, ticketId, ticket) {
  const key = `${ticketType}:${ticketId}`;
  if (ticket.ladderId) {
    const ladder = state.ladders.find(l => l.id === ticket.ladderId);
    return `<span class="ladder-ctrl" data-tickettype="${ticketType}" data-ticketid="${esc(ticketId)}">
      <span class="ladder-link-badge">🪜 ${esc(ladder ? ladder.name : 'ladder')}</span>
      <button type="button" class="void-btn" data-ladderaction="unlink">Unlink</button>
    </span>`;
  }
  return `<span class="ladder-ctrl" data-tickettype="${ticketType}" data-ticketid="${esc(ticketId)}">
    <button type="button" class="secondary" style="font-size:11px;padding:3px 9px" data-ladderaction="open">🪜 Add to Ladder</button>
    <div class="addladder-panel hide" data-panel></div>
  </span>`;
}
function buildAddLadderPanel(panelEl, ticketType, ticketId) {
  const ticket = findTicket(ticketType, ticketId);
  const { stake, odds, payout } = ticketStakeOddsPayout(ticket);
  // Every open ladder is listed by its own real, user-given name — never a generic
  // "most recent ladder" label standing in for it. When there's more than one open
  // ladder, the dropdown starts on an explicit "— Choose a ladder —" placeholder
  // rather than silently defaulting to whichever happens to be newest; with exactly
  // one open ladder (no ambiguity to silently resolve) it's fine to preselect it.
  // "Create a new ladder" is its own clearly separate option, never confused with
  // picking among the existing ones.
  const openLadders = state.ladders.filter(l => l.status === 'open').sort((a, b) => b.createdAt - a.createdAt);
  const needsExplicitChoice = openLadders.length > 1;
  const options = [
    needsExplicitChoice ? `<option value="" selected disabled>— Choose a ladder —</option>` : '',
    ...openLadders.map(l => `<option value="${esc(l.id)}">${esc(l.name)} (balance $${fmt(computeLadderBalance(l))})</option>`),
    `<option value="__new__">＋ Create a new ladder</option>`,
  ].filter(Boolean).join('');
  // Odds/stake/payout are prefilled straight from the ticket itself (parlay legs use
  // the ticket's own COMBINED odds, never one leg's odds — ticketStakeOddsPayout()
  // above already picks the right field per ticket type) so the only things genuinely
  // missing — and the only things this form actually asks for — are which ladder this
  // step belongs to and, for a brand-new ladder, its goal. Nothing here is ever
  // silently changed later without the user seeing it: editing a field just changes
  // what the next Review step shows, and Back always returns to these exact values.
  panelEl.innerHTML = `
    <div class="ladder-form-fields">
      <div class="formgrid">
        <div class="field full"><label>Which ladder?</label><select data-f="ladderChoice">${options || '<option value="__new__">＋ Create a new ladder</option>'}</select></div>
      </div>
      <div class="new-ladder-fields${openLadders.length ? ' hide' : ''}">
        <div class="formgrid">
          <div class="field full"><label>New ladder name</label><input data-f="newName" placeholder="e.g. $50 to $500"></div>
          <div class="field"><label>Starting amount</label><input data-f="newStart" type="number" step="0.01" value="${fmt(stake || 0)}"></div>
          <div class="field"><label>Goal amount</label><input data-f="newGoal" type="number" step="0.01" placeholder="500"></div>
          <div class="field"><label>Progression</label><select data-f="newMode"><option value="reinvest">Reinvest full return</option><option value="stakeOnly">Reinvest stake only</option><option value="manual">Manual every step</option></select></div>
        </div>
      </div>
      <div class="formgrid" style="margin-top:8px">
        <div class="field"><label>Stake for this step</label><input data-f="stake" type="number" step="0.01" value="${fmt(stake || 0)}"></div>
        <div class="field"><label>American odds</label><input data-f="odds" value="${esc(odds || '')}"></div>
        <div class="field"><label>Expected payout (optional)</label><input data-f="payout" type="number" step="0.01" value="${payout != null ? fmt(payout) : ''}"></div>
      </div>
      <p class="hint">This step's result will only be filled in once this ticket's own result is confirmed final — adding it to a ladder never changes its status.</p>
      <div class="btnrow"><button type="button" class="primary" data-ladderaction="review-link">Review &amp; Add Step</button><button type="button" data-ladderaction="cancel-link">Cancel</button></div>
    </div>
    <div class="ladder-review hide" data-review></div>
  `;
  const choiceSel = panelEl.querySelector('[data-f="ladderChoice"]');
  const newFields = panelEl.querySelector('.new-ladder-fields');
  choiceSel.addEventListener('change', () => newFields.classList.toggle('hide', choiceSel.value !== '__new__'));
  if (choiceSel.value === '__new__') newFields.classList.remove('hide');
}
document.addEventListener('click', e => {
  const btn = e.target.closest('[data-ladderaction]');
  if (!btn) return;
  const ctrl = btn.closest('.ladder-ctrl');
  if (!ctrl) return;
  const ticketType = ctrl.dataset.tickettype, ticketId = ctrl.dataset.ticketid;
  const action = btn.dataset.ladderaction;
  if (action === 'unlink') {
    if (!confirm('Remove this ticket from its ladder?')) return;
    const r = unlinkTicketFromLadder(ticketType, ticketId);
    if (!r.ok) alert(r.reason);
    renderAll();
    return;
  }
  if (action === 'open') {
    const panel = ctrl.querySelector('[data-panel]');
    const nowHidden = panel.classList.contains('hide');
    document.querySelectorAll('.addladder-panel').forEach(p => p.classList.add('hide'));
    if (nowHidden) { buildAddLadderPanel(panel, ticketType, ticketId); panel.classList.remove('hide'); }
    return;
  }
  if (action === 'cancel-link') { ctrl.querySelector('[data-panel]').classList.add('hide'); return; }
  if (action === 'back-link') {
    const panel = ctrl.querySelector('[data-panel]');
    panel.querySelector('.ladder-form-fields').classList.remove('hide');
    panel.querySelector('[data-review]').classList.add('hide');
    return;
  }
  if (action === 'review-link') {
    const panel = ctrl.querySelector('[data-panel]');
    const get = f => panel.querySelector(`[data-f="${f}"]`)?.value ?? '';
    const ladderChoice = get('ladderChoice');
    if (ladderChoice === '') { alert('Choose a ladder first (or pick "＋ Create a new ladder").'); return; }
    let ladderName, ladderGoalCheck = true;
    if (ladderChoice === '__new__') {
      ladderName = get('newName') || `Ladder ${new Date().toLocaleDateString()}`;
      if (!(Number(get('newGoal')) > 0)) { alert('Enter a goal amount for the new ladder.'); return; }
    } else {
      const existing = state.ladders.find(l => l.id === ladderChoice);
      ladderName = existing ? existing.name : '(unknown ladder)';
    }
    const stake = Number(get('stake')) || 0;
    const odds = get('odds') || null;
    const payoutRaw = get('payout');
    const est = estimateGrossPayout(stake, odds, payoutRaw);
    // Expected RETURN is the total that would come back on a win (stake included).
    // Expected PROFIT is return minus the stake. Neither is the ACTUAL settled
    // return — that only exists once the ticket's own result is confirmed final and
    // the step actually settles (shown later as "WON +$X" on the ladder itself, never
    // confused with this pre-settlement estimate).
    const expectedReturn = est.value;
    const expectedProfit = expectedReturn != null ? Math.round((expectedReturn - stake) * 100) / 100 : null;
    panel.querySelector('.ladder-form-fields').classList.add('hide');
    const review = panel.querySelector('[data-review]');
    review.innerHTML = `
      <div class="review-box">
        <p><b>Ladder:</b> ${esc(ladderName)}${ladderChoice === '__new__' ? ' (new)' : ''}</p>
        <p><b>Stake for this step:</b> $${fmt(stake)}</p>
        <p><b>Odds:</b> ${esc(odds || '—')}</p>
        <p><b>Expected return if won</b> (stake + profit, ${est.estimated ? 'estimated from odds' : 'from the entered payout'}): ${expectedReturn != null ? '$' + fmt(expectedReturn) : '—'}</p>
        <p><b>Expected profit if won</b> (return minus stake): ${expectedProfit != null ? '$' + fmt(expectedProfit) : '—'}</p>
        <p class="hint">This is an estimate for planning only — the ladder will only ever record the ACTUAL settled return once this ticket's result is confirmed final.</p>
      </div>
      <div class="btnrow"><button type="button" class="primary" data-ladderaction="confirm-link">Confirm &amp; Add Step</button><button type="button" data-ladderaction="back-link">Back</button></div>
    `;
    review.classList.remove('hide');
    return;
  }
  if (action === 'confirm-link') {
    const panel = ctrl.querySelector('[data-panel]');
    const get = f => panel.querySelector(`[data-f="${f}"]`)?.value ?? '';
    let ladderId = get('ladderChoice');
    if (ladderId === '__new__') {
      const name = get('newName'), start = get('newStart'), goal = get('newGoal'), mode = get('newMode');
      if (!(Number(goal) > 0)) { alert('Enter a goal amount for the new ladder.'); return; }
      const ladder = createLadder({ name, startingAmount: Number(start) || 0, goalAmount: Number(goal), progressionMode: mode, allOrNothing: true });
      ladderId = ladder.id;
    }
    const stepFields = { stake: get('stake'), americanOdds: get('odds') || null, enteredPayout: get('payout') };
    const r = linkTicketToLadder(ticketType, ticketId, ladderId, stepFields);
    if (!r.ok) { alert(r.reason); return; }
    renderAll();
  }
});

// ---------------------------------------------------------------------------
// 30-minute archival (archive.js) — persisted per-ticket, survives reload/close.
// ---------------------------------------------------------------------------
// Updates straightBet/parlay.finalizedAt in place (persisted) and returns whether
// the ticket is currently archived (i.e. should be hidden from Overview's active
// sections and shown only in Bet History). Never deletes anything — archiving is
// purely a display concern.
function syncFinalization(ticket, gameStates) {
  const now = Date.now();
  ticket.finalizedAt = computeFinalizedAt(gameStates, ticket.finalizedAt, now);
  return isArchived(ticket.finalizedAt, now);
}
function isTicketArchived(ticket) { return isArchived(ticket.finalizedAt, Date.now()); }

// ---------------------------------------------------------------------------
// Ladder sync — advances a ladder step ONLY once its linked ticket's canonical
// result is actually final (never from a live/"reached" status), using the exact
// same evaluateSelection()/parlayOverallStatus() this whole app uses everywhere
// else. This is the direct fix for "advance a ladder only after confirmed result."
// ---------------------------------------------------------------------------
function canonicalTicketResultForLadder(ticketType, ticketId) {
  if (ticketType === 'straight') {
    const bet = state.straightBets.find(b => b.id === ticketId);
    if (!bet) return 'unmatched';
    return evaluateSelection(bet).status; // 'won'|'lost'|'push'|'void'|'upcoming'|'live'|'target_reached_live'|'unmatched'
  }
  const p = state.parlays.find(x => x.id === ticketId);
  if (!p) return 'unmatched';
  return parlayOverallStatus(p); // 'won'|'lost'|'push'|'void'|'live'|'needs_review'
}
function syncLadders() {
  let changed = false;
  state.ladders.forEach(ladder => {
    (ladder.steps || []).forEach(step => {
      if (step.result && step.result !== 'pending') return; // already settled — never re-settle
      const canonical = canonicalTicketResultForLadder(step.ticketType, step.ticketId);
      const settleable = canonical === 'won' || canonical === 'lost' || canonical === 'push' || canonical === 'void';
      if (!settleable) return; // still live/upcoming/needs-review — balance must not move yet
      const settled = settleStep(step, canonical, step.actualGrossPayout ?? null);
      Object.assign(step, settled, { settledAt: Date.now() });
      changed = true;
    });
    const prevStatus = ladder.status;
    ladder.status = deriveLadderStatus(ladder);
    if (ladder.status !== prevStatus) changed = true;
  });
  if (changed) saveState();
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
// Every straight bet / leg gets a stable `_uid` (not persisted) purely so the shared
// card renderer can bind its Remove/Void buttons without re-deriving an identity.
function tagUids() {
  state.straightBets.forEach(b => {
    b._uid = 'straight:' + b.id;
    if (b.gameId === undefined) migrateBindingFields(b); // bets added this session (forms, OCR import)
  });
  state.parlays.forEach(p => p.legs.forEach(l => {
    l._uid = 'leg:' + p.id + ':' + l.legId;
    // A leg's game-plausibility window is anchored to its ticket's creation time
    // (covers legs added this session, e.g. via screenshot import, before any reload).
    if (l.createdAt === undefined || l.createdAt === null) l.createdAt = p.createdAt ?? null;
    if (l.gameId === undefined) migrateBindingFields(l);
  }));
}

function runFinalizationSweep() {
  state.straightBets.forEach(b => { b._archived = syncFinalization(b, gameStatesForStraight(b)); });
  state.parlays.forEach(p => { p._archived = syncFinalization(p, gameStatesForParlay(p)); });
  saveState();
}
function renderAll() {
  tagUids();
  runSettlementSweep(); // v6.1: bind + persist results BEFORE archival/ladder logic reads them
  runFinalizationSweep();
  syncLadders();
  renderOverview();
  renderStraightTable();
  renderParlayList();
  renderPlayerPropsBoard();
  renderTeamMarketsBoard();
  renderLadders();
  renderHistory();
  renderRosterStatus();
  renderSettingsTab();
}

// --- Overview: the unified command center -----------------------------------
let ovFilter = 'all', ovTypeFilter = 'all', ovSearch = '';

function allSelectionsFlat() {
  // One flat list of every tracked selection, straight AND parlay legs alike,
  // each carrying its evaluation + a pointer back to its parent ticket (if any).
  const out = [];
  state.straightBets.forEach(b => out.push({ sel: b, parent: null, type: b.kind === 'prop' ? 'straightProp' : 'straightTeam' }));
  state.parlays.forEach(p => p.legs.forEach(l => out.push({ sel: l, parent: p, type: 'parlay' })));
  return out.map(x => ({ ...x, ev: evaluateSelection(x.sel) }));
}

function renderOverview() {
  // Archived tickets (final for 30+ minutes) live only in Bet History — Overview's
  // active sections never show them, per the 30-minute rule.
  const flat = allSelectionsFlat().filter(x => !(x.parent ? x.parent._archived : x.sel._archived));
  const activeStraights = state.straightBets.filter(b => !b._archived);
  const activeParlays = state.parlays.filter(p => !p._archived);
  const activeTickets = activeParlays.filter(p => !isSettled(parlayOverallStatus(p))).length;
  const activeLegs = flat.filter(x => !isSettled(x.ev.status)).length;
  const hitting = flat.filter(x => x.ev.status === 'target_reached_live').length;
  const settledWins = activeStraights.filter(b => evaluateSelection(b).status === 'won').length
    + activeParlays.filter(p => parlayOverallStatus(p) === 'won').length;
  const settledLosses = activeStraights.filter(b => evaluateSelection(b).status === 'lost').length
    + activeParlays.filter(p => parlayOverallStatus(p) === 'lost').length;
  const needsReview = flat.filter(x => x.ev.status === 'unmatched').length
    + activeParlays.filter(p => parlayOverallStatus(p) === 'needs_review').length;
  const openLadderCount = state.ladders.filter(l => l.status === 'open').length;

  $('ov-active-tickets').textContent = activeTickets;
  $('ov-active-legs').textContent = activeLegs;
  $('ov-hitting').textContent = hitting;
  $('ov-wins').textContent = settledWins;
  $('ov-losses').textContent = settledLosses;
  $('ov-review').textContent = needsReview;
  $('ov-ladders').textContent = openLadderCount;

  renderGameCenter();

  // Build render groups: straight selections render as plain cards; parlays render
  // as one parent ticket with every leg nested beneath it using the SAME card renderer.
  // Both exclude archived tickets (those only live in Bet History now).
  const straightItems = flat.filter(x => !x.parent);
  const parlayItems = activeParlays.map(p => ({ p, legs: p.legs.map(l => ({ sel: l, ev: evaluateSelection(l) })), overall: parlayOverallStatus(p) }));

  function passesFilters(status, type) {
    if (ovTypeFilter !== 'all' && type !== ovTypeFilter) return false;
    if (ovFilter === 'all') return true;
    if (ovFilter === 'live') return status === 'live' || status === 'target_reached_live';
    if (ovFilter === 'pregame') return status === 'upcoming';
    if (ovFilter === 'hitting') return status === 'target_reached_live';
    if (ovFilter === 'settled') return isSettled(status);
    if (ovFilter === 'review') return status === 'unmatched';
    return true;
  }
  function matchesSearch(text) { return !ovSearch || norm(text).includes(norm(ovSearch)); }

  // LIVE & ACTIVE: anything not upcoming and not settled, plus parlays still live/needs_review.
  const liveStraight = straightItems.filter(x => !isSettled(x.ev.status) && x.ev.status !== 'upcoming' && passesFilters(x.ev.status, x.type) && matchesSearch(selectionLabel(x.sel)));
  const liveParlays = parlayItems.filter(pi => !isSettled(pi.overall) && !pi.legs.every(l => l.ev.status === 'upcoming') && passesFilters(pi.overall === 'needs_review' ? 'unmatched' : pi.overall, 'parlay') && matchesSearch(pi.p.name));
  $('ov-live-section').innerHTML = renderOverviewSection([...liveStraight.map(x => ({ kind: 'single', x })), ...liveParlays.map(pi => ({ kind: 'parlay', pi }))], 'Nothing live or active right now.');

  // UPCOMING
  const upStraight = straightItems.filter(x => x.ev.status === 'upcoming' && passesFilters('upcoming', x.type) && matchesSearch(selectionLabel(x.sel)));
  const upParlays = parlayItems.filter(pi => pi.legs.length && pi.legs.every(l => l.ev.status === 'upcoming') && passesFilters('upcoming', 'parlay') && matchesSearch(pi.p.name));
  $('ov-upcoming-section').innerHTML = renderOverviewSection([...upStraight.map(x => ({ kind: 'single', x })), ...upParlays.map(pi => ({ kind: 'parlay', pi }))], 'Nothing upcoming.');

  // NEEDS REVIEW
  const reviewStraight = straightItems.filter(x => x.ev.status === 'unmatched' && matchesSearch(selectionLabel(x.sel)));
  const reviewParlays = parlayItems.filter(pi => pi.overall === 'needs_review' && matchesSearch(pi.p.name));
  if (!reviewStraight.length && !reviewParlays.length) {
    $('ov-review-section').innerHTML = '<div class="empty">Nothing needs review right now.</div>';
  } else {
    $('ov-review-section').innerHTML = [
      ...reviewStraight.map(x => `<div class="leg-row"><span>${esc(selectionLabel(x.sel))} — couldn't be matched to a live game/player. Check the team spelling, or the game hasn't tipped off / isn't on today's slate.</span></div>`),
      ...reviewParlays.map(pi => `<div class="leg-row"><span>Parlay "${esc(pi.p.name)}" has a leg that couldn't be matched — open the Parlays tab to see which one and correct it.</span></div>`),
    ].join('');
  }

  // RECENTLY SETTLED — still shown here for 30 minutes after a ticket's games are
  // all final; after that it's archived (moved to Bet History only, never deleted).
  const settledStraight = straightItems.filter(x => isSettled(x.ev.status) && matchesSearch(selectionLabel(x.sel)));
  const settledParlays = parlayItems.filter(pi => isSettled(pi.overall) && matchesSearch(pi.p.name));
  if (!settledStraight.length && !settledParlays.length) {
    $('ov-settled-section').innerHTML = '<div class="empty">Nothing settled yet.</div>';
  } else {
    const countdown = ticket => { const m = minutesRemaining(ticket.finalizedAt, Date.now()); return m === null ? '' : ` <small class="subtle">(moves to Bet History in ${m} min)</small>`; };
    const rows = [
      ...settledStraight.map(x => {
        const info = statusDisplay(x.ev);
        return `<div class="leg-row"><span>${esc(selectionLabel(x.sel))}${countdown(x.sel)}</span><span class="pill ${info.cls}">${esc(info.txt)}</span></div>`;
      }),
      ...settledParlays.map(pi => {
        const pill = parlayOverallPill(pi.overall);
        return `<div class="leg-row"><span>Parlay "${esc(pi.p.name)}" (${pi.legs.length} legs)${countdown(pi.p)}</span><span class="pill ${pill.cls}">${esc(pill.txt)}</span></div>`;
      }),
    ];
    $('ov-settled-section').innerHTML = rows.join('');
  }

  // Bind void toggles for the straight-bet cards rendered in Live/Upcoming (parlay
  // legs are bound from renderParlayList instead, since Overview's parlay cards use
  // the no-action variant to keep the dashboard read-focused).
  liveStraight.concat(upStraight).forEach(x => bindSelectionCardActions($('ov-live-section').parentElement.parentElement.parentElement, x.sel, () => renderAll()));
}
// ---------------------------------------------------------------------------
// GAME CENTER — grouped scoreboard (Live / Upcoming / Final) with a clickable,
// inline-expanding box score per game. Expansion is a plain show/hide of a div
// right under the clicked card — never a navigation — so the page's scroll
// position is simply never disturbed, and "closing" just hides it again.
// ---------------------------------------------------------------------------
let gcExpandedGameId = null;
function renderGameCenter() {
  const liveGames = games.filter(g => g.state === 'in' || g.state === 'halftime');
  $('ov-live-count').textContent = games.length ? `${liveGames.length} live of ${games.length} (last 24 hrs)` : '—';
  if (!games.length) {
    $('ov-games').innerHTML = `<div class="empty">No games found in the last 24 hours, or the feed hasn't loaded yet.${isStaleFeed() ? ' (data feed looks stale — last success ' + new Date(lastSuccessAt).toLocaleTimeString() + ')' : ''}</div>`;
    return;
  }
  // "Final" is sorted most-recent-first, and spans up to the last ~24-30 hours (today
  // and yesterday's calendar days) rather than only today's slate — see refresh().
  const groups = [
    ['Live now', games.filter(g => g.state === 'in' || g.state === 'halftime')],
    ['Upcoming', games.filter(g => g.state === 'pre')],
    ['Final (last 24 hrs)', games.filter(g => g.state === 'post').sort((a, b) => (b.startTimestamp || 0) - (a.startTimestamp || 0))],
  ];
  $('ov-games').innerHTML = groups.filter(([, list]) => list.length).map(([title, list]) => `
    <div class="gc-group-title">${esc(title)}</div>
    <div class="gc-grid">${list.map(g => gameCenterCardHtml(g)).join('')}</div>
  `).join('') || '<div class="empty">No games found in the last 24 hours.</div>';
  $('ov-games').querySelectorAll('[data-gccard]').forEach(card => card.addEventListener('click', () => {
    gcExpandedGameId = (gcExpandedGameId === card.dataset.gccard) ? null : card.dataset.gccard;
    renderGameCenter();
  }));
  $('ov-games').querySelectorAll('[data-gcclose]').forEach(btn => btn.addEventListener('click', (e) => {
    e.stopPropagation();
    gcExpandedGameId = null;
    renderGameCenter();
  }));
}
function gameCenterCardHtml(g) {
  const statusPill = g.state === 'in' ? 'live' : g.state === 'halftime' ? 'half' : g.state === 'post' ? 'final' : 'wait';
  const expanded = gcExpandedGameId === g.id;
  return `<div class="gc-card" data-gccard="${esc(g.id)}">
    <div class="gc-card-row"><b>${esc(g.away)} @ ${esc(g.home)}</b><span class="pill ${statusPill}">${esc(gameClockLabel(g))}</span></div>
    <div class="gc-card-row"><span>${esc(g.away)} ${esc(String(g.awayScore))}</span><span>${esc(g.home)} ${esc(String(g.homeScore))}</span></div>
    ${g.dayLabel ? `<div class="subtle" style="font-size:11px">${esc(g.dayLabel)}${g.startTime ? ' · ' + esc(g.startTime) : ''}</div>` : ''}
    ${g.feedError ? '<div class="subtle" style="font-size:11px">Box score unavailable this refresh</div>' : ''}
    <div class="subtle" style="font-size:11px">${expanded ? 'Click to close' : 'Click for box score'}</div>
  </div>${expanded ? `<div class="gc-detail">${gameCenterDetailHtml(g)}</div>` : ''}`;
}
function gameCenterDetailHtml(g) {
  if (g.state === 'pre') {
    return `<p>${esc(g.away)} @ ${esc(g.home)} — scheduled for ${esc(g.startTime || 'a time not yet confirmed')}. No live stats before tipoff.</p>`;
  }
  if (!g.players || !g.players.length) {
    // A finished game within the last ~24-30 hours always has its box score fetched
    // (see needsBoxScore()), regardless of tracked bets, so reaching this branch for
    // a "post" game means the fetch itself failed or hasn't completed yet this
    // refresh — not that it was skipped to save requests (that skip-to-save-requests
    // reason only still applies to a live/upcoming game with no tracked bet on it).
    const msg = g.feedError
      ? 'Box score unavailable this refresh — showing the last known score only. It will be retried automatically.'
      : (g.state === 'post'
        ? 'Box score hasn\'t loaded for this final game yet — it will load on the next refresh (every finished game from the last ~24 hours gets a full box score here, whether or not you have a bet tracked on it).'
        : 'No player box score loaded for this game yet (it may not have any tracked bets on it, so its box score wasn\'t fetched to save requests).');
    return `<p class="empty">${esc(msg)}</p>`;
  }
  const freshness = lastSuccessAt ? `Last successful update: ${new Date(lastSuccessAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })}${isStaleFeed() ? ' — STALE' : ''}` : 'Not yet updated';
  const teamsInGame = [...new Set(g.players.map(p => p.team))];
  const shoot = (s) => s && s.a ? `${s.m}/${s.a}` : '—';
  const rowHtml = (p) => `<tr class="${(p.minutes || 0) <= 0 ? 'gc-dnp' : ''}">
      <td>${esc(p.name)}</td>
      <td>${(p.minutes || 0) <= 0 ? 'DNP' : fmt(p.minutes)}</td>
      <td>${fmt(p.stats.PTS ?? 0)}</td><td>${fmt(p.stats.REB ?? 0)}</td><td>${fmt(p.stats.AST ?? 0)}</td>
      <td>${fmt(p.stats.STL ?? 0)}</td><td>${fmt(p.stats.BLK ?? 0)}</td><td>${fmt(p.stats.TO ?? 0)}</td><td>${fmt(p.stats.PF ?? 0)}</td>
      <td>${shoot(p.shooting.FG)} FG</td><td>${shoot(p.shooting['3P'])} 3P</td><td>${shoot(p.shooting.FT)} FT</td>
    </tr>`;
  const tableHead = '<tr><th>Player</th><th>MIN</th><th>PTS</th><th>REB</th><th>AST</th><th>STL</th><th>BLK</th><th>TO</th><th>PF</th><th>FG</th><th>3P</th><th>FT</th></tr>';
  const sections = teamsInGame.map(teamAbbr => {
    const roster = g.players.filter(p => p.team === teamAbbr);
    const totals = teamTotals(roster);
    const dnp = roster.filter(p => p.dnpReason || (p.minutes || 0) <= 0);
    const played = roster.filter(p => !(p.dnpReason || (p.minutes || 0) <= 0)).sort((a, b) => (b.minutes || 0) - (a.minutes || 0));
    // "starter" is ESPN's own box-score flag for who started the game (a roster fact,
    // not a live floor claim) — only split into Starters/Bench when the feed actually
    // supplies it for these players; otherwise show one plain "Players" list rather
    // than guessing who started.
    const haveStarterFlag = played.some(p => typeof p.starter === 'boolean');
    const starters = haveStarterFlag ? played.filter(p => p.starter === true) : [];
    const bench = haveStarterFlag ? played.filter(p => p.starter !== true) : played;
    const group = (label, rows) => rows.length
      ? `<div class="gc-subhead">${esc(label)}</div><table class="gc-boxtable"><thead>${tableHead}</thead><tbody>${rows.map(rowHtml).join('')}</tbody></table>` : '';
    const sectionsHtml = [
      haveStarterFlag ? group('Starters', starters) : '',
      group(haveStarterFlag ? 'Bench' : 'Players', bench),
      group('DNP / Not suited up', dnp),
    ].filter(Boolean).join('');
    return `<div class="gc-team-head">${teamLogoHtml(teamAbbr)} ${esc(teamAbbr)} team totals: ${totals.PTS} PTS · ${totals.REB} REB · ${totals.AST} AST · ${totals.FG.a ? totals.FG.m + '/' + totals.FG.a + ' FG' : '—'} · ${totals['3P'].a ? totals['3P'].m + '/' + totals['3P'].a + ' 3P' : '—'} · ${totals.FT.a ? totals.FT.m + '/' + totals.FT.a + ' FT' : '—'}</div>${sectionsHtml}`;
  }).join('');
  return `<button type="button" class="void-btn gc-detail-close" data-gcclose="${esc(g.id)}">Close</button>
    <p class="data-freshness">${esc(freshness)}. "Starters"/"Bench" reflect who started the game per the box score, not who is currently on the floor. "DNP / Not suited up" means 0 minutes recorded — a real number from the box score, not a guess.</p>
    ${sections}`;
}

function renderOverviewSection(entries, emptyMsg) {
  if (!entries.length) return `<div class="empty">${esc(emptyMsg)}</div>`;
  return entries.map(e => {
    if (e.kind === 'single') return renderSelectionCard(e.x.sel, { onToggleVoid: true, onLadder: true, onEdit: true });
    const { p, legs, overall } = e.pi;
    const pill = parlayOverallPill(overall);
    const reached = legs.filter(l => l.ev.status === 'target_reached_live').length;
    const lost = legs.filter(l => l.ev.status === 'lost').length;
    const settledCount = legs.filter(l => isSettled(l.ev.status)).length;
    return `<div class="ticket">
      <div class="ticket-head"><h3>${esc(p.name)}</h3><span class="pill ${pill.cls}">${esc(pill.txt)}</span></div>
      <div class="ticket-summary-row">
        <span>${legs.length} legs</span>
        ${p.combinedOdds ? `<span>Odds ${esc(p.combinedOdds)}</span>` : ''}
        ${p.stake ? `<span>Stake $${fmt(p.stake)}</span>` : ''}
        ${p.potentialPayout ? `<span>To win $${fmt(p.potentialPayout)}</span>` : ''}
        <span>${reached} reached target · ${lost} lost · ${settledCount}/${legs.length} settled</span>
        ${ladderControlsHtml('parlay', p.id, p)}
      </div>
      <div class="nested-legs">${legs.map(l => renderSelectionCard(l.sel, {})).join('')}</div>
    </div>`;
  }).join('');
}
$('ov-search') && $('ov-search').addEventListener('input', e => { ovSearch = e.target.value; renderOverview(); });
$('ov-sort') && $('ov-sort').addEventListener('change', renderOverview);
document.querySelectorAll('[data-ovfilter]').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('[data-ovfilter]').forEach(b => b.classList.toggle('active', b === btn));
  ovFilter = btn.dataset.ovfilter; renderOverview();
}));
document.querySelectorAll('[data-ovtype]').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('[data-ovtype]').forEach(b => b.classList.toggle('active', b === btn));
  ovTypeFilter = btn.dataset.ovtype; renderOverview();
}));

function renderStraightTable() {
  $('straightCount').textContent = state.straightBets.length;
  if (!state.straightBets.length) { $('straightTableWrap').innerHTML = '<div class="empty">No straight bets yet.</div>'; return; }
  const rows = state.straightBets.map(b => {
    const ev = evaluateSelection(b);
    const info = statusDisplay(ev);
    const label = b.kind === 'prop' ? `${esc(b.player)} (${esc(b.team || '—')})` : `${esc(b.teamAbbr)}`;
    const market = b.kind === 'prop' ? marketLabel(b.market) + ' ' + (b.direction === 'over' ? 'O' : 'U') + ' ' + fmt(b.target) : teamMarketLabel(b);
    return `<tr><td>${b.kind === 'prop' ? 'Player Prop' : 'Team Market'}</td><td>${label}</td><td>${esc(market)}</td><td>${esc(b.odds || '—')}${b.stake ? ' · $' + fmt(b.stake) : ''}</td><td><span class="pill ${info.cls}">${esc(info.txt)}</span></td><td>${ladderControlsHtml('straight', b.id, b)}</td><td><button class="secondary" data-editstraight="${b.id}" style="font-size:11px">Edit</button> <button class="void-btn" data-voidstraight="${b.id}" style="font-size:11px">${b.voided ? 'Un-void' : 'Void'}</button> <button class="danger" data-rmstraight="${b.id}" style="padding:4px 9px;font-size:11px">Remove</button></td></tr>`;
  }).join('');
  $('straightTableWrap').innerHTML = `<table><thead><tr><th>Type</th><th>Selection</th><th>Market</th><th>Odds / Stake</th><th>Status</th><th>Ladder</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  $('straightTableWrap').querySelectorAll('[data-rmstraight]').forEach(b => b.addEventListener('click', () => {
    state.straightBets = state.straightBets.filter(x => x.id !== b.dataset.rmstraight);
    saveState(); renderAll();
  }));
  $('straightTableWrap').querySelectorAll('[data-voidstraight]').forEach(b => b.addEventListener('click', () => {
    const bet = state.straightBets.find(x => x.id === b.dataset.voidstraight);
    if (bet) { bet.voided = !bet.voided; saveState(); renderAll(); }
  }));
  $('straightTableWrap').querySelectorAll('[data-editstraight]').forEach(b => b.addEventListener('click', () => {
    const bet = state.straightBets.find(x => x.id === b.dataset.editstraight);
    if (bet) openSelectionEditModal(bet, () => renderAll());
  }));
}

function renderParlayList() {
  $('parlayCount').textContent = state.parlays.length;
  if (!state.parlays.length) { $('parlayList').innerHTML = '<div class="empty">No parlays yet.</div>'; return; }
  // Every leg renders with the exact same rich card used on the Player Props / Team
  // Markets boards and on Overview — no separate, thinner leg-row template. This is
  // the direct fix for "a prop added inside a parlay only shows a generic LIVE badge".
  $('parlayList').innerHTML = state.parlays.map(p => {
    const legCards = p.legs.map(l => renderSelectionCard(l, { onToggleVoid: true, onEdit: true })).join('');
    const overall = parlayOverallStatus(p);
    const overallPill = parlayOverallPill(overall);
    return `<div class="ticket"><div class="ticket-head"><h3>${esc(p.name)}</h3><span class="pill ${overallPill.cls}">${esc(overallPill.txt)}</span></div>
      <div class="row" style="margin-bottom:8px">${p.combinedOdds ? `<span class="chip">Odds ${esc(p.combinedOdds)}</span>` : ''}${p.stake ? `<span class="chip">Stake $${fmt(p.stake)}</span>` : ''}${p.potentialPayout ? `<span class="chip">To win $${fmt(p.potentialPayout)}</span>` : ''}</div>
      <div class="nested-legs">${legCards}</div>
      <div class="btnrow">${ladderControlsHtml('parlay', p.id, p)}<button class="secondary" data-editparlay="${p.id}">Edit ticket</button><button class="danger" data-rmparlay="${p.id}" style="flex:0">Delete ticket</button></div>
    </div>`;
  }).join('');
  $('parlayList').querySelectorAll('[data-rmparlay]').forEach(b => b.addEventListener('click', () => {
    if (!confirm('Delete this parlay ticket?')) return;
    state.parlays = state.parlays.filter(x => x.id !== b.dataset.rmparlay);
    saveState(); renderAll();
  }));
  $('parlayList').querySelectorAll('[data-editparlay]').forEach(b => b.addEventListener('click', () => {
    const p = state.parlays.find(x => x.id === b.dataset.editparlay);
    if (p) openParlayMetaEditModal(p, () => renderAll());
  }));
  state.parlays.forEach(p => p.legs.forEach(l => bindSelectionCardActions($('parlayList'), l, () => renderAll())));
}
// Edits the parlay TICKET's own fields (name/stake/combined odds/potential payout)
// — distinct from editing one of its individual legs (handled by the shared
// selection-edit modal above, via each leg's own "Edit" button).
function openParlayMetaEditModal(p, afterChange) {
  const before = { ...p };
  openEditModal({
    title: 'Edit parlay ticket',
    hint: 'Corrects the ticket\'s own name, stake, combined odds, or potential payout. To fix an individual leg (player, line, odds…), use that leg\'s own "Edit" button instead.',
    fields: [
      { key: 'name', label: 'Parlay name', value: p.name, full: true, required: true },
      { key: 'combinedOdds', label: 'Combined odds (American)', value: p.combinedOdds },
      { key: 'stake', label: 'Stake', type: 'number', value: p.stake },
      { key: 'potentialPayout', label: 'Potential payout', type: 'number', value: p.potentialPayout },
    ],
    onSave: (values) => {
      p.name = values.name;
      p.combinedOdds = values.combinedOdds || null;
      p.stake = (values.stake === null || values.stake === undefined) ? null : Number(values.stake);
      p.potentialPayout = (values.potentialPayout === null || values.potentialPayout === undefined) ? null : Number(values.potentialPayout);
      logEdit('parlay', p.id, editDiffSummary(before, values));
      saveState();
      afterChange();
    },
  });
}

function renderPlayerPropsBoard() {
  const props = state.straightBets.filter(b => b.kind === 'prop');
  $('pp-total').textContent = props.length;
  $('pp-live').textContent = games.filter(g => g.state === 'in' || g.state === 'halftime').length;
  const filter = $('pp-filter').value;
  let hits = 0, need = 0;
  const groups = {};
  props.forEach(p => {
    const ev = evaluateSelection(p);
    if (ev.status === 'target_reached_live' || ev.status === 'won') hits++;
    else if (ev.g && (ev.g.state === 'in' || ev.g.state === 'halftime') && ev.current !== null && ((p.direction === 'over' && ev.current < p.target) || (p.direction === 'under' && ev.current >= p.target))) need++;
    const key = ev.g ? (ev.g.away + ' @ ' + ev.g.home) : (p.game || 'Unmatched / upcoming');
    if (filter === 'live' && !(ev.g?.state === 'in' || ev.g?.state === 'halftime')) return;
    if (filter === 'hit' && !(ev.status === 'target_reached_live' || ev.status === 'won')) return;
    if (filter === 'pending' && !(ev.status === 'upcoming' || ev.status === 'unmatched')) return;
    (groups[key] ??= []).push(p);
  });
  $('pp-hit').textContent = hits; $('pp-need').textContent = need;
  const keys = Object.keys(groups);
  if (!keys.length) { $('pp-board').innerHTML = `<div class="empty"><strong>${props.length ? 'No props in this view.' : 'No player props yet.'}</strong>${props.length ? ' Change the filter.' : ' Add one from the Straight Bets tab.'}</div>`; return; }
  $('pp-board').innerHTML = keys.map(key => {
    const rows = groups[key];
    const firstEv = evaluateSelection(rows[0]);
    const meta = firstEv.g ? `${firstEv.g.awayScore} – ${firstEv.g.homeScore} · ${gameClockLabel(firstEv.g)}` : (rows[0].game || 'Waiting for a matching game');
    return `<div class="game-section"><div class="game-head"><div class="game-title">${esc(key)}</div><div class="game-meta">${esc(meta)}</div></div><div class="cards">${rows.map(p => renderSelectionCard(p, { onToggleVoid: true, onRemove: true, onLadder: true, onEdit: true })).join('')}</div></div>`;
  }).join('');
  props.forEach(p => bindSelectionCardActions($('pp-board'), p, (action) => {
    if (action === 'remove') { state.straightBets = state.straightBets.filter(x => x.id !== p.id); saveState(); }
    renderAll();
  }));
}

function renderTeamMarketsBoard() {
  const bets = state.straightBets.filter(b => b.kind === 'team');
  $('tm-total').textContent = bets.length;
  $('tm-live').textContent = games.filter(g => g.state === 'in' || g.state === 'halftime').length;
  let hit = 0, live = 0;
  bets.forEach(b => {
    const ev = evaluateSelection(b);
    if (ev.status === 'target_reached_live' || ev.status === 'won') hit++;
    if (ev.status === 'live' || ev.status === 'target_reached_live') live++;
  });
  $('tm-hit').textContent = hit; $('tm-need').textContent = live;
  $('tm-board').innerHTML = bets.length
    ? `<div class="cards">${bets.map(b => renderSelectionCard(b, { onToggleVoid: true, onRemove: true, onLadder: true, onEdit: true })).join('')}</div>`
    : '<div class="empty">No team-market bets yet. Add one from the Straight Bets tab.</div>';
  bets.forEach(b => bindSelectionCardActions($('tm-board'), b, (action) => {
    if (action === 'remove') { state.straightBets = state.straightBets.filter(x => x.id !== b.id); saveState(); }
    renderAll();
  }));
}

// ---------------------------------------------------------------------------
// LADDER CHALLENGES tab
// ---------------------------------------------------------------------------
const LADDER_STATUS_PILL = {
  open: { txt: 'OPEN', cls: 'live' }, won: { txt: 'WON', cls: 'hit' }, lost: { txt: 'LOST', cls: 'miss' },
  paused: { txt: 'PAUSED', cls: 'wait' }, cancelled: { txt: 'CANCELLED', cls: 'void' },
};
function ladderStepTicketLabel(step) {
  const t = findTicket(step.ticketType, step.ticketId);
  if (t) return step.ticketType === 'straight' ? selectionLabel(t) : `Parlay "${t.name}" (${t.legs.length} legs)`;
  // The underlying ticket may have been removed via "Clear History" — a minimal
  // label snapshot (taken at removal time) keeps this step readable for
  // reconciliation even though the live ticket record is gone.
  return step.ticketLabelSnapshot ? `${step.ticketLabelSnapshot} (removed from History)` : '(ticket no longer exists)';
}
function renderLadders() {
  $('ladderCount').textContent = state.ladders.length;
  if (!state.ladders.length) { $('ladderList').innerHTML = '<div class="empty">No ladders yet. Create one, or use "Add to Ladder" on a straight bet or parlay.</div>'; return; }
  $('ladderList').innerHTML = [...state.ladders].sort((a, b) => b.createdAt - a.createdAt).map(l => {
    const pill = LADDER_STATUS_PILL[l.status] || { txt: l.status, cls: '' };
    const balance = computeLadderBalance(l);
    const pl = totalRealizedPL(l);
    const pct = progressPct(l);
    const needed = amountNeededForGoal(l);
    const nextStake = suggestedNextStake(l);
    const stepsHtml = l.steps.length ? l.steps.map(s => {
      const resPill = { won: { txt: 'WON', cls: 'hit' }, lost: { txt: 'LOST', cls: 'miss' }, push: { txt: 'PUSH', cls: 'push' }, void: { txt: 'VOID', cls: 'void' }, pending: { txt: 'PENDING', cls: 'wait' } }[s.result] || { txt: s.result, cls: '' };
      return `<div class="ladder-step-row">
        <span class="ladder-step-num">#${s.stepNum}</span>
        <span style="flex:1">${esc(ladderStepTicketLabel(s))} · stake $${fmt(s.stake)}${s.americanOdds ? ' @ ' + esc(s.americanOdds) : ''}</span>
        <span>${s.result === 'won' ? `+$${fmt(s.netProfit)}${s.estimated ? '<span class="ladder-estimated-tag">(estimated)</span>' : ''}` : (s.result === 'lost' ? `-$${fmt(s.stake)}` : '')}</span>
        <span class="pill ${resPill.cls}">${esc(resPill.txt)}</span>
        <button type="button" class="secondary" data-editstep="${l.id}:${s.id}" style="font-size:11px;padding:3px 8px">Edit</button>
      </div>`;
    }).join('') : '<div class="subtle" style="font-size:12px">No steps yet — use "Add to Ladder" on a straight bet or parlay.</div>';
    return `<div class="ladder-card">
      <div class="ladder-head"><h3>${esc(l.name)}</h3><span class="pill ${pill.cls}">${esc(pill.txt)}</span></div>
      <div class="ladder-meta-row">
        <span>Start $${fmt(l.startingAmount)} → Goal $${fmt(l.goalAmount)}</span>
        <span>Balance: $${fmt(balance)}</span>
        <span>Realized P/L: ${pl >= 0 ? '+' : ''}$${fmt(pl)}</span>
        <span>Needed to goal: $${fmt(needed)}</span>
        <span>Mode: ${{ reinvest: 'Reinvest full return', stakeOnly: 'Reinvest stake only', manual: 'Manual' }[l.progressionMode] || l.progressionMode}</span>
        ${nextStake !== null ? `<span>Suggested next stake: $${fmt(nextStake)} (confirm when you add the next step)</span>` : ''}
        ${l.targetDate ? `<span>Target date: ${esc(l.targetDate)}</span>` : ''}
      </div>
      <div class="ladder-progress-bar"><div class="fill" style="width:${pct}%"></div></div>
      ${l.notes ? `<p class="hint" style="margin:4px 0 10px">${esc(l.notes)}</p>` : ''}
      <div class="ladder-steps">${stepsHtml}</div>
      <div class="btnrow" style="margin-top:10px">
        <button type="button" class="secondary" data-ladderedit="${l.id}">Edit ladder</button>
        ${l.status === 'open' ? `<button type="button" class="secondary" data-ladderpause="${l.id}">Pause</button>` : ''}
        ${l.status === 'paused' ? `<button type="button" class="secondary" data-ladderresume="${l.id}">Resume</button>` : ''}
        ${(l.status === 'open' || l.status === 'paused') ? `<button type="button" class="secondary" data-laddercancel="${l.id}">Cancel</button>` : ''}
        <button type="button" class="danger" data-ladderdelete="${l.id}">Delete ladder</button>
      </div>
    </div>`;
  }).join('');
  $('ladderList').querySelectorAll('[data-ladderpause]').forEach(b => b.addEventListener('click', () => { const l = state.ladders.find(x => x.id === b.dataset.ladderpause); if (l) { l.status = 'paused'; saveState(); renderAll(); } }));
  $('ladderList').querySelectorAll('[data-ladderresume]').forEach(b => b.addEventListener('click', () => { const l = state.ladders.find(x => x.id === b.dataset.ladderresume); if (l) { l.status = 'open'; saveState(); renderAll(); } }));
  $('ladderList').querySelectorAll('[data-laddercancel]').forEach(b => b.addEventListener('click', () => {
    if (!confirm('Cancel this ladder? Its history is kept, and any linked tickets stay exactly as they are — only the ladder itself stops being Open.')) return;
    const l = state.ladders.find(x => x.id === b.dataset.laddercancel); if (l) { l.status = 'cancelled'; saveState(); renderAll(); }
  }));
  $('ladderList').querySelectorAll('[data-ladderdelete]').forEach(b => b.addEventListener('click', () => {
    if (!confirm('Delete this ladder entirely? Linked tickets are NOT deleted — they just become unlinked. This cannot be undone.')) return;
    deleteLadder(b.dataset.ladderdelete); renderAll();
  }));
  $('ladderList').querySelectorAll('[data-ladderedit]').forEach(b => b.addEventListener('click', () => {
    const l = state.ladders.find(x => x.id === b.dataset.ladderedit);
    if (l) openLadderMetaEditModal(l, () => renderAll());
  }));
  $('ladderList').querySelectorAll('[data-editstep]').forEach(b => b.addEventListener('click', () => {
    const [ladderId, stepId] = b.dataset.editstep.split(':');
    const l = state.ladders.find(x => x.id === ladderId);
    const s = l && l.steps.find(x => x.id === stepId);
    if (l && s) openLadderStepEditModal(l, s, () => renderAll());
  }));
}
// Edits the LADDER's own fields (name, starting/goal amount, progression mode,
// all-or-nothing policy, notes, target date) — never touches its steps or any
// linked ticket. Changing the starting/goal amount immediately recalculates the
// displayed balance/progress/needed-to-goal, since those are always computed
// live from the ladder's current fields, never cached.
function openLadderMetaEditModal(l, afterChange) {
  const before = { ...l };
  openEditModal({
    title: 'Edit ladder',
    hint: 'Corrects the ladder\'s own name, amounts, or settings. Its steps and any linked tickets are untouched by this — edit a step directly (its own "Edit" button) to correct a stake/odds mistake on one step.',
    fields: [
      { key: 'name', label: 'Ladder name', value: l.name, full: true, required: true },
      { key: 'startingAmount', label: 'Starting amount ($)', type: 'number', value: l.startingAmount, required: true },
      { key: 'goalAmount', label: 'Goal amount ($)', type: 'number', value: l.goalAmount, required: true },
      { key: 'progressionMode', label: 'Progression mode', type: 'select', value: l.progressionMode, options: [
        { value: 'reinvest', label: 'Reinvest full settled return each step' },
        { value: 'stakeOnly', label: 'Reinvest only the original stake' },
        { value: 'manual', label: 'Manual — I pick the stake every step' },
      ] },
      { key: 'allOrNothing', label: 'If a step loses…', type: 'select', value: String(l.allOrNothing), options: [
        { value: 'true', label: 'End the challenge (mark LOST) — classic ladder' },
        { value: 'false', label: "Keep it open — track balance, don't auto-end" },
      ] },
      { key: 'targetDate', label: 'Target date (optional)', type: 'date', value: l.targetDate },
      { key: 'notes', label: 'Notes (optional)', value: l.notes, full: true },
    ],
    onSave: (values) => {
      l.name = values.name;
      l.startingAmount = Number(values.startingAmount) || 0;
      l.goalAmount = Number(values.goalAmount) || 0;
      l.progressionMode = values.progressionMode;
      l.allOrNothing = values.allOrNothing === 'true';
      l.targetDate = values.targetDate || null;
      l.notes = values.notes || null;
      logEdit('ladder', l.id, editDiffSummary(before, values));
      saveState();
      afterChange();
    },
  });
}
// Edits one STEP's own stake/odds/entered-payout. If the step has already
// settled, its payout/profit is recomputed via the exact same settleStep() the
// rest of the app uses — never a second accounting formula — so the ladder's
// running balance reflects the correction immediately rather than going stale.
function openLadderStepEditModal(ladder, step, afterChange) {
  const before = { ...step };
  const alreadySettled = step.result && step.result !== 'pending';
  openEditModal({
    title: 'Edit ladder step',
    hint: alreadySettled
      ? `This step already settled as ${step.result.toUpperCase()}. Saving a change here recomputes its payout/profit from the new numbers — the ladder's balance updates to match.`
      : 'Corrects this step\'s stake, odds, or entered payout before it settles.',
    fields: [
      { key: 'stake', label: 'Stake', type: 'number', value: step.stake, required: true },
      { key: 'americanOdds', label: 'Odds (American, optional)', value: step.americanOdds },
      { key: 'enteredPayout', label: 'Entered payout (optional — overrides the odds-based estimate)', type: 'number', value: step.enteredPayout },
    ],
    onSave: (values) => {
      step.stake = Number(values.stake) || 0;
      step.americanOdds = values.americanOdds || null;
      step.enteredPayout = (values.enteredPayout === null || values.enteredPayout === undefined) ? null : Number(values.enteredPayout);
      if (alreadySettled) {
        const resettled = settleStep(step, step.result, step.actualGrossPayout ?? null);
        Object.assign(step, resettled);
        ladder.status = deriveLadderStatus(ladder);
      }
      logEdit('ladderStep', step.id, editDiffSummary(before, values));
      saveState();
      afterChange();
    },
  });
}
$('createLadderBtn').addEventListener('click', () => {
  const errEl = $('ladderErr'); errEl.style.display = 'none';
  const name = $('ld-name').value.trim();
  const start = Number($('ld-start').value), goal = Number($('ld-goal').value);
  if (!(goal > 0)) { errEl.textContent = 'Enter a goal amount greater than 0.'; errEl.style.display = 'block'; return; }
  createLadder({
    name, startingAmount: start || 0, goalAmount: goal,
    progressionMode: $('ld-mode').value, allOrNothing: $('ld-allornothing').value === 'true',
    notes: $('ld-notes').value.trim() || null, targetDate: $('ld-targetdate').value || null,
  });
  $('ld-name').value = ''; $('ld-start').value = ''; $('ld-goal').value = ''; $('ld-notes').value = ''; $('ld-targetdate').value = '';
  renderAll();
});

// ---------------------------------------------------------------------------
// BET HISTORY tab — the permanent record of every straight bet and parlay ever
// created (archived or not). Overview only shows the active subset; this shows
// everything, with filters, search, manual correction, and a CSV export.
// ---------------------------------------------------------------------------
let histFilter = 'all', histSearch = '';
let histSelected = new Set(); // "ticketType:ticketId" keys currently checked for removal

// Removes tickets from History (and from the Straight Bets/Parlays tabs, since this
// app shows every ticket ever created there with no archived filter — "History" and
// "the permanent record" are the same underlying records). ONLY ever removes a ticket
// that is already fully archived — which this app only ever marks true once every one
// of its games is confirmed final AND 30 minutes have passed (see archive.js) — so an
// active/live/needs-review ticket can never be removed this way, and any ladder step
// it was linked to is, by construction, already settled (syncLadders() settles a step
// the moment its ticket's canonical result is final, well before the 30-minute archival
// delay elapses) — so removing it here can never affect ladder accounting.
function removeHistoryTickets(keys) {
  let removed = 0, skipped = 0;
  keys.forEach(key => {
    const [tType, tId] = key.split(':');
    const ticket = findTicket(tType, tId);
    if (!ticket || !ticket._archived) { skipped++; return; } // safety net, see above
    if (ticket.ladderId) {
      const ladder = state.ladders.find(l => l.id === ticket.ladderId);
      const step = ladder && ladder.steps.find(s => s.id === ticket.ladderStepId);
      if (step) step.ticketLabelSnapshot = tType === 'straight' ? selectionLabel(ticket) : `Parlay "${ticket.name}" (${ticket.legs.length} legs)`;
    }
    if (tType === 'straight') state.straightBets = state.straightBets.filter(x => x.id !== tId);
    else state.parlays = state.parlays.filter(x => x.id !== tId);
    removed++;
  });
  saveState();
  return { removed, skipped };
}
function historyStatusBucket(status) {
  if (status === 'won') return 'won';
  if (status === 'lost') return 'lost';
  if (status === 'push' || status === 'void') return 'pushvoid';
  if (status === 'unmatched' || status === 'needs_review') return 'review';
  return 'open'; // upcoming / live / target_reached_live
}
function renderHistory() {
  const rows = [
    ...state.straightBets.map(b => ({ ticketType: 'straight', ticket: b, status: evaluateSelection(b).status, label: selectionLabel(b) })),
    ...state.parlays.map(p => ({ ticketType: 'parlay', ticket: p, status: parlayOverallStatus(p), label: `Parlay "${p.name}" (${p.legs.length} legs)` })),
  ].sort((a, b) => b.ticket.createdAt - a.ticket.createdAt);
  const filtered = rows.filter(r => (histFilter === 'all' || historyStatusBucket(r.status) === histFilter) && (!histSearch || norm(r.label).includes(norm(histSearch))));
  $('histCount').textContent = `${filtered.length} / ${rows.length}`;
  // Only archived (fully settled + 30-min-elapsed) tickets are ever eligible for
  // removal — an active/live/needs-review ticket never gets a checkbox at all.
  const archivedKeysInView = filtered.filter(r => r.ticket._archived).map(r => `${r.ticketType}:${r.ticket.id}`);
  for (const key of [...histSelected]) { if (!archivedKeysInView.includes(key) && !rows.some(r => `${r.ticketType}:${r.ticket.id}` === key && r.ticket._archived)) histSelected.delete(key); }
  const selCount = histSelected.size;
  $('histToolbar').innerHTML = `
    <span class="subtle">${selCount} selected</span>
    <button type="button" class="secondary" id="histExportFirstBtn">⬇ Export Backup First</button>
    <button type="button" class="danger" id="histRemoveSelectedBtn" ${selCount ? '' : 'disabled'}>Remove Selected (${selCount})</button>
    <button type="button" class="danger" id="histClearAllBtn">Clear All History</button>
  `;
  $('histExportFirstBtn').addEventListener('click', exportBackupFile);
  $('histRemoveSelectedBtn').addEventListener('click', () => {
    if (!histSelected.size) return;
    if (!confirm(`Remove ${histSelected.size} record(s) from History? This cannot be undone (your Export Backup file, if you made one, still has them). Active bets, open parlays, ladders, and settlement accounting are never affected — only these archived, already-settled tickets.`)) return;
    const { removed } = removeHistoryTickets([...histSelected]);
    histSelected.clear();
    renderAll();
    $('histStatusMsg').textContent = `Removed ${removed} record(s) from History.`;
    setTimeout(() => { if ($('histStatusMsg')) $('histStatusMsg').textContent = ''; }, 4000);
  });
  $('histClearAllBtn').addEventListener('click', () => {
    const allArchivedKeys = [
      ...state.straightBets.filter(b => b._archived).map(b => `straight:${b.id}`),
      ...state.parlays.filter(p => p._archived).map(p => `parlay:${p.id}`),
    ];
    if (!allArchivedKeys.length) { $('histStatusMsg').textContent = 'There is no archived history to clear.'; return; }
    if (!confirm(`Clear ALL History? This will permanently remove ${allArchivedKeys.length} archived/settled record(s) — this cannot be undone. Still-active bets, open parlays, and all ladder accounting are never touched. Consider clicking "Export Backup First" before continuing.`)) return;
    const { removed } = removeHistoryTickets(allArchivedKeys);
    histSelected.clear();
    renderAll();
    $('histStatusMsg').textContent = `Cleared ${removed} archived record(s) from History.`;
    setTimeout(() => { if ($('histStatusMsg')) $('histStatusMsg').textContent = ''; }, 4000);
  });
  if (!filtered.length) { $('historyList').innerHTML = '<div class="empty">No tickets match this filter.</div>'; return; }
  $('historyList').innerHTML = filtered.map(r => {
    const info = r.ticketType === 'straight' ? statusDisplay(evaluateSelection(r.ticket)) : parlayOverallPill(r.status);
    const ladder = r.ticket.ladderId ? state.ladders.find(l => l.id === r.ticket.ladderId) : null;
    const created = new Date(r.ticket.createdAt).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
    const correctId = `${r.ticketType}:${r.ticket.id}`;
    const checkboxHtml = r.ticket._archived
      ? `<input type="checkbox" class="hist-check" data-histcheck="${esc(correctId)}" ${histSelected.has(correctId) ? 'checked' : ''} title="Select for removal (only archived/settled tickets can be removed)">`
      : `<span class="hist-check-spacer" title="Still active — not eligible for History removal"></span>`;
    return `<div class="history-row">
      ${checkboxHtml}
      <div class="hr-main">
        <div>${esc(r.label)}</div>
        <div class="hr-meta">Created ${esc(created)}${r.ticket._archived ? ' · archived' : ''}${ladder ? ' · 🪜 ' + esc(ladder.name) : ''}${r.ticket.manualStatus ? ' · manually corrected' : ''}</div>
      </div>
      <span class="pill ${info.cls}">${esc(info.txt)}</span>
      <button type="button" class="secondary" data-histedit="${esc(correctId)}" style="font-size:11px">Edit</button>
      <select data-histcorrect="${esc(correctId)}" style="font-size:11px">
        <option value="">Correct result…</option>
        <option value="won">Force WON</option>
        <option value="lost">Force LOST</option>
        <option value="push">Force PUSH</option>
        <option value="void">Force VOID</option>
        <option value="__clear__">Clear correction</option>
      </select>
    </div>`;
  }).join('');
  $('historyList').querySelectorAll('[data-histcheck]').forEach(cb => cb.addEventListener('change', () => {
    if (cb.checked) histSelected.add(cb.dataset.histcheck); else histSelected.delete(cb.dataset.histcheck);
    renderHistory();
  }));
  // Retroactively edit a past ticket's own recorded facts (name, stake, odds,
  // line…) right from History — including an already-archived one. This is the
  // same edit modal/engine used on the Straight Bets, Parlays, and Ladders tabs;
  // History is just another place to reach the SAME underlying ticket object.
  $('historyList').querySelectorAll('[data-histedit]').forEach(b => b.addEventListener('click', () => {
    const [tType, tId] = b.dataset.histedit.split(':');
    const ticket = findTicket(tType, tId);
    if (!ticket) return;
    if (tType === 'straight') openSelectionEditModal(ticket, () => renderAll());
    else openParlayMetaEditModal(ticket, () => renderAll());
  }));
  $('historyList').querySelectorAll('[data-histcorrect]').forEach(sel => sel.addEventListener('change', () => {
    const [tType, tId] = sel.dataset.histcorrect.split(':');
    const ticket = findTicket(tType, tId);
    if (!ticket || !sel.value) { sel.value = ''; return; }
    const newVal = sel.value === '__clear__' ? null : sel.value;
    const from = ticket.manualStatus || 'computed';
    if (newVal && !confirm(`Force this ticket's result to ${newVal.toUpperCase()}? This overrides the computed status everywhere (Overview, History, and its ladder if linked) and is recorded in the audit log.`)) { sel.value = ''; return; }
    ticket.manualStatus = newVal;
    state.auditLog.push({ ts: Date.now(), ticketType: tType, ticketId: tId, from, to: newVal || 'computed', note: 'manual correction via Bet History' });
    saveState();
    renderAll();
  }));
}
$('hist-search') && $('hist-search').addEventListener('input', e => { histSearch = e.target.value; renderHistory(); });
document.querySelectorAll('[data-histfilter]').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('[data-histfilter]').forEach(b => b.classList.toggle('active', b === btn));
  histFilter = btn.dataset.histfilter; renderHistory();
}));
$('histExportBtn').addEventListener('click', () => {
  // v6.1: each row also carries WHICH game it was settled against (ESPN event id + ET
  // game date), when it was settled, and whether the result is automatic or a manual
  // correction — so an exported ledger can be audited and used for calibration.
  const ladderName = t => t.ladderId ? (state.ladders.find(l => l.id === t.ladderId)?.name || '') : '';
  const iso = ms => (ms ? new Date(ms).toISOString() : '');
  const uniq = arr => [...new Set(arr.filter(Boolean))].join(' | ');
  const rows = [
    ...state.straightBets.map(b => ({
      type: 'Straight', label: selectionLabel(b), status: evaluateSelection(b).status, stake: b.stake, odds: b.odds, created: iso(b.createdAt), ladder: ladderName(b),
      gameId: b.settlement?.gameId || b.gameId || '', gameDate: b.settlement?.gameDate || b.gameDate || '', settledAt: iso(b.settlement?.settledAt),
      source: b.manualStatus ? 'manual correction' : (b.settlement ? 'auto (final box score)' : ''),
    })),
    ...state.parlays.map(p => {
      const legSettled = p.legs.filter(l => !l.voided).map(l => l.settlement?.settledAt).filter(Boolean);
      const allSettled = p.legs.every(l => l.voided || l.settlement);
      return {
        type: 'Parlay', label: `${p.name} (${p.legs.length} legs)`, status: parlayOverallStatus(p), stake: p.stake, odds: p.combinedOdds, created: iso(p.createdAt), ladder: ladderName(p),
        gameId: uniq(p.legs.map(l => l.settlement?.gameId || l.gameId)), gameDate: uniq(p.legs.map(l => l.settlement?.gameDate || l.gameDate)),
        settledAt: allSettled && legSettled.length ? iso(Math.max(...legSettled)) : '',
        source: p.manualStatus ? 'manual correction' : (allSettled ? 'auto (final box score)' : ''),
      };
    }),
  ];
  const header = ['Type', 'Selection', 'Status', 'Stake', 'Odds', 'Created', 'Ladder', 'Game ID', 'Game date (ET)', 'Settled at', 'Result source'];
  const csv = [header.join(',')].concat(rows.map(r => [r.type, r.label, r.status, r.stake ?? '', r.odds ?? '', r.created, r.ladder, r.gameId, r.gameDate, r.settledAt, r.source].map(v => `"${String(v).replace(/"/g, '""')}"`).join(','))).join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `nba-prop-command-center-history-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
});

// ---------------------------------------------------------------------------
// Export / Import backup
// ---------------------------------------------------------------------------
function exportBackupFile() {
  const blob = new Blob([JSON.stringify(state, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = `NBA-Prop-Command-Center-Backup-${new Date().toISOString().slice(0, 10)}.json`;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}
$('exportBtn').addEventListener('click', exportBackupFile);

// ---------------------------------------------------------------------------
// Backup validation + Merge/Replace import. Validates the WHOLE file before
// changing anything (so a malformed file never partially applies and leaves
// inconsistent data), carries every record type over — straight bets, parlays
// (with legs), ladders (with steps), the audit log, and odds history, where the
// old handler silently dropped ladders and the audit log entirely — and offers
// a real choice between Merge (dedup by each record's own stable id, never
// duplicates, never touches anything already here unless it's a genuinely new
// id) and Replace All (explicit confirmation, explained as an overwrite).
// ---------------------------------------------------------------------------
function validateBackupShape(parsed) {
  const errors = [];
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) { errors.push('the file is not a valid backup (expected a JSON object, not a list or plain value)'); return errors; }
  if (!Array.isArray(parsed.straightBets)) errors.push('missing or invalid "straightBets" list');
  if (!Array.isArray(parsed.parlays)) errors.push('missing or invalid "parlays" list');
  if (parsed.ladders !== undefined && !Array.isArray(parsed.ladders)) errors.push('"ladders" is present but is not a list');
  if (parsed.auditLog !== undefined && !Array.isArray(parsed.auditLog)) errors.push('"auditLog" is present but is not a list');
  if (parsed.oddsHistory !== undefined && (typeof parsed.oddsHistory !== 'object' || Array.isArray(parsed.oddsHistory))) errors.push('"oddsHistory" is present but is not an object');
  if (errors.length) return errors; // don't bother spot-checking records if the top-level shape is already wrong
  const badBet = (parsed.straightBets || []).find(b => !b || typeof b !== 'object' || !b.id);
  if (badBet) errors.push('one or more straight-bet records is missing its id field');
  const badParlay = (parsed.parlays || []).find(p => !p || typeof p !== 'object' || !p.id || !Array.isArray(p.legs));
  if (badParlay) errors.push('one or more parlay records is missing its id or its legs list');
  const badLadder = (parsed.ladders || []).find(l => !l || typeof l !== 'object' || !l.id || !Array.isArray(l.steps));
  if (badLadder) errors.push('one or more ladder records is missing its id or its steps list');
  return errors;
}
// Adds every record from `parsed` that isn't already present on this device (matched by
// each record's own stable id), and for a ladder that already exists here, merges in only
// the steps it doesn't already have — the ladder's own fields (name, status, goal, etc.) on
// THIS device are never overwritten by an incoming copy. Returns a plain-language summary.
function mergeBackupIntoState(parsed) {
  const migratedBets = (parsed.straightBets || []).map(migrateSelection);
  const migratedParlays = (parsed.parlays || []).map(p => migrateSelection({ ...p, legs: (p.legs || []).map(l => migrateLeg(l, p.createdAt)) }));
  const migratedLadders = (parsed.ladders || []).map(migrateLadder);
  const incomingAudit = parsed.auditLog || [];
  const incomingOdds = parsed.oddsHistory || {};

  const counts = { addedBets: 0, skippedBets: 0, addedParlays: 0, skippedParlays: 0, addedLadders: 0, mergedLadders: 0, addedSteps: 0, skippedSteps: 0, addedAudit: 0, skippedAudit: 0 };

  const existingBetIds = new Set(state.straightBets.map(b => b.id));
  migratedBets.forEach(b => { if (existingBetIds.has(b.id)) counts.skippedBets++; else { state.straightBets.push(b); existingBetIds.add(b.id); counts.addedBets++; } });

  const existingParlayIds = new Set(state.parlays.map(p => p.id));
  migratedParlays.forEach(p => { if (existingParlayIds.has(p.id)) counts.skippedParlays++; else { state.parlays.push(p); existingParlayIds.add(p.id); counts.addedParlays++; } });

  const ladderById = new Map(state.ladders.map(l => [l.id, l]));
  migratedLadders.forEach(incoming => {
    const existing = ladderById.get(incoming.id);
    if (!existing) { state.ladders.push(incoming); ladderById.set(incoming.id, incoming); counts.addedLadders++; return; }
    counts.mergedLadders++;
    const existingStepIds = new Set(existing.steps.map(s => s.id));
    (incoming.steps || []).forEach(s => { if (existingStepIds.has(s.id)) counts.skippedSteps++; else { existing.steps.push(s); existingStepIds.add(s.id); counts.addedSteps++; } });
  });

  // Audit-log entries have no id of their own — key on their actual content instead.
  const auditKey = a => `${a.ts}|${a.ticketType}|${a.ticketId}|${a.from}|${a.to}|${a.note}`;
  const existingAuditKeys = new Set(state.auditLog.map(auditKey));
  incomingAudit.forEach(a => { const k = auditKey(a); if (existingAuditKeys.has(k)) counts.skippedAudit++; else { state.auditLog.push(a); existingAuditKeys.add(k); counts.addedAudit++; } });

  Object.keys(incomingOdds).forEach(k => { if (!(k in state.oddsHistory)) state.oddsHistory[k] = incomingOdds[k]; });

  return counts;
}
function summarizeMergeCounts(c) {
  const parts = [];
  parts.push(`${c.addedBets} new straight bet(s) added${c.skippedBets ? `, ${c.skippedBets} already present (skipped)` : ''}`);
  parts.push(`${c.addedParlays} new parlay(s) added${c.skippedParlays ? `, ${c.skippedParlays} already present (skipped)` : ''}`);
  parts.push(`${c.addedLadders} new ladder(s) added, ${c.mergedLadders} existing ladder(s) merged (${c.addedSteps} new step(s) added${c.skippedSteps ? `, ${c.skippedSteps} already present` : ''})`);
  if (c.addedAudit || c.skippedAudit) parts.push(`${c.addedAudit} audit-log entr${c.addedAudit === 1 ? 'y' : 'ies'} added${c.skippedAudit ? `, ${c.skippedAudit} already present` : ''}`);
  return parts.join('. ') + '.';
}
function closeImportModal() { $('importModalOverlay').classList.add('hide'); $('importModalBox').innerHTML = ''; }
function showImportChoiceModal(parsed, fileName) {
  const counts = { bets: parsed.straightBets.length, parlays: parsed.parlays.length, ladders: (parsed.ladders || []).length };
  $('importModalBox').innerHTML = `
    <h3>Import backup: ${esc(fileName)}</h3>
    <p>This file contains ${counts.bets} straight bet(s), ${counts.parlays} parlay(s), and ${counts.ladders} ladder(s).</p>
    <p><b>Merge (recommended)</b> adds anything in this file that isn't already on this device and leaves everything already here untouched — safe to import the same backup more than once, nothing gets duplicated.</p>
    <p><b>Replace All Data</b> deletes everything currently saved on this device first, then loads only what's in this file. This cannot be undone unless you have another backup of what's currently here.</p>
    <div class="btnrow">
      <button class="secondary" id="importCancelBtn">Cancel</button>
      <button class="danger" id="importReplaceBtn">Replace All Data</button>
      <button class="primary" id="importMergeBtn">Merge</button>
    </div>`;
  $('importModalOverlay').classList.remove('hide');
  $('importCancelBtn').onclick = closeImportModal;
  $('importMergeBtn').onclick = () => {
    const counts2 = mergeBackupIntoState(parsed);
    saveState();
    renderAll();
    closeImportModal();
    alert('Backup merged. ' + summarizeMergeCounts(counts2));
  };
  $('importReplaceBtn').onclick = () => {
    if (!confirm('This deletes ALL bets, parlays, ladders, and history currently saved on this device and replaces them with only what\'s in this backup file. This cannot be undone. Continue?')) return;
    state = buildStateFromRaw(parsed);
    saveState();
    renderAll();
    closeImportModal();
    alert('All data replaced with the contents of this backup.');
  };
}
$('importBtn').addEventListener('click', () => $('importFile').click());
$('importFile').addEventListener('change', async e => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const text = await file.text();
    let parsed;
    try { parsed = JSON.parse(text); } catch (e2) { throw new Error('this isn\'t valid JSON — the file may be corrupted or isn\'t actually a backup export'); }
    const errors = validateBackupShape(parsed);
    if (errors.length) throw new Error('this file doesn\'t look like a valid backup: ' + errors.join('; ') + '. Nothing was changed.');
    showImportChoiceModal(parsed, file.name);
  } catch (err) {
    alert('Could not import this file: ' + err.message);
  }
  e.target.value = '';
});
$('settingsExportBtn').addEventListener('click', exportBackupFile);
$('settingsImportBtn').addEventListener('click', () => $('importFile').click());

// ---------------------------------------------------------------------------
// Settings tab — a dedicated place for data management, storage/offline status,
// live-refresh status, appearance, and app info/integrity, so the main betting
// workflows (Overview, Straight Bets, etc.) stay uncluttered by this. Nothing here
// is a second source of truth: it only reads and displays state that already lives
// elsewhere (saveState()'s own status, refresh()'s own status, state itself), and
// the one action that can change anything — the integrity check — only ever reads.
// ---------------------------------------------------------------------------
function runDataIntegrityCheck() {
  const issues = [];
  const ladderById = id => state.ladders.find(l => l.id === id);
  function checkTicketLadderLink(kind, label, t) {
    if (!t.ladderId) return;
    const ladder = ladderById(t.ladderId);
    if (!ladder) { issues.push(`${kind} "${label}" links to a ladder that no longer exists (ladderId ${t.ladderId}).`); return; }
    if (t.ladderStepId && !ladder.steps.find(s => s.id === t.ladderStepId)) {
      issues.push(`${kind} "${label}" links to a step that no longer exists in ladder "${ladder.name}".`);
    }
  }
  // v6.1: a saved settlement must belong to the game the selection is bound to.
  function checkSettlementBinding(kind, label, s) {
    if (!s.settlement) return;
    if (!s.settlement.gameId) issues.push(`${kind} "${label}" has a saved result with no game id.`);
    else if (s.gameId && String(s.settlement.gameId) !== String(s.gameId)) issues.push(`${kind} "${label}" has a saved result from game ${s.settlement.gameId} but is bound to game ${s.gameId}.`);
  }
  state.straightBets.forEach((b, i) => {
    const label = b.player || b.teamAbbr || `#${i + 1}`;
    if (!b.id) issues.push(`A straight bet (${label}) is missing its id.`);
    checkTicketLadderLink('Straight bet', label, b);
    checkSettlementBinding('Straight bet', label, b);
  });
  state.parlays.forEach((p, i) => {
    const label = p.name || `#${i + 1}`;
    if (!p.id) issues.push(`A parlay (${label}) is missing its id.`);
    if (!Array.isArray(p.legs) || !p.legs.length) issues.push(`Parlay "${label}" has no legs.`);
    checkTicketLadderLink('Parlay', label, p);
    (p.legs || []).forEach((l, j) => checkSettlementBinding('Parlay leg', `${label} #${j + 1}`, l));
  });
  state.ladders.forEach(l => {
    if (!l.id) issues.push(`A ladder ("${l.name}") is missing its id.`);
    (l.steps || []).forEach((s, i) => {
      if (!s.id) issues.push(`Step #${i + 1} in ladder "${l.name}" is missing its id.`);
      if (!s.ticketType || !s.ticketId) { issues.push(`Step #${i + 1} in ladder "${l.name}" is missing its ticket reference.`); return; }
      const ticket = s.ticketType === 'straight' ? state.straightBets.find(b => b.id === s.ticketId) : state.parlays.find(p => p.id === s.ticketId);
      if (!ticket && !s.ticketLabelSnapshot) issues.push(`Step #${i + 1} in ladder "${l.name}" references a ticket that no longer exists and has no saved description — its history may be hard to read.`);
    });
  });
  return issues;
}
function renderSettingsTab() {
  const expBtn = $('settingsExportBtn');
  if (!expBtn) return; // settings tab not in this build / not yet in DOM
  $('settingsBackupCounts').textContent = `This backup would include ${state.straightBets.length} straight bet(s), ${state.parlays.length} parlay(s), ${state.ladders.length} ladder(s), and ${state.auditLog.length} audit-log entr${state.auditLog.length === 1 ? 'y' : 'ies'}.`;

  const storageBody = $('settingsStorageBody');
  const savedWhen = lastSaveAt ? new Date(lastSaveAt).toLocaleString() : 'never (not yet saved this session)';
  storageBody.innerHTML = `
    <p><b>Status:</b> ${lastSaveError ? `<span style="color:var(--bad)">Save failed — ${esc(lastSaveError)}</span>` : '<span style="color:var(--win)">Saving normally</span>'}</p>
    <p><b>Last successful save:</b> ${esc(savedWhen)}</p>
    <p><b>Offline app shell:</b> ${('serviceWorker' in navigator) ? (location.protocol === 'file:' ? 'Not available — this page was opened directly as a file (run it through a local web server to enable offline "Add to Home Screen" access; see the README).' : 'Supported by this browser — the app\'s own files are cached so it can still open while offline.') : 'Not supported by this browser.'}</p>
    <p class="hint">Everything here is saved ONLY in this browser, on this device — in its local storage (plus a backup mirror in this browser's IndexedDB, used only to recover from a local save failure, never for sync). There is no cloud account and nothing is sent anywhere. That means: it survives closing the browser and restarting this device, but it does NOT survive clearing this browser's site data, switching to a different browser, using a different device, or a damaged browser profile. Export a backup regularly, and especially before any of those. Opening this app on another device, or installing it to a phone's Home Screen, does NOT bring this data with it — each install keeps its own separate local copy, unless you import a backup into it.</p>
  `;

  const liveBody = $('settingsLiveBody');
  const online = typeof navigator !== 'undefined' ? navigator.onLine !== false : true;
  liveBody.innerHTML = `
    <p><b>Refresh interval:</b> every ${Math.round(REFRESH_INTERVAL_MS / 1000)} seconds while this tab is visible (paused automatically while it's not, to avoid polling a page nobody is looking at) — this is how often the app asks ESPN's feed for anything new, not a guarantee of how often ESPN itself updates.</p>
    <p><b>Connection:</b> ${online ? '<span style="color:var(--win)">Online</span>' : '<span style="color:var(--warn)">Offline</span>'}</p>
    <p><b>Last successful live-data refresh:</b> ${lastSuccessAt ? esc(new Date(lastSuccessAt).toLocaleString()) : 'never yet'}${isStaleFeed() ? ' — <span style="color:var(--warn)">this looks stale; live scores may be out of date</span>' : ''}</p>
    <p class="hint">Offline access to your saved bets is separate from this — you can always view and edit everything you've tracked while offline, but scores/stats obviously can't update until the connection comes back.</p>
    <div class="btnrow" style="margin-top:10px"><button class="secondary" id="settingsRefreshBtn">↻ Refresh now</button></div>
  `;
  $('settingsRefreshBtn').onclick = refresh;

  $('settingsAppInfo').innerHTML = `NBA Prop Command Center <b>${esc(APP_VERSION)}</b> (data schema v${SCHEMA_VERSION}). ${lastSaveError ? 'Local saving currently has a problem — see Local Storage above.' : 'Local saving is working normally.'} ${('serviceWorker' in navigator && location.protocol !== 'file:') ? 'Offline app shell available.' : 'Offline app shell not active in this session.'}`;
}
$('settingsIntegrityBtn') && $('settingsIntegrityBtn').addEventListener('click', () => {
  const issues = runDataIntegrityCheck();
  const el = $('settingsIntegrityResult');
  if (!issues.length) {
    el.innerHTML = `<p class="hint" style="color:var(--win)">No problems found — ${state.straightBets.length + state.parlays.length} ticket(s) and ${state.ladders.length} ladder(s) checked.</p>`;
    return;
  }
  el.innerHTML = `
    <p class="hint" style="color:var(--warn)">${issues.length} issue(s) found. Nothing has been changed — this check only looks.</p>
    <ul style="margin:6px 0 0;padding-left:18px;font-size:12px;color:var(--muted)">${issues.map(i => `<li>${esc(i)}</li>`).join('')}</ul>
    <p class="hint">If something here looks wrong, the safest fix is usually to Export a backup right now (so you have a safety copy of things as they are), then review the affected bet(s)/ladder(s) by hand in their own tabs.</p>
  `;
});

// ---------------------------------------------------------------------------
// Screenshot import (OCR) — Tesseract.js loaded lazily on first use.
// v6.1: the main bundle is a PINNED, vendored copy of tesseract.js 5.1.1 (see
// vendor/tesseract.js-5.1.1/VENDOR.md for provenance + hashes) instead of the old
// floating `@5` CDN URL. Its worker/WASM/language files still come from jsDelivr, at
// exact versions derived from this build (not floating) — OCR still needs a connection.
// ---------------------------------------------------------------------------
const TESSERACT_SCRIPT_PATH = 'vendor/tesseract.js-5.1.1/tesseract.min.js';
let tesseractLoadPromise = null;
function loadTesseract() {
  if (window.Tesseract) return Promise.resolve();
  if (tesseractLoadPromise) return tesseractLoadPromise;
  tesseractLoadPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = TESSERACT_SCRIPT_PATH;
    s.onload = resolve;
    s.onerror = () => { tesseractLoadPromise = null; reject(new Error('Could not load the OCR library file (' + TESSERACT_SCRIPT_PATH + ').')); };
    document.head.appendChild(s);
  });
  return tesseractLoadPromise;
}

let lastParseResult = null;
$('uploadZone').addEventListener('click', () => $('shotInput').click());
$('uploadZone').addEventListener('dragover', e => e.preventDefault());
$('uploadZone').addEventListener('drop', e => {
  e.preventDefault();
  if (e.dataTransfer.files.length) handleScreenshotFiles(e.dataTransfer.files);
});
$('shotInput').addEventListener('change', e => { if (e.target.files.length) handleScreenshotFiles(e.target.files); });

async function handleScreenshotFiles(fileList) {
  const progressEl = $('ocrProgress');
  progressEl.textContent = 'Loading OCR library…';
  try { await loadTesseract(); }
  catch (e) { progressEl.textContent = e.message; return; }

  let combinedText = '';
  const files = Array.from(fileList);
  for (let i = 0; i < files.length; i++) {
    progressEl.textContent = `Reading image ${i + 1} of ${files.length}…`;
    try {
      const { data: { text } } = await window.Tesseract.recognize(files[i], 'eng', {
        logger: m => { if (m.status === 'recognizing text') progressEl.textContent = `Reading image ${i + 1} of ${files.length}… (${Math.round((m.progress || 0) * 100)}%)`; }
      });
      combinedText += text + '\n';
    } catch (e) {
      progressEl.textContent = 'Could not read one of the images: ' + e.message;
      return;
    }
  }
  progressEl.textContent = 'Done reading. Review the results below.';
  runParseAndShowReview(combinedText);
}

function runParseAndShowReview(rawText) {
  const playerIndex = rosterIndex.players;
  const teamIndex = NBA_TEAMS.map(t => ({ name: t.name, team: t.abbr, abbr: t.abbr, aliases: t.aliases }));
  lastParseResult = parseBetSlipText(rawText, { playerIndex, teamIndex });
  renderReview(rawText);
  $('reviewPanel').classList.remove('hide');
}

function renderReview(rawText) {
  const r = lastParseResult;
  const legHtml = r.legs.map((leg, i) => {
    const flagged = leg.needsReview;
    const field = (label, key, value) => `<div class="review-field${flagged && (value === null || value === undefined || value === '') ? ' flag' : ''}"><label>${label}</label><input data-leg="${i}" data-field="${key}" value="${esc(value ?? '')}"></div>`;
    return `<div class="ticket">
      <div class="ticket-head"><h3>Leg ${i + 1}${flagged ? '<span class="flag-tag">needs review</span>' : ''}</h3></div>
      ${field('Kind (player/team)', 'kind', leg.kind)}
      ${field('Name', 'name', leg.name)}
      ${field('Team', 'team', leg.team)}
      ${field('Market', 'marketType', leg.marketType)}
      ${field('Direction', 'direction', leg.direction)}
      ${field('Line', 'line', leg.line)}
      ${field('Odds', 'odds', leg.odds)}
      <div class="hint">Original OCR text for this leg:</div>
      <div class="raw-ocr">${esc(leg.raw)}</div>
    </div>`;
  }).join('');
  $('reviewBody').innerHTML = `
    <div class="row" style="margin-bottom:10px">
      <span class="chip">${r.isParlay ? 'Detected as a parlay' : 'Detected as straight bet(s)'}</span>
      ${r.combinedOdds ? `<span class="chip">Combined odds ${esc(r.combinedOdds)}</span>` : ''}
      ${r.stake !== null ? `<span class="chip">Stake $${fmt(r.stake)}</span>` : ''}
      ${r.potentialPayout !== null ? `<span class="chip">To win $${fmt(r.potentialPayout)}</span>` : ''}
    </div>
    <p class="hint">Nothing below is saved yet. Fields highlighted in yellow couldn't be read confidently — please fill them in or correct them before saving.</p>
    ${legHtml || '<div class="empty">Couldn\'t find any legs in this image. You can still add the bet manually from the Straight Bets tab.</div>'}
    <div class="hint" style="margin-top:10px">Full raw text read from the image(s):</div>
    <div class="raw-ocr">${esc(rawText)}</div>
  `;
  $('reviewBody').querySelectorAll('input[data-leg]').forEach(inp => {
    inp.addEventListener('input', () => {
      const i = Number(inp.dataset.leg), field = inp.dataset.field;
      let v = inp.value;
      if (field === 'line') v = v === '' ? null : Number(v);
      lastParseResult.legs[i][field] = v;
    });
  });
}

$('discardReviewBtn').addEventListener('click', () => {
  $('reviewPanel').classList.add('hide');
  lastParseResult = null;
  $('ocrProgress').textContent = '';
});
$('saveReviewBtn').addEventListener('click', () => {
  if (!lastParseResult || !lastParseResult.legs.length) { $('reviewPanel').classList.add('hide'); return; }
  const r = lastParseResult;
  const toBet = (leg) => leg.kind === 'player'
    ? { kind: 'prop', player: leg.name, team: leg.team, opponent: null, market: leg.marketType, target: Number(leg.line), direction: leg.direction || 'over', odds: leg.odds, bookmaker: null, stake: null, game: null, voided: false }
    : { kind: 'team', marketType: leg.marketType, teamAbbr: leg.team, oppAbbr: null, line: leg.line === null ? null : Number(leg.line), direction: leg.direction || 'team', odds: leg.odds, bookmaker: null, stake: null, game: null, voided: false };

  const incomplete = r.legs.filter(l => !l.name || !l.kind || !l.marketType || (l.kind === 'player' && !(Number(l.line) > 0)));
  if (incomplete.length) {
    if (!confirm(`${incomplete.length} leg(s) are still missing required info (name/market/line) and will be skipped. Save the rest?`)) return;
  }
  const usable = r.legs.filter(l => l.name && l.kind && l.marketType && (l.kind !== 'player' || Number(l.line) > 0));
  if (!usable.length) { alert('Nothing complete enough to save yet — fill in the highlighted fields and try again.'); return; }

  if (r.isParlay && usable.length >= 2) {
    state.parlays.push({
      id: uid(), name: `Imported parlay ${new Date().toLocaleDateString()}`,
      legs: usable.map(l => ({ legId: uid(), ...toBet(l) })),
      combinedOdds: r.combinedOdds, stake: r.stake, potentialPayout: r.potentialPayout, createdAt: Date.now(),
    });
  } else {
    usable.forEach(l => state.straightBets.push({ id: uid(), createdAt: Date.now(), ...toBet(l) }));
  }
  saveState();
  $('reviewPanel').classList.add('hide');
  lastParseResult = null;
  $('ocrProgress').textContent = 'Saved.';
  renderAll();
  refresh();
});

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------
$('refreshBtn').addEventListener('click', refresh);
$('pp-filter').addEventListener('change', renderPlayerPropsBoard);
loadRosterCache();
loadSeenPlayers();
renderRosterStatus();
renderPendingLegs();
renderAll();
tryRecoverFromIndexedDB();
updateOnlineBanner();
refresh();
startRefreshTimer();
buildRosterIndex(false);

// Pause live-data polling entirely while this tab/window isn't visible (saves battery/
// data and respects the feed provider rather than polling a page nobody is looking at),
// and refresh immediately — not on the next 15s tick — the moment it becomes visible
// again, so the person never comes back to data that's silently been going stale. This
// ONLY affects the scoreboard/box-score refresh loop above; it never touches the roster
// cache, which refreshes on its own much slower 12-hour schedule regardless of tab
// visibility.
if (typeof document !== 'undefined') {
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopRefreshTimer();
    else { refresh(); startRefreshTimer(); }
  });
}

// PWA shell caching — only registers over http(s) (service workers refuse file://), so
// this silently does nothing when the app is opened directly as a file. Running it via
// `python -m http.server` (see README) lets "Add to Home Screen" work on an iPhone and
// caches the app's own files for offline opening. It never touches live ESPN data, and
// it is NOT cross-device sync — each device/browser still keeps its own local data.
if ('serviceWorker' in navigator && location.protocol !== 'file:') {
  window.addEventListener('load', () => { navigator.serviceWorker.register('sw.js').catch(() => {}); });
}

// Asks the browser to mark THIS origin's storage (localStorage + the IndexedDB
// backup mirror together) as "persistent" — i.e. not subject to automatic
// eviction under disk pressure, the way an ordinary, rarely-visited site's
// storage can be. This is purely a request for durability: it changes nothing
// about HOW or WHERE data is read/written (still the exact same localStorage
// calls as before), it's best-effort (some browsers auto-grant it, some prompt,
// some ignore it entirely), and it never blocks app startup either way — the
// app works identically whether or not this is granted.
if (typeof navigator !== 'undefined' && navigator.storage && navigator.storage.persist) {
  navigator.storage.persist().catch(() => {});
}
