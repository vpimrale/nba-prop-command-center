// Pure player-prop stat math + status logic. Testable in isolation, and used by
// every view that shows a prop leg (straight board, parlay tickets, Overview) so
// there is exactly one place that decides what a prop's live status means.
//
// Status vocabulary (shared with settlement.js for team markets):
//   'unmatched'            — couldn't find this player/game in the live feed
//   'upcoming'             — game hasn't started
//   'live'                 — game in progress, condition not (yet, provably) met
//   'target_reached_live'  — game in progress/halftime AND the OVER condition is
//                            already met by the cumulative stat so far. For a
//                            cumulative counting stat (points, rebounds, etc.)
//                            this is very unlikely to reverse, but it is still
//                            not a final result — an official stat correction
//                            can change it, so it is reported as "live", not "won".
//   'won' / 'lost' / 'push'— only ever set once the game is final

const STAT_KEYS = {
  PTS: ['PTS', 'POINTS'],
  REB: ['REB', 'REBOUNDS'],
  AST: ['AST', 'ASSISTS'],
  STL: ['STL', 'STEALS'],
  BLK: ['BLK', 'BLOCKS'],
  TO: ['TO', 'TOV', 'TURNOVERS'],
  PF: ['PF', 'FOULS', 'PERSONAL FOULS'],
  '3PM': ['3PM', '3PTM', 'THREES MADE', '3PT'],
};

function rawStat(statsObj, code) {
  const s = statsObj || {};
  for (const k of (STAT_KEYS[code] || [code])) { if (s[k] !== undefined) return Number(s[k]) || 0; }
  return 0;
}

// Component stats used by this market — surfaced so the UI can show "PTS 12 | REB 5 | AST 2"
// alongside the combined total, for any combo market.
function componentsForMarket(market) {
  switch (market) {
    case 'PRA': return ['PTS', 'REB', 'AST'];
    case 'PR': return ['PTS', 'REB'];
    case 'PA': return ['PTS', 'AST'];
    case 'RA': return ['REB', 'AST'];
    case 'PTS': return ['PTS'];
    case 'REB': return ['REB'];
    case 'AST': return ['AST'];
    case 'STL': return ['STL'];
    case 'BLK': return ['BLK'];
    case 'TO': return ['TO'];
    case 'PF': return ['PF'];
    case '3PM': return ['3PM'];
    default: return [];
  }
}

function getStatForMarket(statsObj, market) {
  return componentsForMarket(market).reduce((sum, code) => sum + rawStat(statsObj, code), 0);
}

function marketLabel(m) {
  return ({
    PRA: 'PRA', PR: 'PTS + REB', PA: 'PTS + AST', RA: 'REB + AST',
    PTS: 'Points', REB: 'Rebounds', AST: 'Assists',
    '3PM': '3-pointers made', STL: 'Steals', BLK: 'Blocks', TO: 'Turnovers', PF: 'Fouls',
  })[m] || m;
}

// line-rules.js's evaluateOverUnderLine() is the ONE shared win/push/lose function
// for every Over/Under threshold in the app — loaded as a global in the browser
// (script tag order), required directly here under Node for tests.
// (In the browser, line-rules.js is a plain <script> that already declares the
// global function `evaluateOverUnderLine` — calling it directly below just reads
// that global. Only Node needs require() here; a top-level `const` with the SAME
// name as the global would redeclare it and throw across separate <script> tags,
// so this uses a differently-named local purely for the Node/test path.)
const _lineRulesForProps = (typeof module !== 'undefined') ? require('./line-rules.js') : null;

// leg: { market, target, direction: 'over'|'under' }
// athleteStats: the matched athlete's raw stats object, or null if unmatched
// gameState: 'pre' | 'in' | 'halftime' | 'post'
// `leg.target` is always the EXACT line the user entered/the sportsbook showed —
// never rewritten here (see line-rules.js for exactly how it's interpreted).
function evaluatePropLeg(leg, athleteStats, gameState) {
  // A game that hasn't tipped off yet is "upcoming", not a data error — checked
  // before the athlete-match check, since a pre-game box score legitimately has
  // no player stats yet. gameState === null/undefined means no game was found
  // at all (not even by team), which IS a real "can't find this" situation.
  if (gameState === 'pre') {
    return { status: 'upcoming', current: null };
  }
  if (athleteStats === null || athleteStats === undefined) {
    return { status: 'unmatched', current: null };
  }
  const current = getStatForMarket(athleteStats, leg.market);
  const isFinal = gameState === 'post';
  const ouFn = _lineRulesForProps ? _lineRulesForProps.evaluateOverUnderLine : evaluateOverUnderLine;
  return ouFn({ current, direction: leg.direction, line: leg.target, isFinal });
}

if (typeof module !== 'undefined') module.exports = { evaluatePropLeg, getStatForMarket, componentsForMarket, marketLabel, rawStat };
