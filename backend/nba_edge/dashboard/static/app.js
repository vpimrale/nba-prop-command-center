'use strict';
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (v, d = 1) => (v === null || v === undefined ? '—' : (100 * v).toFixed(d) + '%');
const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? '—' : Number(v).toFixed(d));
const odds = (a) => (a === null || a === undefined ? '—' : (a > 0 ? '+' + a : String(a)));
const sgn = (v, d = 3) => (v === null || v === undefined ? '—' : `<span class="${v > 0 ? 'pos' : v < 0 ? 'neg' : ''}">${v > 0 ? '+' : ''}${Number(v).toFixed(d)}</span>`);
const MARKET = { PTS: 'Points', REB: 'Rebounds', AST: 'Assists', '3PM': 'Threes', STL: 'Steals', BLK: 'Blocks', TO: 'Turnovers', PRA: 'Pts+Reb+Ast', PR: 'Pts+Reb', PA: 'Pts+Ast', RA: 'Reb+Ast' };
const get = async (u) => { const r = await fetch(u, { cache: 'no-store' }); if (!r.ok) throw new Error(u + ' ' + r.status); return r.json(); };
let PROJ = null;

function table(el, cols, rows) {
  el.innerHTML = `<thead><tr>${cols.map((c) => `<th class="${c.l ? 'l' : ''}">${esc(c.h)}</th>`).join('')}</tr></thead>` +
    `<tbody>${rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.l ? 'l' : ''}">${c.f(r)}</td>`).join('')}</tr>`).join('') ||
      `<tr><td class="l muted" colspan="${cols.length}">No rows.</td></tr>`}</tbody>`;
}

async function loadFreshness() {
  try {
    const s = await get('/api/status');
    const snap = s.odds.last_successful_snapshot;
    const run = s.model_run;
    const bits = [snap ? `odds ${num(snap.age_min, 0)} min old` : 'no odds yet', run ? `model run #${run.id} ${esc(run.started_at.slice(11, 16))} UTC` : 'no model run'];
    const f = $('#freshness');
    f.innerHTML = esc(bits.join(' · ')) + (s.warnings.length ? ` · <span class="warn">${s.warnings.length} warning(s)</span>` : '');
    f.classList.toggle('bad', s.warnings.length > 0);
    renderData(s);
    loadHealth().catch(() => {});
  } catch (e) { $('#freshness').textContent = 'backend unreachable: ' + e.message; }
}

function renderData(s) {
  const j = s.jobs || {};
  const jobRows = Object.entries(j).map(([k, v]) => ({ k, ...v }));
  $('#data-body').innerHTML = `
    ${s.warnings.length ? `<div class="banner">${s.warnings.map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join('')}</div>` : '<div class="banner nobet">No freshness warnings.</div>'}
    <div class="cards">
      <div class="card"><h3>Last odds snapshot</h3><div class="big">${s.odds.last_successful_snapshot ? num(s.odds.last_successful_snapshot.age_min, 0) + ' min ago' : '—'}</div><div class="muted">${esc(s.odds.last_successful_snapshot?.finished_at || '')}</div></div>
      <div class="card"><h3>Active pre-game quotes</h3><div class="big">${Object.values(s.odds.active_pregame_quotes_by_bookmaker || {}).reduce((a, b) => a + b, 0)}</div><div class="muted">${esc(JSON.stringify(s.odds.active_pregame_quotes_by_bookmaker))}</div><div class="${s.odds.active_pregame_quotes_flagged_stale ? 'warn' : 'muted'}">${s.odds.active_pregame_quotes_flagged_stale} flagged stale</div></div>
      <div class="card"><h3>Box scores</h3><div class="big">${esc(s.box_scores.latest_date_ingested || '—')}</div><div class="muted">${s.box_scores.player_rows} player-game rows</div></div>
      <div class="card"><h3>Model run</h3><div class="big">${s.model_run ? '#' + s.model_run.id : '—'}</div><div class="muted">${s.model_run ? esc(`${s.model_run.n_players} players · ${s.model_run.n_evaluations} quotes · ${s.model_run.n_candidates} candidates`) : ''}</div></div>
      <div class="card"><h3>Provider usage today</h3><div class="muted">${esc(JSON.stringify(s.odds.usage_by_provider))}</div></div>
    </div>
    <h3>Scheduler health</h3><div id="health-body" class="muted">loading…</div>
    <h3>Jobs</h3><div class="tablewrap"><table id="jobs-table"></table></div>`;
  table($('#jobs-table'), [
    { h: 'Job', l: 1, f: (r) => esc(r.k) }, { h: 'Status', f: (r) => esc(r.status) }, { h: 'Age (min)', f: (r) => num(r.age_min, 0) },
    { h: 'Detail', l: 1, f: (r) => esc((r.detail || '').slice(0, 140)) }], jobRows);
}

