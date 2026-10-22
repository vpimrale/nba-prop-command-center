// Static NBA team reference data. Team identities/abbreviations are stable league facts
// (not live data), so this is bundled rather than fetched. Rosters (which DO change
// often — trades, signings, rookies) are fetched live from ESPN at runtime instead
// of being hard-coded here; see buildPlayerIndex() in app.js.
const NBA_TEAMS = [
  { abbr:'ATL', city:'Atlanta', nickname:'Hawks' },
  { abbr:'BOS', city:'Boston', nickname:'Celtics' },
  { abbr:'BKN', city:'Brooklyn', nickname:'Nets' },
  { abbr:'CHA', city:'Charlotte', nickname:'Hornets' },
  { abbr:'CHI', city:'Chicago', nickname:'Bulls' },
  { abbr:'CLE', city:'Cleveland', nickname:'Cavaliers', aliases:['Cavs'] },
  { abbr:'DAL', city:'Dallas', nickname:'Mavericks', aliases:['Mavs'] },
  { abbr:'DEN', city:'Denver', nickname:'Nuggets' },
  { abbr:'DET', city:'Detroit', nickname:'Pistons' },
  { abbr:'GSW', city:'Golden State', nickname:'Warriors', aliases:['Dubs','GS'] },
  { abbr:'HOU', city:'Houston', nickname:'Rockets' },
  { abbr:'IND', city:'Indiana', nickname:'Pacers' },
  { abbr:'LAC', city:'LA', nickname:'Clippers', aliases:['Los Angeles Clippers'] },
  { abbr:'LAL', city:'Los Angeles', nickname:'Lakers' },
  { abbr:'MEM', city:'Memphis', nickname:'Grizzlies' },
  { abbr:'MIA', city:'Miami', nickname:'Heat' },
  { abbr:'MIL', city:'Milwaukee', nickname:'Bucks' },
  { abbr:'MIN', city:'Minnesota', nickname:'Timberwolves', aliases:['Wolves'] },
  { abbr:'NOP', city:'New Orleans', nickname:'Pelicans' },
  { abbr:'NYK', city:'New York', nickname:'Knicks' },
  { abbr:'OKC', city:'Oklahoma City', nickname:'Thunder' },
  { abbr:'ORL', city:'Orlando', nickname:'Magic' },
  { abbr:'PHI', city:'Philadelphia', nickname:'76ers', aliases:['Sixers'] },
  { abbr:'PHX', city:'Phoenix', nickname:'Suns' },
  { abbr:'POR', city:'Portland', nickname:'Trail Blazers', aliases:['Blazers'] },
  { abbr:'SAC', city:'Sacramento', nickname:'Kings' },
  { abbr:'SAS', city:'San Antonio', nickname:'Spurs' },
  { abbr:'TOR', city:'Toronto', nickname:'Raptors' },
  { abbr:'UTA', city:'Utah', nickname:'Jazz' },
  { abbr:'WAS', city:'Washington', nickname:'Wizards' },
].map(t => ({
  ...t,
  name: `${t.city} ${t.nickname}`,
  aliases: [...(t.aliases||[]), t.nickname, t.abbr, t.city],
}));

if (typeof module !== 'undefined') module.exports = { NBA_TEAMS };
