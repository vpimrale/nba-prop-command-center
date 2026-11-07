// Pure game-binding + persisted-settlement logic. No DOM, no storage, no network —
// testable in isolation under Node, loaded as a plain <script> in the browser.
//
// WHY THIS EXISTS (v6.1 settlement fix): before this module, a bet's result was
// recomputed on every render against whatever game the live feed currently held
// for that team/player, with no date check. The feed only covers today + yesterday,
// so a bet from days ago could be "settled" against a newer, unrelated game for the
// same team (reproduced: a 3-day-old BOS vs MIA prop showed WON from today's BOS vs
// NYK box score). The fix has three parts, all implemented here:
//
//   1. PLAUSIBILITY WINDOW — an unbound selection may only match a game whose tip-off
//      is within BIND_LOOKBACK_MS before / BIND_LOOKAHEAD_MS after the moment the bet
//      was recorded (or on the explicit ET game date the user gave). A bet from three
//      days ago can never match today's game.
//   2. BINDING — the first time a selection is matched, its game's event id, ET game
//      date and tip-off time are stored on the selection itself (gameId / gameDate /
//      gameStart). From then on ONLY that exact game is ever used for it.
//   3. PERSISTED SETTLEMENT — once that bound game is final and the selection is
//      won/lost/push, a settlement record (status, the evidence it was decided from —
//      final scores and the athlete's final stat line — game id/date, settledAt) is
//      saved on the selection. It no longer depends on the live feed at all, so it
//      survives the game dropping out of the feed window, reloads, and import/export.
//      The status is re-derived from the saved evidence (same pure rules as live), so
//      a retroactive edit to the line/direction/market still re-evaluates correctly.

const HOUR_MS = 60 * 60 * 1000;
// A bet can be entered after the fact (or placed live), so its game may have tipped
// off before the bet was recorded. 30h matches the feed's own recent-final window.
const BIND_LOOKBACK_MS = 30 * HOUR_MS;
// A bet can be placed ahead of tip-off (props usually post the day before at most).
const BIND_LOOKAHEAD_MS = 30 * HOUR_MS;
// Ranking preference: a game that tipped off no more than this long before the bet was
// recorded (i.e. upcoming or in progress at bet time) is preferred over an older one.
const PREFER_NOT_STARTED_GRACE_MS = 4 * HOUR_MS;

// ESPN's site API uses a few abbreviations that differ from the ones this app's team
// list (teams.js) and its autocomplete store. Both sides are mapped to one canonical
// code before ANY comparison, so "GSW" (autocomplete) and "GS" (ESPN) are the same team.
const TEAM_CODE_ALIASES = {
  GS: 'GSW', NY: 'NYK', SA: 'SAS', NO: 'NOP', NOR: 'NOP', UTAH: 'UTA', WSH: 'WAS',
  PHO: 'PHX', BRK: 'BKN', BK: 'BKN', CHO: 'CHA', NJ: 'BKN',
};

const _teamsForBinding = (typeof module !== 'undefined') ? require('./teams.js').NBA_TEAMS : null;
const _propsForBinding = (typeof module !== 'undefined') ? require('./prop-status.js') : null;
const _settleForBinding = (typeof module !== 'undefined') ? require('./settlement.js') : null;
function _teamList() { return _teamsForBinding || (typeof NBA_TEAMS !== 'undefined' ? NBA_TEAMS : []); }
function _evalPropLeg(...a) { return (_propsForBinding ? _propsForBinding.evaluatePropLeg : evaluatePropLeg)(...a); }
function _evalTeamBet(...a) { return (_settleForBinding ? _settleForBinding.evaluateTeamBet : evaluateTeamBet)(...a); }

const _normTeam = s => String(s ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '');

// Free text / ESPN code / app code -> canonical app team code (e.g. 'GSW'). Exact
// matches only (abbr, alias, full name, nickname, city) — never a substring guess.
// Unknown input is returned normalized, so two identical unknown strings still match.
function canonicalTeam(value) {
  const n = _normTeam(value);
  if (!n) return null;
  if (TEAM_CODE_ALIASES[n]) return TEAM_CODE_ALIASES[n];
  const teams = _teamList();
  const byAbbr = teams.find(t => t.abbr === n);
  if (byAbbr) return byAbbr.abbr;
  const byName = teams.find(t => _normTeam(t.name) === n || (t.aliases || []).some(a => _normTeam(a) === n));
  return byName ? byName.abbr : n;
}
function sameTeam(a, b) {
  const ca = canonicalTeam(a), cb = canonicalTeam(b);
  return !!ca && ca === cb;
}

// ---- Dates (NBA schedules, and ESPN's `dates=` param, are US-Eastern calendar days) --
const _etFormatter = (typeof Intl !== 'undefined')
  ? new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit' })
  : null;