async function loadHealth() {
  const r = await get('/api/health');
  const el = $('#health-body');
  if (!el) return;
  el.innerHTML = `<div class="banner ${r.level === 'ok' ? 'nobet' : ''}">Overall: <b class="${r.level === 'ok' ? '' : 'warn'}">${esc(r.level.toUpperCase())}</b> (as of ${esc(r.as_of.slice(0, 16).replace('T', ' '))} UTC)</div><div class="tablewrap"><table id="health-table"></table></div>`;
  table($('#health-table'), [{ h: 'Check', l: 1, f: (c) => esc(c.check) }, { h: 'Level', f: (c) => `<span class="${c.level === 'ok' ? '' : 'warn'}">${esc(c.level)}</span>` },
    { h: 'Detail', l: 1, f: (c) => esc(c.detail) }], r.checks);
}

async function loadProps() {
  const d = $('#f-decision').value, b = $('#f-book').value;
  const r = await get(`/api/props?decision=${encodeURIComponent(d)}&book=${encodeURIComponent(b)}&limit=300`);
  const c = r.counts || {};
  $('#nobet').className = 'banner' + (r.no_bet ? ' nobet' : '');
  $('#nobet').innerHTML = r.run ? (r.no_bet
    ? `<b>NO BET.</b> No prop passed every gate in model run #${r.run.id} (${c.NO_BET || 0} no-bet, ${c.WATCH || 0} watch). That is the expected outcome on most slates.`
    : `<b>${c.PAPER_CANDIDATE} paper candidate(s)</b> in run #${r.run.id} — paper-trade only until the model is validated on live outcomes.`) : 'No model run yet.';
  table($('#props-table'), [
    { h: 'Decision', l: 1, f: (x) => `<span class="pill ${esc(x.decision)}">${esc(x.decision)}</span>` },
    { h: 'Player', l: 1, f: (x) => `<a data-player="${esc(x.athlete_id)}">${esc(x.player_name)}</a>` },
    { h: 'Book', l: 1, f: (x) => esc(x.bookmaker) },
    { h: 'Market', l: 1, f: (x) => esc((MARKET[x.market_code] || x.market_code) + (x.is_alternate ? ' (alt)' : '')) },
    { h: 'Bet', l: 1, f: (x) => esc(`${x.side} ${x.line}`) }, { h: 'Odds', f: (x) => esc(odds(x.american_odds)) },
    { h: 'Model P', f: (x) => pct(x.model_prob) }, { h: '± unc', f: (x) => pct(x.model_prob_sd) },
    { h: 'No-vig P', f: (x) => pct(x.novig_prob) + (x.novig_method === 'book_margin_approx' ? '*' : '') },
    { h: 'Implied', f: (x) => pct(x.implied_prob) }, { h: 'Fair', f: (x) => esc(odds(x.fair_american)) },
    { h: 'EV', f: (x) => sgn(x.ev) }, { h: 'EV (cons.)', f: (x) => sgn(x.ev_low) }, { h: 'Play', f: (x) => pct(x.p_play, 0) },
    { h: 'Conf.', f: (x) => esc(x.confidence) },
    { h: 'Failed gates', l: 1, f: (x) => esc(x.reasons.filter((g) => g.gate && !g.pass).map((g) => g.gate).join(', ') || '—') }], r.rows);
}

