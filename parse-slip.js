// Pure parsing logic for OCR'd bet-slip text. No DOM, no network — testable in isolation.
// This is deliberately conservative: anything it can't confidently read is left blank
// with the original OCR line attached, rather than guessed.

const MARKET_WORDS = {
  'PRA': /\bpts\s*\+\s*reb\s*\+\s*ast\b|\bp\s*\+\s*r\s*\+\s*a\b|\bpra\b/i,
  'PR': /\bpts\s*\+\s*reb\b|\bpoints\s*\+\s*rebounds\b/i,
  'PA': /\bpts\s*\+\s*ast\b|\bpoints\s*\+\s*assists\b/i,
  'PTS': /\bpoints\b|\bpts\b/i,
  'REB': /\brebounds\b|\breb\b/i,
  'AST': /\bassists\b|\bast\b/i,
  '3PM': /\b3[\s-]?pt(?:ers)?\s*made\b|\bthrees?\s*made\b|\b3pm\b/i,
  'STL': /\bsteals\b|\bstl\b/i,
  'BLK': /\bblocks\b|\bblk\b/i,
  'TO':  /\bturnovers\b/i,
};

function detectOddsToken(line){
  const m = line.match(/([+-]\d{2,5})\b/);
  return m ? m[1] : null;
}
function detectOverUnder(line){
  const m = line.match(/\b(over|under|o|u)\b\s*([0-9]+(?:\.5)?)/i) || line.match(/([0-9]+(?:\.5)?)\s*\b(over|under|o|u)\b/i);
  if(!m) return null;
  let dir, num;
  if(/^(over|o)$/i.test(m[1])) { dir='over'; num=m[2]; }
  else if(/^(under|u)$/i.test(m[1])) { dir='under'; num=m[2]; }
  else { num=m[1]; dir=/^(over|o)$/i.test(m[2])?'over':'under'; }
  return { direction: dir, line: Number(num) };
}
function detectMarket(line){
  for(const key of Object.keys(MARKET_WORDS)){
    if(MARKET_WORDS[key].test(line)) return key;
  }
  return null;
}
function detectSpread(line){
  // e.g. "Celtics -5.5" or "+5.5" as a standalone handicap near a team line
  const m = line.match(/\bspread\b/i) || line.match(/^[+-]\d+(\.5)?$/);
  return !!m;
}
function detectMoneyline(line){ return /\bmoneyline\b|\bml\b/i.test(line); }
function detectGameTotalLabel(line){ return /\btotal\s*points?\b|\bgame\s*total\b|\bover\/under\b/i.test(line); }
function detectTeamTotalLabel(line){ return /\bteam\s*total\b/i.test(line); }

function money(line){
  const m = line.match(/\$\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)/);
  return m ? Number(m[1].replace(/,/g,'')) : null;
}

function findBestNameMatch(line, index){
  // index: [{name, team, kind:'player'|'team', aliases:[...]}]
  const norm = s => (s||'').toLowerCase().replace(/[^a-z0-9]/g,'');
  const nLine = norm(line);
  if(!nLine) return null;
  let best = null, bestScore = 0;
  for(const entry of index){
    const candidates = [entry.name, ...(entry.aliases||[])];
    for(const c of candidates){
      const nc = norm(c);
      if(!nc) continue;
      let score = 0;
      if(nLine === nc) score = 100;
      else if(nLine.includes(nc) && nc.length > 3) score = 60 + nc.length;
      if(score > bestScore){ bestScore = score; best = entry; }
    }
  }
  return bestScore >= 60 ? best : null;
}

