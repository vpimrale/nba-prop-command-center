// Pure settlement logic for team markets + parlay aggregation. Testable in isolation.
// Status vocabulary matches prop-status.js exactly, so a straight prop, a team-market
// bet, and a parlay leg are all judged by the same rules everywhere in the app:
//   'unmatched' | 'upcoming' | 'live' | 'target_reached_live' | 'won' | 'lost' | 'push'
// 'target_reached_live' is only used for totals (game/team), never for spread or
// moneyline, since a scoring margin or who's leading can flip completely before
// final — unlike a cumulative counting stat, which (for an Over) only ever goes up.

// bet: { marketType: 'gameTotal'|'teamTotal'|'spread'|'moneyline', team, opponent, direction, line }
//   - gameTotal: direction 'over'|'under', line = combined total
//   - teamTotal: direction 'over'|'under', line = that team's total (team field says which team)
//   - spread:    line = signed handicap applied to `team` (direction unused)
//   - moneyline: direction 'team'|'opponent' (which side is picked to win)
// game: { state: 'pre'|'in'|'halftime'|'post', homeAbbr, awayAbbr, homeScore, awayScore }

// line-rules.js's evaluateOverUnderLine() is the ONE shared win/push/lose function
// for every Over/Under threshold in the app (player props AND team/game totals) —
// loaded as a global in the browser, required directly here under Node for tests.
// (Same note as prop-status.js: in the browser this reads the global function
// line-rules.js already declared; only Node's require() needs a local binding,
// kept under a different name so it never redeclares the global.)
const _lineRulesForSettlement = (typeof module !== 'undefined') ? require('./line-rules.js') : null;
const evalOverUnderLine = (...args) => (_lineRulesForSettlement ? _lineRulesForSettlement.evaluateOverUnderLine : evaluateOverUnderLine)(...args);

function teamScore(game, abbr) {
  if (!game) return null;
  if (abbr === game.homeAbbr) return Number(game.homeScore);
  if (abbr === game.awayAbbr) return Number(game.awayScore);
  return null;
}

function evaluateTotal(current, direction, line, isFinal) {
  return evalOverUnderLine({ current, direction, line, isFinal });
}

function evaluateTeamBet(bet, game) {
  if (!game) return { status: 'unmatched', current: null, detail: 'Game not found' };
  if (game.state === 'pre') return { status: 'upcoming', current: null };

  const isFinal = game.state === 'post';

  switch (bet.marketType) {
    case 'gameTotal': {
      const current = Number(game.homeScore) + Number(game.awayScore);
      return evaluateTotal(current, bet.direction, bet.line, isFinal);
    }
    case 'teamTotal': {
      const current = teamScore(game, bet.team);
      if (current === null) return { status: 'unmatched', current: null, detail: 'Team not in this game' };
      return evaluateTotal(current, bet.direction, bet.line, isFinal);
    }
    case 'spread': {
      const teamPts = teamScore(game, bet.team);
      const oppAbbr = bet.team === game.homeAbbr ? game.awayAbbr : game.homeAbbr;
      const oppPts = teamScore(game, oppAbbr);
      if (teamPts === null || oppPts === null) return { status: 'unmatched', current: null };
      const margin = teamPts - oppPts;
      const adjusted = margin + bet.line; // standard against-the-spread formula
      if (!isFinal) return { status: 'live', current: margin, adjustedMargin: adjusted };
      // Reuse the same shared win/push/lose rule: "beat the spread" is exactly an
      // Over-0 bet on the adjusted margin — a whole-number spread (e.g. -5) can
      // push at adjusted===0, a half-point spread (e.g. -5.5) never can, exactly
      // like any other Over/Under line in this app.
      const r = evalOverUnderLine({ current: adjusted, direction: 'over', line: 0, isFinal: true });
      // Only a whole-number ORIGINAL spread can push — a half-point original spread
      // can never produce adjusted===0 (margin is always an integer), so this guard
      // is just defensive; the real push-capability already comes from bet.line itself.
      if (r.status === 'push' && !Number.isInteger(bet.line)) return { status: adjusted > 0 ? 'won' : 'lost', current: margin, adjustedMargin: adjusted };
      return { status: r.status, current: margin, adjustedMargin: adjusted };
    }
    case 'moneyline': {
      const pickAbbr = bet.direction === 'team' ? bet.team : (bet.team === game.homeAbbr ? game.awayAbbr : game.homeAbbr);
      const otherAbbr = pickAbbr === game.homeAbbr ? game.awayAbbr : game.homeAbbr;
      const pickScore = teamScore(game, pickAbbr);
      const otherScore = teamScore(game, otherAbbr);
      if (pickScore === null || otherScore === null) return { status: 'unmatched', current: null };
      if (!isFinal) return { status: 'live', current: `${pickScore}-${otherScore}`, leading: pickScore > otherScore };
      if (pickScore === otherScore) return { status: 'push', current: `${pickScore}-${otherScore}` };
      return { status: pickScore > otherScore ? 'won' : 'lost', current: `${pickScore}-${otherScore}` };
    }
    default:
      return { status: 'unmatched', current: null, detail: 'Unknown market type' };
  }
}

// Parlay aggregation. A leg that merely "reached its target live" is NOT a won leg
// — the ticket stays 'live' until every leg is actually settled. A push leg is
// dropped from the price (standard sportsbook treatment): it doesn't make the
// ticket win or lose by itself, but if every leg pushes, the whole ticket pushes.
function evaluateParlay(legStatuses) {
  if (legStatuses.some(s => s === 'lost')) return 'lost';
  if (legStatuses.some(s => s === 'unmatched')) return 'needs_review';
  const settled = legStatuses.filter(s => s !== 'push');
  if (settled.length === 0) return 'push'; // every leg pushed
  if (settled.every(s => s === 'won')) return 'won';
  return 'live';
}

if (typeof module !== 'undefined') module.exports = { evaluateTeamBet, evaluateParlay, teamScore };
