// ONE shared Over/Under line-evaluation function, used by prop-status.js (player
// props) and settlement.js (team/game totals, and the spread push case) — so a
// whole-number line and a half-point line are judged by exactly the same rule
// everywhere in the app: straight bets, parlay legs, manual entries, screenshot
// imports, edits, and every displayed ticket. This is the single place that
// decides win/push/lose for an Over/Under threshold; nothing else in the app
// re-implements this comparison.
//
// THE RULE (for an integer-valued stat/score — points, rebounds, a game total,
// an against-the-spread margin, etc. — which is what every market in this app is):
//   OVER line:  win if current > line.  A WHOLE-NUMBER line also pushes if
//               current === line (the result landed exactly on the number).
//               A HALF-POINT line (x.5) can never push — there's no way for an
//               integer stat to equal a half value, so every half-point line
//               settles win or lose, never push.
//   UNDER line: the mirror image — win if current < line, same push rule.
//
// This means Over 17 and Over 17.5 are NOT the same line — Over 17 can push (if
// the result is exactly 17), while Over 17.5 never can — but they DO require the
// exact same result to actually WIN (18 or more). Same for Under 17 vs Under
// 17.5: both need 16 or fewer to win, but only Under 17 can push (at exactly 17).
// The original line is never rewritten to "simplify" it — a whole-number line
// keeps its real push possibility, a half-point line is never invented in its
// place, and vice versa.
//
// A LIVE/in-progress game only ever reports:
//   - 'target_reached_live' for an OVER whose cumulative stat has already
//     reached the line (points/rebounds/etc. only ever go up — but this is
//     still reported as live, not won, since the game isn't final and an
//     official stat correction could still change the number)
//   - 'live' for everything else not yet final (an UNDER is never reported as
//     "secured" before the game ends, since the stat can still climb past it)
function evaluateOverUnderLine({ current, direction, line, isFinal }) {
  const isWholeLine = Number.isInteger(line);
  if (!isFinal) {
    if (direction === 'over') return { status: current >= line ? 'target_reached_live' : 'live', current };
    return { status: 'live', current, onPace: current < line };
  }
  if (isWholeLine && current === line) return { status: 'push', current };
  const hit = direction === 'over' ? current > line : current < line;
  return { status: hit ? 'won' : 'lost', current };
}

// Short, user-facing explanation of how a given line will be interpreted —
// shown as helper text next to the line input so the exact rule is never a
// silent assumption. Only meaningful for integer-valued stats (every stat this
// app tracks); markets with their own settlement rules (moneyline) don't use this.
function lineHelperText(direction, line) {
  const n = Number(line);
  if (!Number.isFinite(n) || n <= 0) return '';
  const dirWord = direction === 'under' ? 'Under' : 'Over';
  if (Number.isInteger(n)) {
    const winAt = direction === 'under' ? n - 1 : n + 1;
    const cmp = direction === 'under' ? 'or lower' : 'or higher';
    return `${dirWord} ${n}: wins at ${winAt} ${cmp}, pushes (refunds the stake) at exactly ${n} — a whole-number line can push.`;
  }
  const floor = Math.floor(n), ceil = Math.ceil(n);
  const winAt = direction === 'under' ? floor : ceil;
  const cmp = direction === 'under' ? 'or lower' : 'or higher';
  return `${dirWord} ${n}: wins at ${winAt} ${cmp} — a half-point line never pushes.`;
}

if (typeof module !== 'undefined') module.exports = { evaluateOverUnderLine, lineHelperText };