function parseBetSlipText(rawText, { playerIndex = [], teamIndex = [] } = {}){
  const lines = rawText.split(/\r?\n/).map(l=>l.trim()).filter(Boolean);
  const combinedIndex = [...playerIndex.map(p=>({...p,kind:'player'})), ...teamIndex.map(t=>({...t,kind:'team'}))];

  const result = {
    isParlay: false,
    legHintCount: null,
    combinedOdds: null,
    stake: null,
    potentialPayout: null,
    legs: [],
    unparsedLines: [],
  };

  // Header / summary lines, detected anywhere in the text (not consumed into legs)
  const summaryLineIdx = new Set();
  lines.forEach((line, i) => {
    const parlayHeader = line.match(/\b(\d+)[\s-]*leg\s*parlay\b/i) || /\bparlay\b/i.test(line);
    if (parlayHeader) {
      result.isParlay = true;
      const n = line.match(/\b(\d+)[\s-]*leg/i);
      if (n) result.legHintCount = Number(n[1]);
      summaryLineIdx.add(i);
    }
    if (/\btotal\s*odds\b|\bcombined\s*odds\b|\bparlay\s*odds\b/i.test(line)) {
      result.combinedOdds = detectOddsToken(line);
      summaryLineIdx.add(i);
    }
    if (/\bwager\b|\brisk\b|\bstake\b/i.test(line)) {
      result.stake = money(line);
      summaryLineIdx.add(i);
    }
    if (/\bto\s*win\b|\bpotential\s*payout\b|\bpayout\b/i.test(line)) {
      result.potentialPayout = money(line);
      summaryLineIdx.add(i);
    }
  });

  const legLines = lines.filter((_, i) => !summaryLineIdx.has(i));

  // Group lines into legs: a leg block ends when we hit a standalone odds token line
  // (or the end of input), after having seen at least one non-odds line.
  let current = [];
  const blocks = [];
  for (const line of legLines) {
    current.push(line);
    const isOddsOnly = /^[+-]\d{2,5}$/.test(line);
    if (isOddsOnly) {
      blocks.push(current);
      current = [];
    }
  }
  if (current.length) blocks.push(current); // trailing block with no odds line found

  for (const block of blocks) {
    const leg = {
      raw: block.join(' / '),
      kind: null,        // 'player' | 'team'
      name: null,         // matched display name
      team: null,
      opponent: null,
      marketType: null,   // for team legs: gameTotal | teamTotal | spread | moneyline ; for players: stat code
      direction: null,    // over/under, or team/opponent for moneyline
      line: null,          // numeric line (target / spread / total)
      odds: null,
      confidence: 'low',
      needsReview: true,
    };

    let oddsLine = block.find(l => /^[+-]\d{2,5}$/.test(l));
    if (oddsLine) leg.odds = oddsLine;

    const matchup = block.find(l => /\bvs\b|@/i.test(l) && !/\d/.test(l.replace(/[^\d]/g,'')));

    for (const line of block) {
      if (line === oddsLine) continue;
      const ou = detectOverUnder(line);
      if (ou) { leg.direction = ou.direction; leg.line = ou.line; }
      // Team-market phrasing ("Total Points", "Team Total", "Moneyline", a bare spread
      // number) is checked before the generic player-stat words, since phrases like
      // "Total Points" would otherwise also match the PTS stat regex.
      let mkt = null;
      if (detectMoneyline(line)) { leg.marketType = 'moneyline'; }
      else if (detectTeamTotalLabel(line)) { leg.marketType = 'teamTotal'; }
      else if (detectGameTotalLabel(line)) { leg.marketType = 'gameTotal'; }
      else if (detectSpread(line)) { leg.marketType = leg.marketType || 'spread'; if(!leg.line){ const m=line.match(/[+-]\d+(\.5)?/); if(m) leg.line = Number(m[0]); } }
      else { mkt = detectMarket(line); if (mkt) leg.marketType = mkt; }

      // Try to resolve a name (player or team) from this line if we haven't yet,
      // and it isn't itself a market/odds/matchup descriptor line.
      if (!leg.name && !ou && !mkt && !detectMoneyline(line) && line !== matchup) {
        const match = findBestNameMatch(line, combinedIndex);
        if (match) {
          leg.name = match.name;
          leg.kind = match.kind;
          leg.team = match.team || (match.kind === 'team' ? match.abbr : null);
          leg.confidence = 'matched';
        }
      }
    }

    if (matchup) {
      const parts = matchup.split(/\bvs\b|@/i).map(s=>s.trim()).filter(Boolean);
      if (parts.length === 2) { leg.matchupA = parts[0]; leg.matchupB = parts[1]; }
      if (!leg.marketType) leg.marketType = 'gameTotal'; // "Team A vs Team B" + a total line with no team name usually means game total
    }

    if (!leg.kind) leg.kind = (leg.marketType && ['gameTotal','teamTotal','spread','moneyline'].includes(leg.marketType)) ? 'team' : (leg.marketType ? 'player' : null);
    if (leg.name && leg.marketType) leg.needsReview = leg.confidence !== 'matched' || !leg.odds;
    else leg.needsReview = true;

    result.legs.push(leg);
  }

  return result;
}

if (typeof module !== 'undefined') module.exports = { parseBetSlipText, detectOddsToken, detectOverUnder, detectMarket, findBestNameMatch };