// epoch ms -> 'YYYY-MM-DD' (US Eastern calendar date), or null.
function etDateString(ts) {
  if (ts === null || ts === undefined || !Number.isFinite(Number(ts))) return null;
  const d = new Date(Number(ts));
  if (_etFormatter) return _etFormatter.format(d); // en-CA formats as YYYY-MM-DD
  return d.toISOString().slice(0, 10);
}
// ESPN scoreboard `dates=YYYYMMDD` for the Eastern calendar day `offsetDays` from now.
function etDateParam(nowMs, offsetDays) {
  const [y, m, d] = etDateString(nowMs).split('-').map(Number);
  const shifted = new Date(Date.UTC(y, m - 1, d + (offsetDays || 0)));
  return shifted.getUTCFullYear() + String(shifted.getUTCMonth() + 1).padStart(2, '0') + String(shifted.getUTCDate()).padStart(2, '0');
}

// ---- Parsing ESPN payloads into the app's game shape --------------------------------
// Shared by the scoreboard (list of events) and the per-event summary (used to load a
// bound game that is no longer on the scoreboard). `completed` is ESPN's own flag;
// a 'post' state that is NOT completed (e.g. postponed/cancelled) is never settled.
function _gameFromParts(id, dateStr, competitors, statusObj) {
  const home = (competitors || []).find(c => c.homeAway === 'home');
  const away = (competitors || []).find(c => c.homeAway === 'away');
  const st = (statusObj && statusObj.type) || {};
  const isHalftime = /halftime/i.test(st.description || '') || /halftime/i.test(st.name || '');
  const startTimestamp = dateStr ? new Date(dateStr).getTime() : null;
  return {
    id: id === null || id === undefined ? null : String(id),
    home: (home && home.team && home.team.abbreviation) || 'HOME',
    away: (away && away.team && away.team.abbreviation) || 'AWAY',
    homeScore: (home && home.score !== undefined && home.score !== null) ? (typeof home.score === 'object' ? (home.score.displayValue ?? home.score.value ?? '0') : home.score) : '0',
    awayScore: (away && away.score !== undefined && away.score !== null) ? (typeof away.score === 'object' ? (away.score.displayValue ?? away.score.value ?? '0') : away.score) : '0',
    state: isHalftime ? 'halftime' : (st.state || 'pre'),
    completed: typeof st.completed === 'boolean' ? st.completed : undefined,
    period: (statusObj && statusObj.period) || 0,
    clock: (statusObj && statusObj.displayClock) || '',
    startTimestamp: Number.isFinite(startTimestamp) ? startTimestamp : null,
    gameDate: etDateString(startTimestamp),
  };
}
function gameFromScoreboardEvent(ev) {
  const comp = ev && ev.competitions && ev.competitions[0];
  if (!comp) return null;
  return _gameFromParts(ev.id, ev.date || comp.date, comp.competitors, ev.status || comp.status);
}
function gameFromSummary(summary, fallbackId) {
  const header = summary && summary.header;
  const comp = header && header.competitions && header.competitions[0];
  if (!comp) return null;
  return _gameFromParts(header.id ?? comp.id ?? fallbackId, comp.date, comp.competitors, comp.status || header.status);
}

// ---- Candidate games for a selection -----------------------------------------------
function selectionTeams(sel) {
  if (sel.kind === 'prop') return { team: canonicalTeam(sel.team), opp: canonicalTeam(sel.opponent) };
  return { team: canonicalTeam(sel.teamAbbr), opp: canonicalTeam(sel.oppAbbr) };
}
function gameInvolves(game, teamCode) {
  return !!teamCode && (canonicalTeam(game.home) === teamCode || canonicalTeam(game.away) === teamCode);
}
function isPlausibleGameForBet(game, createdAt) {
  if (!game || !Number.isFinite(game.startTimestamp) || !Number.isFinite(Number(createdAt))) return false;
  const delta = game.startTimestamp - Number(createdAt);
  return delta >= -BIND_LOOKBACK_MS && delta <= BIND_LOOKAHEAD_MS;
}
// Returns candidate games for an UNSETTLED selection, best first. Never returns a game
// outside the plausibility window (or the user's explicit ET game date), and once a
// selection is bound returns only its bound game (or nothing, if that game isn't loaded).
function candidateGamesForSelection(sel, games) {
  const list = (games || []).filter(Boolean);
  if (sel.gameId) return list.filter(g => String(g.id) === String(sel.gameId));
  let c;
  if (sel.gameDateHint) c = list.filter(g => g.gameDate === sel.gameDateHint || etDateString(g.startTimestamp) === sel.gameDateHint);
  else c = list.filter(g => isPlausibleGameForBet(g, sel.createdAt));
  const { team, opp } = selectionTeams(sel);
  if (sel.kind === 'team') c = c.filter(g => gameInvolves(g, team) || gameInvolves(g, opp));
  else if (team) c = c.filter(g => gameInvolves(g, team));
  // An explicit opponent is a hard constraint: if it was given, a game against anyone
  // else is never used (a wrong opponent shows "Needs review" instead of a wrong result).
  if (opp) c = c.filter(g => gameInvolves(g, opp));
  const created = Number(sel.createdAt);
  const bucket = g => (Number.isFinite(created) && g.startTimestamp >= created - PREFER_NOT_STARTED_GRACE_MS) ? 0 : 1;
  const dist = g => Number.isFinite(created) ? Math.abs(g.startTimestamp - created) : 0;
  return c.slice().sort((x, y) => bucket(x) - bucket(y) || dist(x) - dist(y));
}