async function loadProjections() {
  PROJ = await get('/api/projections');
  $('#player-list').innerHTML = (PROJ.players || []).map((p) => `<option value="${esc(p.player_name)}">`).join('');
  renderProjections();
}
function renderProjections() {
  const q = ($('#f-proj').value || '').toLowerCase();
  const rows = (PROJ?.players || []).filter((p) => !q || p.player_name.toLowerCase().includes(q) || p.team.toLowerCase() === q || p.opponent.toLowerCase() === q);
  const cell = (m) => (p) => p[m] ? `${num(p[m].mean)} <span class="muted">(${num(p[m].p10, 0)}–${num(p[m].p90, 0)})</span>` : '—';
  table($('#proj-table'), [
    { h: 'Player', l: 1, f: (p) => `<a data-player="${esc(p.athlete_id)}">${esc(p.player_name)}</a>` },
    { h: 'Team', l: 1, f: (p) => esc(`${p.team} ${p.is_home ? 'vs' : '@'} ${p.opponent}`) },
    { h: 'Play', f: (p) => pct(p.p_play, 0) + (p.injury_status ? ` <span class="warn">${esc(p.injury_status)}</span>` : '') },
    { h: 'Min', f: cell('MIN') }, { h: 'Pts', f: cell('PTS') }, { h: 'Reb', f: cell('REB') }, { h: 'Ast', f: cell('AST') },
    { h: '3PM', f: cell('3PM') }, { h: 'PRA', f: cell('PRA') }, { h: 'Stl', f: cell('STL') }, { h: 'Blk', f: cell('BLK') },
    { h: 'Games (season/both)', f: (p) => esc(`${p.games_this_season}/${p.games_sample}`) }], rows);
}

function wilson(k, n) {
  if (!n) return [0, 1]; const z = 1.96, p = k / n, d = 1 + z * z / n, c = p + z * z / (2 * n), r = z * Math.sqrt(p * (1 - p) / n + z * z / (4 * n * n));
  return [(c - r) / d, (c + r) / d];
}

