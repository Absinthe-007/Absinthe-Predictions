/* Absinthe Predictions — static front end (reads ./data/*.json) */
(() => {
const $app = document.getElementById("app");
const cache = {};
const TZ = "America/Chicago";
const LABELS = {pass_yds:"Pass Yds",att:"Att",cmp:"Cmp",pass_td:"Pass TD",int:"INT",rush_att:"Car",rush_yds:"Rush Yds",
  rush_td:"Rush TD",tgt:"Tgt",rec:"Rec",rec_yds:"Rec Yds",rec_td:"Rec TD",fg:"FG",xp:"XP",tackles:"Tkl",sacks:"Sacks",def_int:"INT"};
const GROUPS = [
  {name:"Quarterback", pos:["QB"], keys:["cmp","att","pass_yds","pass_td","int","rush_yds","rush_td"]},
  {name:"Running backs", pos:["RB"], keys:["rush_att","rush_yds","rush_td","tgt","rec","rec_yds","rec_td"]},
  {name:"Receivers", pos:["WR","TE"], keys:["tgt","rec","rec_yds","rec_td"]},
  {name:"Kicker", pos:["K"], keys:["fg","xp"]},
  {name:"Defense", pos:["LB","DB","DL"], keys:["tackles","sacks","def_int"]},
];
const OUT = new Set(["Out","Injured Reserve","IR","PUP","Suspension","Doubtful-Out"]);

const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const f1 = v => v == null ? "–" : (Math.round(v * 10) / 10).toFixed(1).replace(/\.0$/, "");
const pct = v => v == null ? "–" : `${f1(v)}%`;
async function get(path) {
  if (cache[path]) return cache[path];
  const r = await fetch(`data/${path}?v=${(cache.meta && cache.meta.updated) || Date.now()}`);
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return (cache[path] = await r.json());
}
const gcls = g => g ? "g" + g[0] : "";
const gradeBadge = g => g ? `<span class="grade ${gcls(g)}">${esc(g)}</span>` : "";
const when = iso => new Date(iso).toLocaleString("en-US", {timeZone: TZ, weekday:"short", hour:"numeric", minute:"2-digit"});
const dayKey = iso => new Date(iso).toLocaleDateString("en-US", {timeZone: TZ, weekday:"long", month:"short", day:"numeric"});
function slot(iso) {
  const d = new Date(iso);
  const wd = d.toLocaleDateString("en-US", {timeZone: TZ, weekday:"short"});
  const h = +d.toLocaleString("en-US", {timeZone: TZ, hour:"numeric", hour12:false});
  if (wd === "Thu") return "Thursday Night";
  if (wd === "Mon") return "Monday Night";
  if (wd === "Sun" && h >= 19) return "Sunday Night";
  return null;
}
function injTag(p) {
  if (!p.status || p.status === "Active") return "";
  const o = OUT.has(p.status);
  const short = {"Injured Reserve":"IR","Questionable":"Q","Doubtful":"D","Out":"OUT","Suspension":"SUSP"}[p.status] || p.status;
  return `<span class="inj ${o ? "o" : "q"}" title="${esc(p.status)}${p.detail ? " – " + esc(p.detail) : ""}">${esc(short)}</span>`;
}
function setNav(k) { document.querySelectorAll("nav a").forEach(a => a.classList.toggle("on", a.dataset.nav === k)); }

// ------------------------------------------------------------------ games list
async function viewWeek(key) {
  setNav("week");
  const [meta, weeks] = await Promise.all([get("meta.json"), get("weeks.json")]);
  key = key || meta.current;
  const wk = await get(`weeks/${key}.json`);
  const graded = wk.games.filter(g => g.grade);
  const w = graded.filter(g => g.grade.winner_correct).length;
  let sh = 0, sn = 0;
  graded.forEach(g => ["home","away"].forEach(s => g.players[s].forEach(p => { if (p.grade && !p.grade.dnp) { sh += p.grade.hits; sn += p.grade.n; } })));
  const avg = graded.length ? graded.reduce((a, g) => a + g.grade.score, 0) / graded.length : null;
  const opts = weeks.slice().reverse().map(x => `<option value="${x.key}" ${x.key === key ? "selected" : ""}>${x.season} ${esc(x.label)}${x.backtest ? " (backtest)" : ""}</option>`).join("");
  let html = `<div class="ph"><div><h1>${wk.season} ${esc(wk.label)}</h1>
    <div class="sub">Predicted final scores and player stat lines for every game${wk.backtest ? " · <span class='pill bt'>Backtest: model run as if before kickoff</span>" : ""}</div></div>
    <select id="wsel" aria-label="Week">${opts}</select></div>
  <div class="strip">
    <div class="stat"><div class="k">Games</div><div class="v">${wk.games.length}</div><div class="s">${graded.length} graded</div></div>
    <div class="stat"><div class="k">Winners picked</div><div class="v g">${graded.length ? `${w}-${graded.length - w}` : "–"}</div><div class="s">${graded.length ? pct(100 * w / graded.length) : "after games finish"}</div></div>
    <div class="stat"><div class="k">Player stat hit rate</div><div class="v g">${sn ? pct(100 * sh / sn) : "–"}</div><div class="s">${sn ? `${sh} of ${sn} key stats` : "after games finish"}</div></div>
    <div class="stat"><div class="k">Avg game grade</div><div class="v">${avg != null ? f1(avg) : "–"}</div><div class="s">0–100 score</div></div>
  </div>`;
  const byDay = {};
  wk.games.forEach(g => (byDay[dayKey(g.date)] ||= []).push(g));
  for (const [d, gs] of Object.entries(byDay)) {
    html += `<div class="day">${esc(d)}</div><div class="games">${gs.map(g => gameCard(g, key)).join("")}</div>`;
  }
  $app.innerHTML = html;
  document.getElementById("wsel").onchange = e => location.hash = `#/week/${e.target.value}`;
}

function gameCard(g, key) {
  const p = g.pred || {};
  const act = g.actual || g.live_score;
  const homeWin = (p.home ?? 0) > (p.away ?? 0);
  const wph = Math.round(100 * (p.win_prob_home ?? .5));
  const sl = slot(g.date);
  let status;
  if (g.completed) status = `<span class="pill">Final</span>`;
  else if (g.state === "in") status = `<span class="pill live">● Live</span>`;
  else status = `<span>${esc(when(g.date))}</span>`;
  const row = (side) => {
    const ab = g[side], win = side === "home" ? homeWin : !homeWin;
    return `<div class="team ${win ? "win" : ""}"><img src="${esc(g[side + "_logo"])}" alt="" loading="lazy">
      <div class="nm">${esc(ab)}<span class="rec">${esc(g[side + "_record"] || "")}</span></div>
      <div class="pr">${p[side] ?? "–"}</div><div class="ac">${act ? act[side] : ""}</div></div>`;
  };
  const v = g.vegas;
  const flags = [g.backtest ? `<span class="pill bt">Backtest</span>` : "", g.late ? `<span class="pill bt" title="Prediction created after kickoff; not counted in official record">Late</span>` : ""].join("");
  return `<a class="gc" href="#/game/${key}/${g.id}">
    <div class="gc-top"><span>${sl ? `<span class="tag">${sl}</span> · ` : ""}${esc(g.broadcast || "")}</span>${status}</div>
    <div class="colhd"><span></span><span></span><span>Pred</span><span>${act ? (g.completed ? "Final" : "Now") : ""}</span></div>
    ${row("away")}${row("home")}
    <div class="wp" title="Win probability"><i style="width:${100 - wph}%;opacity:.45"></i><i style="width:${wph}%"></i></div>
    <div class="gc-bot"><span>${esc(g.away)} ${100 - wph}% · ${esc(g.home)} ${wph}%${v ? ` · Vegas ${esc(v.text || "")} / ${v.total}` : ""}</span>
      <span>${flags} ${g.grade ? `${g.grade.winner_correct ? "<span class='ok'>✓ winner</span>" : "<span class='bad'>✗ winner</span>"} ${gradeBadge(g.grade.grade)}` : ""}</span></div>
  </a>`;
}

// ------------------------------------------------------------------ game page
async function viewGame(key, id, side) {
  setNav("week");
  const wk = await get(`weeks/${key}.json`);
  const g = wk.games.find(x => x.id === id);
  if (!g) { $app.innerHTML = `<div class="empty">Game not found.</div>`; return; }
  side = side || "away";
  const p = g.pred, act = g.actual || g.live_score, v = g.vegas, m = g.model;
  const hw = p.home > p.away;
  const team = (s) => `<div class="sh-team ${(s === "home") === hw ? "win" : ""}"><img src="${esc(g[s + "_logo"])}" alt="">
     <div class="nm">${esc(g[s + "_name"])}</div><div class="big">${p[s]}</div>
     <div class="muted">${act ? `${g.completed ? "Final" : "Now"}: <b>${act[s]}</b>` : esc(g[s + "_record"] || "")}</div></div>`;
  let html = `<a class="crumb" href="#/week/${key}">← ${esc(wk.label)} games</a>
  <div class="ph"><div><h1>${esc(g.away)} @ ${esc(g.home)}</h1><div class="sub">${esc(when(g.date))} CT · ${esc(g.venue || "")}${g.indoor ? " (indoor)" : g.weather ? ` · ${esc(g.weather)}${g.temp != null ? `, ${g.temp}°` : ""}` : ""}${g.broadcast ? " · " + esc(g.broadcast) : ""}</div></div>
   <div>${g.grade ? gradeBadge(g.grade.grade) : ""}</div></div>
  <div class="score-hero">${team("away")}<div class="sh-mid"><div>PREDICTED</div><div class="final">${esc(p.pick)} by ${f1(Math.abs(p.home_raw - p.away_raw))}</div>
     <div>Win prob ${esc(g.home)} ${Math.round(100 * p.win_prob_home)}%</div><div>Total ${f1(p.total)}</div></div>${team("home")}</div>
  <div class="facts">
    <div class="stat"><div class="k">Vegas line</div><div class="v">${v ? esc(v.text || "PK") : "–"}</div><div class="s">${v ? `O/U ${v.total} → implied ${f1(v.away)}-${f1(v.home)}` : "no line available"}</div></div>
    <div class="stat"><div class="k">Team-rating model</div><div class="v">${f1(m.away)}-${f1(m.home)}</div><div class="s">opponent-adjusted scoring${m.qb_out_home || m.qb_out_away ? " · starting QB out adj." : ""}</div></div>
    <div class="stat"><div class="k">Blend</div><div class="v">${f1(p.away_raw)}-${f1(p.home_raw)}</div><div class="s">${v ? "70% Vegas / 30% model" : "model only"}</div></div>
    ${g.grade ? `<div class="stat"><div class="k">Grade</div><div class="v">${f1(g.grade.score)}</div><div class="s">${g.grade.winner_correct ? "✓ winner" : "✗ winner"} · margin off ${g.grade.margin_err} · total off ${g.grade.total_err}${g.grade.beat_vegas_margin != null ? ` · ${g.grade.beat_vegas_margin ? "beat" : "lost to"} Vegas` : ""}</div></div>` : ""}
  </div>
  ${g.backtest ? `<p class="note"><span class="pill bt">Backtest</span> Built after the fact using only stats from before this game. Injuries come from who actually played.</p>` : ""}
  ${g.late ? `<p class="note"><span class="pill bt">Late</span> This prediction was created after kickoff (site launch), so it isn't counted in the official record.</p>` : ""}
  <div class="tabs">${["away","home"].map(s => `<button data-side="${s}" class="${s === side ? "on" : ""}"><img src="${esc(g[s + "_logo"])}" alt="">${esc(g[s + "_name"])}</button>`).join("")}</div>
  <div id="ptables"></div>`;
  $app.innerHTML = html;
  const draw = (s) => {
    document.getElementById("ptables").innerHTML = playerTables(g, s);
    document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.side === s));
  };
  document.querySelectorAll(".tabs button").forEach(b => b.onclick = () => { history.replaceState(null, "", `#/game/${key}/${id}/${b.dataset.side}`); draw(b.dataset.side); });
  draw(side);
}

function playerTables(g, side) {
  const players = g.players[side];
  const opp = side === "home" ? g.away : g.home;
  let html = "";
  const info = (g.team_info || {})[side];
  for (const grp of GROUPS) {
    const ps = players.filter(p => grp.pos.includes(p.pos) && !p.out);
    if (!ps.length) continue;
    const graded = ps.some(p => p.grade && !p.grade.dnp);
    html += `<div class="card"><h3>${grp.name}${grp.name === "Quarterback" && info ? `<small>team: ${info.pass_att} pass att · ${info.rush_att} rush att projected</small>` : ""}</h3><div class="tw"><table>
      <thead><tr><th>Player</th>${grp.keys.map(k => `<th>${LABELS[k]}</th>`).join("")}<th title="Games vs ${esc(opp)} since 2018">vs ${esc(opp)}</th>${graded ? "<th>Hit</th><th>Grade</th>" : ""}</tr></thead><tbody>`;
    for (const p of ps) {
      const gr = p.grade && !p.grade.dnp ? p.grade : null;
      html += `<tr><td class="pl"><a href="#/player/${p.pid}">${esc(p.name)}</a><span class="ps">${esc(p.pos)}</span>${injTag(p)}</td>`;
      for (const k of grp.keys) {
        const pv = p.proj[k];
        if (pv == null) { html += "<td class='dim'>–</td>"; continue; }
        let cell = f1(pv);
        if (gr && gr.stats[k]) {
          const s = gr.stats[k];
          cell += `<span class="act ${s.hit ? "ok" : "bad"}">${f1(s.actual)} ${s.hit ? "✓" : "✗"}</span>`;
        }
        html += `<td>${cell}</td>`;
      }
      html += `<td class="muted">${p.vs_games || 0}</td>`;
      if (graded) html += gr ? `<td>${gr.hits}/${gr.n}</td><td>${gradeBadge(gr.grade)}</td>` : `<td class="dim" colspan="2">${p.grade ? "DNP" : ""}</td>`;
      html += "</tr>";
    }
    html += "</tbody></table></div></div>";
  }
  const outs = players.filter(p => p.out);
  if (outs.length) html += `<div class="card"><h3>Out this week<small>their usage is redistributed to healthy teammates</small></h3><div class="tw"><table><tbody>${outs.map(p => `<tr><td class="pl"><a href="#/player/${p.pid}">${esc(p.name)}</a><span class="ps">${esc(p.pos)}</span>${injTag(p)}</td><td class="muted">${esc(p.detail || "")}</td></tr>`).join("")}</tbody></table></div></div>`;
  return html || `<div class="empty">No player projections.</div>`;
}

// ------------------------------------------------------------------ player page
async function viewPlayer(pid, oppSel) {
  setNav("players");
  let pl;
  try { pl = await get(`players/${pid}.json`); } catch { $app.innerHTML = `<div class="empty">Player not found.</div>`; return; }
  const keys = pl.keys;
  const log = pl.log || [];
  const nx = pl.next || {};
  const opps = [...new Set(log.map(r => r.op))].sort();
  const opp = oppSel || nx.opp || opps[0];
  const bio = pl.bio || {};
  const bioTxt = [bio.height ? `${Math.floor(bio.height / 12)}'${bio.height % 12}"` : null, bio.weight ? `${bio.weight} lbs` : null,
    bio.college_name, bio.draft_year ? `Drafted ${bio.draft_year}${bio.draft_round ? ` R${bio.draft_round}` : ""}${bio.draft_pick ? ` #${bio.draft_pick}` : ""}` : "Undrafted/unknown",
    bio.years_of_experience != null ? `${bio.years_of_experience} yrs exp` : null].filter(Boolean).join(" · ");

  const avg = rows => { const o = {}; keys.forEach(k => o[k] = rows.length ? rows.reduce((a, r) => a + (r[k] || 0), 0) / rows.length : null); return o; };
  const vsRows = log.filter(r => r.op === opp);
  const vsAvg = avg(vsRows), careerAvg = avg(log), last5 = avg(log.slice(-5));
  const seasons = [...new Set(log.map(r => r.s))].sort((a, b) => b - a);

  const proj = nx.proj || {};
  const rec = pl.record;
  const statRow = (label, o, n) => `<tr><td>${label}</td>${keys.map(k => `<td>${o[k] == null ? "–" : f1(o[k])}</td>`).join("")}<td class="muted">${n}</td></tr>`;
  const tableHead = `<tr><th></th>${keys.map(k => `<th>${LABELS[k]}</th>`).join("")}<th>G</th></tr>`;
  const logTable = rows => rows.length ? `<div class="tw"><table><thead><tr><th>Game</th>${keys.map(k => `<th>${LABELS[k]}</th>`).join("")}</tr></thead><tbody>${rows.slice().reverse().map(r => `<tr><td>${r.s} Wk ${r.w} <span class="dim">${esc(r.tm)} vs ${esc(r.op)}</span></td>${keys.map(k => `<td>${f1(r[k])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : `<div class="empty">No games.</div>`;

  let html = `<a class="crumb" href="javascript:history.back()">← Back</a>
  <div class="p-hero">${pl.headshot ? `<img src="${esc(pl.headshot)}" alt="">` : ""}<div>
    <h1>${esc(pl.name)}</h1><div class="meta">${esc(pl.pos)} · ${esc(pl.team)}${bio.jersey_number ? ` · #${bio.jersey_number}` : ""} ${injTag(pl)}${pl.detail && pl.status !== "Active" ? ` <span class="dim">${esc(pl.detail)}</span>` : ""}</div>
    <div class="bio">${esc(bioTxt)}</div></div></div>

  <div class="grid2" style="margin-top:14px">
   <div class="card"><h3>${esc(nx.label || "")} projection<small>${nx.home ? "vs" : "@"} ${esc(nx.opp || "")}</small></h3>
     ${Object.keys(proj).length ? `<div class="projrow">${keys.filter(k => proj[k] != null).map(k => `<div class="pbox"><div class="k">${LABELS[k]}</div><div class="v">${f1(proj[k])}</div><div class="c">career vs ${esc(nx.opp)}: ${vsRowsFor(log, nx.opp, k)}</div></div>`).join("")}</div>` : `<div class="empty">${OUT.has(pl.status) ? "Ruled out — no projection." : "No projection this week."}</div>`}
   </div>
   <div class="card"><h3>Absinthe hit rate on ${esc(pl.name.split(" ").slice(-1)[0])}<small>key stats within the hit band</small></h3>
     ${rec ? `<div class="projrow"><div class="pbox"><div class="k">Hit rate</div><div class="v" style="color:var(--green)">${pct(rec.hit_pct)}</div><div class="c">${rec.hits}/${rec.n} stats</div></div>
       <div class="pbox"><div class="k">Games graded</div><div class="v">${rec.games}</div></div><div class="pbox"><div class="k">Avg grade</div><div class="v">${f1(rec.avg_score)}</div></div></div>
       <div class="tw"><table><thead><tr><th>Week</th><th>Opp</th><th>Hits</th><th>Hit %</th><th>Running %</th><th>Grade</th></tr></thead><tbody>
       ${rec.weeks.slice().reverse().map(w => `<tr><td>${w.season} ${esc(w.label)}${w.backtest ? " <span class='pill bt'>BT</span>" : ""}</td><td>${esc(w.opp)}</td><td>${w.hits}/${w.n}</td><td>${pct(w.hit_pct)}</td><td>${pct(w.running_hit_pct)}</td><td>${gradeBadge(w.grade)}</td></tr>`).join("")}
       </tbody></table></div>` : `<div class="empty">No graded games yet.</div>`}
   </div>
  </div>

  <div class="card"><h3>History vs opponent<small>every game since 2018</small></h3>
    <div class="oppsel"><label class="muted" for="osel">Opponent</label><select id="osel">${opps.map(o => `<option ${o === opp ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>
      <span class="muted">${vsRows.length} game${vsRows.length === 1 ? "" : "s"}</span></div>
    <div class="tw"><table><thead>${tableHead}</thead><tbody>
      ${statRow(`Avg vs ${esc(opp)}`, vsAvg, vsRows.length)}${statRow("Career avg", careerAvg, log.length)}${statRow("Last 5", last5, Math.min(5, log.length))}
    </tbody></table></div>
    ${logTable(vsRows)}
  </div>

  <div class="card"><h3>Season by season</h3><div class="tw"><table><thead>${tableHead}</thead><tbody>
    ${seasons.map(s => { const rs = log.filter(r => r.s === s); return statRow(`${s} <span class="dim">${esc([...new Set(rs.map(r => r.tm))].join("/"))}</span>`, avg(rs), rs.length); }).join("")}
  </tbody></table></div></div>

  <div class="card"><h3>Last 10 games</h3>${logTable(log.slice(-10))}</div>`;
  $app.innerHTML = html;
  document.getElementById("osel").onchange = e => { history.replaceState(null, "", `#/player/${pid}/${e.target.value}`); viewPlayer(pid, e.target.value); };
}
function vsRowsFor(log, opp, k) {
  const r = log.filter(x => x.op === opp);
  return r.length ? `${f1(r.reduce((a, x) => a + (x[k] || 0), 0) / r.length)} (${r.length}g)` : "none";
}

// ------------------------------------------------------------------ accuracy
async function viewAccuracy() {
  setNav("accuracy");
  const a = await get("accuracy.json");
  const T = a.totals || {};
  const off = T.official, all = T.all;
  const box = (k, v, s, g) => `<div class="stat"><div class="k">${k}</div><div class="v ${g ? "g" : ""}">${v}</div><div class="s">${s}</div></div>`;
  let html = `<div class="ph"><div><h1>Accuracy</h1><div class="sub">Every prediction is locked at kickoff and graded after the game. Running tally by week.</div></div></div>
  <div class="strip">
    ${box("Winners (official)", off && off.games ? pct(off.winner_pct) : "–", off && off.games ? `${off.games} live games graded` : "starts with first live week", true)}
    ${box("Stat hit rate (official)", off && off.stats ? pct(off.stat_hit_pct) : "–", off && off.stats ? `${off.stat_hits}/${off.stats} key stats` : "–", true)}
    ${box("Winners (incl. backtest)", all ? pct(all.winner_pct) : "–", all ? `${all.games} games` : "–")}
    ${box("Stat hit rate (incl. backtest)", all ? pct(all.stat_hit_pct) : "–", all ? `${all.stat_hits}/${all.stats} key stats` : "–")}
  </div>
  <div class="card"><h3>Week by week<small>running totals include backtest weeks</small></h3><div class="tw"><table>
    <thead><tr><th>Week</th><th>Games</th><th>Winners</th><th>Win %</th><th>Avg game grade</th><th>Beat Vegas*</th><th>Stat hits</th><th>Hit %</th><th>Running win %</th><th>Running hit %</th></tr></thead><tbody>
    ${a.weeks.slice().reverse().map(w => `<tr><td>${w.season} ${esc(w.label)} ${w.backtest ? "<span class='pill bt'>Backtest</span>" : ""}${w.late ? ` <span class='pill bt' title='games predicted after kickoff'>${w.late} late</span>` : ""}</td>
      <td>${w.games}</td><td>${w.winners}-${w.games - w.winners}</td><td>${pct(w.winner_pct)}</td><td>${f1(w.avg_game_score)}</td><td>${w.beat_vegas}/${w.games}</td>
      <td>${w.stat_hits}/${w.stats}</td><td>${pct(w.stat_hit_pct)}</td><td>${pct(w.run_all.winner_pct)}</td><td><b>${pct(w.run_all.stat_hit_pct)}</b></td></tr>`).join("")}
  </tbody></table></div></div>
  <p class="note">*Beat Vegas = our predicted margin was closer to the real margin than the Vegas spread.</p>
  <div class="grid2">
    <div class="card"><h3>Hit rate by stat</h3><div class="tw"><table><thead><tr><th>Stat</th><th>Hits</th><th>Hit %</th><th></th></tr></thead><tbody>
      ${a.by_stat.map(s => `<tr><td>${esc(s.label)}</td><td>${s.hits}/${s.n}</td><td>${pct(s.pct)}</td><td><div class="bar"><i style="width:${s.pct}%"></i></div></td></tr>`).join("")}
    </tbody></table></div></div>
    <div class="card"><h3>Hit rate by position<small>key stats only</small></h3><div class="tw"><table><thead><tr><th>Pos</th><th>Hits</th><th>Hit %</th><th></th></tr></thead><tbody>
      ${a.by_pos.map(s => `<tr><td>${esc(s.pos)}</td><td>${s.hits}/${s.n}</td><td>${pct(s.pct)}</td><td><div class="bar"><i style="width:${s.pct}%"></i></div></td></tr>`).join("")}
    </tbody></table></div></div>
  </div>
  ${leaderTable("Most predictable players", (a.leaders || []).slice(0, 25))}
  ${leaderTable("Hardest to predict", (a.leaders || []).slice(-25).reverse())}
  <p class="note">A stat is a <b>hit</b> when the real number lands within a band around the projection (e.g. receiving yards within ±15 yds or ±25%, receptions within ±1.5, passing yards within ±30 or ±15%, tackles within ±1.5). Hit rate counts yardage and volume stats where the projection is big enough to matter; TDs, sacks and INTs are graded and shown but kept out of the headline number because "0" is right most of the time.</p>`;
  $app.innerHTML = html;
}
function leaderTable(title, rows) {
  return `<div class="card"><h3>${title}<small>min. 2 graded games</small></h3><div class="tw"><table><thead><tr><th>Player</th><th>Team</th><th>Games</th><th>Hits</th><th>Hit %</th><th>Avg grade</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td class="pl"><a href="#/player/${r.pid}">${esc(r.name)}</a><span class="ps">${esc(r.pos)}</span></td><td>${esc(r.team)}</td><td>${r.games}</td><td>${r.hits}/${r.n}</td><td>${pct(r.hit_pct)}</td><td>${f1(r.avg_score)}</td></tr>`).join("") || `<tr><td class="empty" colspan="6">Not enough graded games yet.</td></tr>`}
  </tbody></table></div></div>`;
}

// ------------------------------------------------------------------ players search
async function viewPlayers(q = "") {
  setNav("players");
  const idx = await get("players_index.json");
  $app.innerHTML = `<div class="ph"><div><h1>Players</h1><div class="sub">${idx.length} players with projections. Click any player for his history against every team.</div></div></div>
    <input type="search" id="pq" placeholder="Search name, team or position…" value="${esc(q)}" autofocus>
    <div class="card" style="margin-top:14px"><div class="tw"><table><thead><tr><th>Player</th><th>Pos</th><th>Team</th></tr></thead><tbody id="plist"></tbody></table></div></div>`;
  const draw = () => {
    const s = document.getElementById("pq").value.trim().toLowerCase();
    const rows = idx.filter(p => !s || `${p.name} ${p.team} ${p.pos}`.toLowerCase().includes(s)).slice(0, 200);
    document.getElementById("plist").innerHTML = rows.map(p => `<tr><td class="pl"><a href="#/player/${p.pid}">${esc(p.name)}</a></td><td>${esc(p.pos)}</td><td>${esc(p.team)}</td></tr>`).join("") || `<tr><td class="empty" colspan="3">No matches.</td></tr>`;
  };
  document.getElementById("pq").oninput = draw;
  draw();
}

function viewAbout() {
  setNav("about");
  const m = cache["meta.json"] || {};
  $app.innerHTML = `<div class="ph"><div><h1>How it works</h1></div></div><div class="prose">
  <h2>Schedule</h2><p>The site rebuilds itself every day at <b>11:30 PM</b> and <b>4:30 AM</b> Central. The night run grades games that finished that day; the morning run catches late finishes (Monday night), pulls overnight injury news and refreshes the projections. Each prediction is locked at kickoff, so it can't be changed after the fact.</p>
  <h2>Data sources</h2><ul>${(m.sources || []).map(s => `<li>${esc(s)}</li>`).join("")}</ul>
  <h2>Game scores</h2><p>Each final score blends the Vegas-implied score (70%) with an opponent-adjusted team rating model (30%) built from every game this season and last. If a team's starting QB is ruled out, the model takes 3 points off that team. Win probability uses a normal curve around the projected margin (SD 13.5 pts).</p>
  <h2>Player stats</h2><p>Projected team pass and rush attempts (adjusted for game script, so favorites run more) × each player's recent share of targets and carries × his efficiency, regressed toward league averages. The opposing defense's stats allowed to that position adjust the efficiency. When a player is ruled out, his targets and carries go to healthy teammates. Questionable players are cut 15%, doubtful 70%. A player's history against this opponent gets up to 20% weight. Touchdowns are scaled so every player's TDs add up to the team's projected points.</p>
  <h2>Grades</h2><p><b>Games</b> start at 100, lose 1.4 points per point the margin was off, 0.9 per point the total was off, and 12 for the wrong winner. <b>Players</b> lose points by how far each stat missed relative to its hit band. A+ ≥ 93, A ≥ 90, B ≥ 80, C ≥ 70, D ≥ 60.</p>
  <h2>Backtests</h2><p>Weeks played before the site launched were built afterward using only data from before each game, so you can see how the model would have done. They're tagged <span class="pill bt">Backtest</span> and kept separate from the official record.</p>
  </div>`;
}

// ------------------------------------------------------------------ router
async function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  window.scrollTo(0, 0);
  try {
    const meta = await get("meta.json");
    cache.meta = meta;
    document.getElementById("updated").textContent = `Updated ${new Date(meta.updated).toLocaleString("en-US", {timeZone: TZ, dateStyle:"medium", timeStyle:"short"})} CT`;
    if (meta.offseason) { $app.innerHTML = `<div class="empty">Offseason — predictions return in September.</div>`; return; }
    switch (parts[0]) {
      case undefined: return viewWeek();
      case "week": return viewWeek(parts[1]);
      case "game": return viewGame(parts[1], parts[2], parts[3]);
      case "player": return viewPlayer(parts[1], parts[2] && decodeURIComponent(parts[2]));
      case "players": return viewPlayers();
      case "accuracy": return viewAccuracy();
      case "about": return viewAbout();
      default: return viewWeek();
    }
  } catch (e) {
    console.error(e);
    $app.innerHTML = `<div class="empty">Couldn't load data (${esc(e.message)}).</div>`;
  }
}
window.addEventListener("hashchange", route);
route();
})();