// ---- Binding + settlement records -------------------------------------------------
function bindingFor(game, nowMs) {
  return {
    gameId: String(game.id),
    gameDate: game.gameDate || etDateString(game.startTimestamp),
    gameStart: Number.isFinite(game.startTimestamp) ? new Date(game.startTimestamp).toISOString() : null,
    boundAt: nowMs,
  };
}
const SETTLEABLE = ['won', 'lost', 'push'];
// ev: { status, current, g, a } from the live evaluator (g = game, a = athlete row).
function shouldPersistSettlement(sel, ev) {
  if (!ev || !ev.g || sel.voided) return false;
  if (sel.gameId && String(ev.g.id) !== String(sel.gameId)) return false; // never settle from a different game
  if (ev.g.state !== 'post' || ev.g.completed === false) return false;
  if (!SETTLEABLE.includes(ev.status)) return false;
  if (sel.kind === 'prop' && !ev.a) return false;
  return true;
}
function buildSettlement(sel, ev, nowMs) {
  const g = ev.g, a = ev.a;
  return {
    status: ev.status,
    current: ev.current ?? null,
    gameId: String(g.id),
    gameDate: g.gameDate || etDateString(g.startTimestamp),
    gameStart: Number.isFinite(g.startTimestamp) ? new Date(g.startTimestamp).toISOString() : null,
    settledAt: nowMs,
    source: 'auto-final-boxscore',
    snapshot: {
      home: g.home, away: g.away, homeScore: g.homeScore, awayScore: g.awayScore,
      athlete: a ? { espnId: a.espnId ?? null, name: a.name, team: a.team, minutes: a.minutes ?? null, stats: { ...(a.stats || {}) } } : null,
    },
  };
}
// Re-derives a settled selection's status from its SAVED evidence (never the live feed),
// so editing the line/direction/market after settlement re-evaluates correctly.
function evaluateFromSettlement(sel, settlement) {
  const s = settlement && settlement.snapshot;
  if (!s) return { status: 'unmatched', current: null };
  if (sel.kind === 'prop') {
    if (!s.athlete) return { status: 'unmatched', current: null };
    return _evalPropLeg(sel, s.athlete.stats, 'post');
  }
  const game = { state: 'post', homeAbbr: canonicalTeam(s.home), awayAbbr: canonicalTeam(s.away), homeScore: s.homeScore, awayScore: s.awayScore };
  return _evalTeamBet({ marketType: sel.marketType, team: canonicalTeam(sel.teamAbbr), direction: sel.direction, line: sel.line }, game);
}
// Fields whose change means "this may be a different game/player" — editing any of them
// clears the binding + saved settlement so the selection is matched afresh.
const IDENTITY_FIELDS = ['player', 'playerId', 'team', 'opponent', 'teamAbbr', 'oppAbbr', 'gameDateHint'];
function identityChanged(before, after) {
  return IDENTITY_FIELDS.some(k => String(before[k] ?? '') !== String(after[k] ?? ''));
}
function clearBinding(sel) {
  sel.gameId = null; sel.gameDate = null; sel.gameStart = null; sel.boundAt = null; sel.settlement = null;
}

if (typeof module !== 'undefined') module.exports = {
  BIND_LOOKBACK_MS, BIND_LOOKAHEAD_MS, PREFER_NOT_STARTED_GRACE_MS, TEAM_CODE_ALIASES,
  canonicalTeam, sameTeam, etDateString, etDateParam,
  gameFromScoreboardEvent, gameFromSummary,
  selectionTeams, gameInvolves, isPlausibleGameForBet, candidateGamesForSelection,
  bindingFor, shouldPersistSettlement, buildSettlement, evaluateFromSettlement,
  identityChanged, clearBinding, IDENTITY_FIELDS,
};