async function loadPlayer(aid) {
  const d = await get('/api/player/' + encodeURIComponent(aid));
  document.querySelector('[data-tab="player"]').click();
  $('#f-player').value = d.name || '';
  const n = d.projection_notes || {};
  const w = n.research?.windows || {};
  const winRow = (s, label) => {
    const x = w[s] || {};
    const f = (k) => (x[k] && x[k].n ? `${num(x[k].mean)} <span class="muted">(n=${x[k].n})</span>` : '—');
    return `<tr><td class="l">${esc(label)}</td><td>${f('season')}</td><td>${f('L20')}</td><td>${f('L10')}</td><td>${f('prev_season')}</td><td>${d.distribution[{ pts: 'PTS', reb: 'REB', ast: 'AST', fg3m: '3PM', stl: 'STL', blk: 'BLK', tov: 'TO', minutes: 'MIN' }[s]] ? num(d.distribution[{ pts: 'PTS', reb: 'REB', ast: 'AST', fg3m: '3PM', stl: 'STL', blk: 'BLK', tov: 'TO', minutes: 'MIN' }[s]].mean) : '—'}</td></tr>`;
  };
  const mains = d.evaluations.filter((e) => !e.is_alternate && e.side === 'over');
  const logs = d.logs.filter((g) => g.played);
  const hitRows = mains.map((e) => {
    const comps = { PTS: ['pts'], REB: ['reb'], AST: ['ast'], '3PM': ['fg3m'], STL: ['stl'], BLK: ['blk'], TO: ['tov'], PRA: ['pts', 'reb', 'ast'], PR: ['pts', 'reb'], PA: ['pts', 'ast'], RA: ['reb', 'ast'] }[e.market_code] || [];
    const vals = logs.slice(0, 10).map((g) => comps.reduce((a, c) => a + (g[c] || 0), 0));
    const k = vals.filter((v) => v > e.line).length; const ci = wilson(k, vals.length);
    return { ...e, k, n: vals.length, ci };
  });
  $('#player-body').innerHTML = `
    <h2>${esc(d.name)} <span class="muted">${esc(n.context ? `vs ${n.context.opponent}` : '')}</span></h2>
    <div class="cards">
      <div class="card"><h3>Availability</h3><div class="big">${pct(n.availability?.p_play, 0)}</div><div class="muted">${esc(`injury: ${n.availability?.injury_status || 'none listed'} · recent DNP rate ${pct(n.availability?.dnp_rate, 0)}`)}</div></div>
      <div class="card"><h3>Minutes</h3><div class="big">${num(n.team?.final_minutes_mean)}</div><div class="muted">${esc(`season-weighted ${n.minutes?.cur_weighted_mean} (n=${n.minutes?.cur_games}) · prior ${n.minutes?.prior_mean} · sd ${n.minutes?.sd}`)}</div></div>
      <div class="card"><h3>Context</h3><div class="muted">${esc(`pace ×${n.context?.pace_factor} · home ×${n.context?.home_factor_applied} · B2B ${n.team?.back_to_back} · exp. margin ${n.team?.expected_margin} · team minutes ×${n.team?.minutes_scale_to_240} · usage ×${n.team?.usage_boost}`)}</div></div>
    </div>
    <h3>Inputs by window (sample sizes shown)</h3>
    <div class="tablewrap"><table><thead><tr><th class="l">Stat</th><th>Season</th><th>L20</th><th>L10</th><th>Prev season</th><th>Projection (mean)</th></tr></thead><tbody>
      ${[['minutes', 'Minutes'], ['pts', 'Points'], ['reb', 'Rebounds'], ['ast', 'Assists'], ['fg3m', 'Threes'], ['stl', 'Steals'], ['blk', 'Blocks'], ['tov', 'Turnovers']].map(([s, l]) => winRow(s, l)).join('')}
    </tbody></table></div>
    <p class="hint">The projection is a shrunk blend (current-season games weighted ×5 if in the last 10, ×3 if in games 11–20, ×1 otherwise; prior = last season shrunk to league positional rates) adjusted for context — not any single window. Regular-season games only.</p>
    <h3>Main lines: model vs market</h3>
    <div class="tablewrap"><table id="pl-lines"></table></div>
    <p class="hint">${esc(d.hit_rate_note)} The hit-rate column is the share of the last ${logs.slice(0, 10).length} games over the line, with a 95% interval.</p>
    <h3>Recent games</h3><div class="tablewrap"><table id="pl-logs"></table></div>`;
  table($('#pl-lines'), [
    { h: 'Book', l: 1, f: (e) => esc(e.bookmaker) }, { h: 'Market', l: 1, f: (e) => esc(MARKET[e.market_code] || e.market_code) },
    { h: 'Line', f: (e) => esc(e.line) }, { h: 'Over odds', f: (e) => esc(odds(e.american_odds)) }, { h: 'Model P(over)', f: (e) => pct(e.model_prob) },
    { h: 'No-vig P', f: (e) => pct(e.novig_prob) }, { h: 'EV', f: (e) => sgn(e.ev) }, { h: 'Decision', l: 1, f: (e) => `<span class="pill ${esc(e.decision)}">${esc(e.decision)}</span>` },
    { h: 'L10 hit rate (descriptive)', f: (e) => (e.n ? `${e.k}/${e.n} <span class="muted">[${pct(e.ci[0], 0)}–${pct(e.ci[1], 0)}]</span>` : '—') }], hitRows);
  table($('#pl-logs'), [
    { h: 'Date', l: 1, f: (g) => esc(g.date_et) + (g.season_type === 3 ? ' <span class="muted">playoffs</span>' : g.season_type === 5 ? ' <span class="muted">play-in</span>' : '') }, { h: 'Opp', l: 1, f: (g) => esc((g.is_home ? 'vs ' : '@ ') + g.opponent) },
    { h: 'Start', f: (g) => (g.starter ? 'yes' : '') }, { h: 'Min', f: (g) => (g.played ? num(g.minutes, 0) : esc(g.dnp_reason || 'DNP')) },
    { h: 'Pts', f: (g) => esc(g.pts) }, { h: 'Reb', f: (g) => esc(g.reb) }, { h: 'Ast', f: (g) => esc(g.ast) }, { h: '3PM', f: (g) => esc(g.fg3m) },
    { h: 'FGA', f: (g) => esc(g.fga) }, { h: 'FTA', f: (g) => esc(g.fta) }, { h: 'Stl', f: (g) => esc(g.stl) }, { h: 'Blk', f: (g) => esc(g.blk) }, { h: 'TO', f: (g) => esc(g.tov) }], d.logs);
}

function relChart(model, base) {
  const W = 260, H = 220, P = 30, sx = (v) => P + v * (W - 2 * P), sy = (v) => H - P - v * (H - 2 * P);
  const dots = (rel, color) => (rel || []).map((r) => `<circle cx="${sx(r.mean_pred)}" cy="${sy(r.observed)}" r="${Math.max(2, Math.min(7, Math.sqrt(r.n) / 6))}" fill="${color}" opacity="0.85"><title>${esc(`${r.bin}: pred ${r.mean_pred} obs ${r.observed} n=${r.n}`)}</title></circle>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:440px;height:auto" role="img" aria-label="reliability diagram"><line x1="${sx(0)}" y1="${sy(0)}" x2="${sx(1)}" y2="${sy(1)}" stroke="#555" stroke-dasharray="3 3"/>
    <line x1="${P}" y1="${H - P}" x2="${W - P}" y2="${H - P}" stroke="#444"/><line x1="${P}" y1="${P}" x2="${P}" y2="${H - P}" stroke="#444"/>
    <text x="${W / 2}" y="${H - 6}" text-anchor="middle">predicted</text><text x="10" y="${H / 2}" transform="rotate(-90 10 ${H / 2})" text-anchor="middle">observed</text>
    ${dots(base, '#EF4444')}${dots(model, '#22C55E')}</svg>`;
}

const LABEL = { market: 'Market', sportsbook: 'Sportsbook', probability_bucket: 'Model probability', side: 'Over / under', line_type: 'Main / alternate', model_version: 'Model version', decision: 'Decision' };

function groupTable(id, groups, withRoi) {
  const rows = Object.entries(groups || {}).map(([k, v]) => ({ k, ...v }));
  const cols = [{ h: 'Group', l: 1, f: (r) => esc(MARKET[r.k] || r.k) }, { h: 'n', f: (r) => r.n },
    { h: 'W-L-P-V', f: (r) => `${r.wins}-${r.losses}-${r.pushes}-${r.voids}` },
    { h: 'Model prob', f: (r) => pct(r.mean_model_prob) }, { h: 'Observed', f: (r) => pct(r.observed_win_rate) },
    { h: 'Brier model', f: (r) => num(r.brier_model, 4) }, { h: 'Brier no-vig', f: (r) => num(r.brier_market_novig, 4) },
    { h: 'CLV (n)', f: (r) => r.clv?.n ? `${sgn(r.clv.mean_prob, 4)} (${r.clv.n})` : '—' }, { h: 'Beat close', f: (r) => pct(r.clv?.pct_beat_close, 0) }];
  if (withRoi) cols.push({ h: 'Units', f: (r) => sgn(r.profit_units, 2) }, { h: 'ROI', f: (r) => pct(r.roi) },
    { h: 'ROI 95% CI', f: (r) => (r.roi_ci95 ? `${pct(r.roi_ci95[0])} … ${pct(r.roi_ci95[1])}` : '—') });
  table($('#' + id), cols, rows);
}

async function loadPaper() {
  const d = await get('/api/paper');
  const p = d.paper, aq = d.all_quotes, ev = p.evidence_status, o = p.overall || {};
  let h = `<h2><span class="label-real">REAL</span> paper-trading ledger</h2>
    <div class="banner">${esc(p.track)} Real-money betting: <b>${esc(p.real_money_betting)}</b>. Profitability claim: <b>${esc(p.profitability_claim)}</b>.
    Evidence: <b>${ev.decided_results} of ${ev.minimum_before_any_review}</b> decided results needed before any review (${esc(ev.status.replaceAll('_', ' '))}). ${esc(ev.note)}</div>
    <div class="cards">
      <div class="card"><h3>Recommendations</h3><div class="big">${p.n_recommendations}</div><div class="muted">${p.n_graded} graded · ${p.n_pending} pending · ${p.n_independent_positions} independent positions</div></div>
      <div class="card"><h3>Record (W-L-P-V)</h3><div class="big">${o.n ? `${o.wins}-${o.losses}-${o.pushes}-${o.voids}` : '—'}</div><div class="muted">at the price captured when recommended</div></div>
      <div class="card"><h3>Paper ROI</h3><div class="big">${o.n ? pct(o.roi) : '—'}</div><div class="muted">${o.roi_ci95 ? `95% CI ${pct(o.roi_ci95[0])} … ${pct(o.roi_ci95[1])}` : 'needs ≥2 settled'} · ${o.n ? sgn(o.profit_units, 2) + ' units' : ''}</div></div>
      <div class="card"><h3>Brier model / no-vig</h3><div class="big">${num(o.brier_model, 4)}</div><div class="muted">market no-vig ${num(o.brier_market_novig, 4)} (lower is better)</div></div>
      <div class="card"><h3>Closing-line value</h3><div class="big">${o.clv?.n ? sgn(o.clv.mean_prob, 4) : '—'}</div><div class="muted">${o.clv?.n ? `${pct(o.clv.pct_beat_close, 0)} beat the close (n=${o.clv.n})` : 'no closing prices yet'}</div></div>
    </div>`;
  if (!p.n_graded) h += `<div class="banner nobet">No graded paper recommendations yet. A recommendation is graded once its game is final and ESPN's box score has been ingested (the scheduler does this). Until hundreds exist, these numbers say nothing about profitability.</div>`;
  h += Object.keys(p.by || {}).map((k) => `<h3>By ${esc(LABEL[k] || k)}</h3><div class="tablewrap"><table id="pg-${k}"></table></div>`).join('');
  h += `<h3>Latest recommendations (immutable)</h3><div class="tablewrap"><table id="paper-recent"></table></div>`;
  h += `<h2><span class="label-real">REAL</span> all-quotes calibration track</h2><p class="hint">${esc(aq.track)}. ${aq.n_decided || 0} decided quotes (each quote's last pre-tip evaluation).</p>`;
  if (aq.n_decided) h += `<div class="cards"><div class="card"><h3>Reliability</h3>${relChart(aq.model?.reliability, aq.market_novig_baseline?.reliability)}<div class="muted">green = model, red = market no-vig</div></div>
     <div class="card"><h3>Brier</h3><div class="muted">${esc(`model ${aq.model?.brier} · market no-vig ${aq.market_novig_baseline?.brier ?? '—'} · constant ${aq.model?.brier_constant_baseline}`)}</div><div class="muted">${esc(`ECE model ${aq.model?.ece} · no-vig ${aq.market_novig_baseline?.ece ?? '—'}`)}</div></div></div>` +
    Object.keys(aq.by || {}).map((k) => `<h3>By ${esc(LABEL[k] || k)}</h3><div class="tablewrap"><table id="aq-${k}"></table></div>`).join('');
  $('#paper-body').innerHTML = h;
  Object.entries(p.by || {}).forEach(([k, g]) => groupTable('pg-' + k, g, true));
  Object.entries(aq.by || {}).forEach(([k, g]) => groupTable('aq-' + k, g, false));
  table($('#paper-recent'), [
    { h: 'Recommended (UTC)', l: 1, f: (r) => esc((r.recommended_at || '').slice(0, 16).replace('T', ' ')) },
    { h: 'Player', l: 1, f: (r) => `<a data-player="${esc(r.athlete_id)}">${esc(r.player_name)}</a>` }, { h: 'Book', l: 1, f: (r) => esc(r.bookmaker) },
    { h: 'Market', l: 1, f: (r) => esc(MARKET[r.market_code] || r.market_code) + (r.is_alternate ? ' (alt)' : '') },
    { h: 'Side', l: 1, f: (r) => esc(r.side) }, { h: 'Line', f: (r) => num(r.line, 1) }, { h: 'Odds', f: (r) => odds(r.american_odds) },
    { h: 'Proj.', f: (r) => num(r.projection_mean, 1) }, { h: 'Model', f: (r) => pct(r.model_prob) }, { h: 'No-vig', f: (r) => pct(r.novig_prob) },
    { h: 'EV', f: (r) => sgn(r.ev, 3) }, { h: 'Model ver.', l: 1, f: (r) => esc(r.model_version) },
    { h: 'Result', f: (r) => esc(r.result || 'pending') }, { h: 'Actual', f: (r) => num(r.actual, 0) },
    { h: 'Units', f: (r) => (r.profit_units === null || r.profit_units === undefined ? '—' : sgn(r.profit_units, 2)) },
    { h: 'Close', f: (r) => odds(r.closing_american) }, { h: 'CLV', f: (r) => sgn(r.clv_prob, 4) }], p.recent || []);
}

async function loadPerf() {
  const d = await get('/api/simulated');
  const bt = d.backtest;
  let h = `<div class="banner"><span class="label-sim">SIMULATED</span> ${esc(d.label)} Real paper results are on the Paper trading tab and are never combined with these.</div>`;
  h += `<h2><span class="label-sim">SIMULATED LINES</span> walk-forward projection backtest</h2>`;
  if (!bt) { h += '<div class="banner nobet">No backtest run yet (`nba-edge backtest`).</div>'; $('#perf-body').innerHTML = h; return; }
  const s = bt.summary || {};
  h += `<p class="hint">${esc(bt.label)}. Lines: ${esc(s.line_method)}. ${esc(bt.date_from)} → ${esc(bt.date_to)}, ${bt.n_rows} player-market rows, model ${esc(bt.model_version)}. Green = model, red = raw L10 hit rate used as a probability (what this project avoids).</p>`;
  const a = s.all_markets || {};
  h += `<div class="cards"><div class="card"><h3>All markets — reliability</h3>${relChart(a.model?.reliability, a.raw_L10_hit_rate_baseline?.reliability)}</div>
     <div class="card"><h3>All markets</h3><div class="muted">${esc(`model Brier ${a.model?.brier} · ECE ${a.model?.ece} · log loss ${a.model?.log_loss}`)}</div><div class="muted">${esc(`raw hit rate Brier ${a.raw_L10_hit_rate_baseline?.brier} · ECE ${a.raw_L10_hit_rate_baseline?.ece} · log loss ${a.raw_L10_hit_rate_baseline?.log_loss}`)}</div><div class="muted">${esc(`constant base-rate Brier ${a.model?.brier_constant_baseline}`)}</div></div></div>
     <div class="tablewrap"><table id="bt-table"></table></div>`;
  $('#perf-body').innerHTML = h;
  const rows = Object.entries(s.markets || {}).map(([k, v]) => ({ k, ...v }));
  table($('#bt-table'), [
    { h: 'Market', l: 1, f: (r) => esc(MARKET[r.k] || r.k) }, { h: 'n', f: (r) => r.n }, { h: 'MAE (mean)', f: (r) => num(r.mae_mean, 2) },
    { h: 'Bias', f: (r) => sgn(r.bias_mean_minus_actual, 2) }, { h: 'Brier model', f: (r) => num(r.model.brier, 4) },
    { h: 'Brier hit-rate', f: (r) => num(r.raw_L10_hit_rate_baseline?.brier, 4) }, { h: 'Brier const.', f: (r) => num(r.model.brier_constant_baseline, 4) },
    { h: 'ECE model', f: (r) => num(r.model.ece, 3) }, { h: 'ECE hit-rate', f: (r) => num(r.raw_L10_hit_rate_baseline?.ece, 3) },
    { h: 'PIT deciles (flat = calibrated)', l: 1, f: (r) => esc((r.pit_deciles || []).join(' ')) }], rows);
}

document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-tab]');
  if (t) {
    document.querySelectorAll('nav button').forEach((b) => b.classList.toggle('active', b === t));
    document.querySelectorAll('.tab').forEach((s) => s.classList.toggle('active', s.id === 'tab-' + t.dataset.tab));
    if (t.dataset.tab === 'perf') loadPerf().catch((er) => ($('#perf-body').textContent = er.message));
    if (t.dataset.tab === 'paper') loadPaper().catch((er) => ($('#paper-body').textContent = er.message));
    if (t.dataset.tab === 'data') loadHealth().catch((er) => ($('#health-body').textContent = er.message));
  }
  const p = e.target.closest('[data-player]');
  if (p) { e.preventDefault(); loadPlayer(p.dataset.player).catch((er) => ($('#player-body').textContent = er.message)); }
});
$('#f-decision').addEventListener('change', loadProps);
$('#f-book').addEventListener('change', loadProps);
$('#f-proj').addEventListener('input', renderProjections);
$('#f-player').addEventListener('change', () => {
  const p = (PROJ?.players || []).find((x) => x.player_name.toLowerCase() === $('#f-player').value.toLowerCase());
  if (p) loadPlayer(p.athlete_id);
});
loadFreshness(); loadProps().catch((e) => ($('#nobet').textContent = e.message)); loadProjections().catch(() => {});
setInterval(loadFreshness, 60000);
