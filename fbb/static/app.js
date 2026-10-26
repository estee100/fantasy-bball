/* Fantasy Hoops HQ dashboard. Vanilla JS; polls /api/state every 60s. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pct = (x, d = 0) => (x == null ? "–" : (x * 100).toFixed(d) + "%");
const fmt = (x, d = 1) => (x == null || Number.isNaN(x) ? "–" : Number(x).toFixed(d));
const signed = (x, d = 2) => (x == null ? "–" : (x >= 0 ? "+" : "−") + Math.abs(x).toFixed(d));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
let S = null;
let charts = {};

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function ago(iso) {
  if (!iso) return "never";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

const ICON = {
  down: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M12 5v14M5 12l7 7 7-7"/></svg>',
  up: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 3 2 21h20L12 3zM12 10v5M12 18h.01"/></svg>',
  check: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M5 12l5 5L20 7"/></svg>',
  x: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6"><path d="M6 6l12 12M18 6 6 18"/></svg>',
  dot: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="5" fill="currentColor"/></svg>',
};

/* ------------------------------------------------------------------ boot */
document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  $$("nav.tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#refresh-btn").addEventListener("click", refreshNow);
  $("#ready-btn").addEventListener("click", () => {
    const pop = $("#ready-pop");
    pop.classList.toggle("hidden");
    $("#ready-btn").setAttribute("aria-expanded", String(!pop.classList.contains("hidden")));
  });
  $("#report-btn").addEventListener("click", submitReport);
  $("#team-btn").addEventListener("click", importTeam);
  $("#fa-btn").addEventListener("click", () => importFa(null));
  $("#txn-btn").addEventListener("click", importTxns);
  $("#fa-add").addEventListener("click", () => importFa(false));
  initWatchPicker();
  initCheckPicker();
  initHistoryForm();
  $("#edit-team").addEventListener("change", () => loadEditor($("#edit-team").value));
  initTrade();
  initTips();
  load();
  setInterval(load, 60_000);
  let h = location.hash.slice(1);
  if (h) showTab(h);
});

function initTheme() {
  let t = null;
  try { t = localStorage.getItem("theme"); } catch (e) {}
  if (t) document.documentElement.dataset.theme = t;
  $("#theme-btn").addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("theme", next); } catch (e) {}
    if (S) renderAll();
    if (learnData) renderLearning(learnData);
  });
}

function showTab(tab) {
  $$("nav.tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === tab)));
  $$("section[data-panel]").forEach((s) => s.classList.toggle("hidden", s.dataset.panel !== tab));
  history.replaceState(null, "", "#" + tab);
  if (tab === "news") loadNews();
  if (tab === "learning") loadLearning();
  if (tab === "setup") loadSetup();
  if (tab === "watch") loadWatch();
  if (tab === "history") loadHistory();
}

async function load() {
  try {
    S = await api("/api/state");
    renderAll();
  } catch (e) {
    $("#updated").textContent = "server unreachable";
    console.error(e);
  }
}

async function refreshNow() {
  const b = $("#refresh-btn");
  b.disabled = true;
  b.querySelector("svg").classList.add("spin");
  const before = S?.last_refresh;
  await api("/api/refresh", { method: "POST" });
  for (let i = 0; i < 60; i++) {           // poll until the pipeline finishes (≈20-60s)
    await new Promise((r) => setTimeout(r, 3000));
    const st = await api("/api/state");
    if (st.last_refresh !== before) { S = st; renderAll(); break; }
  }
  b.disabled = false;
  b.querySelector("svg").classList.remove("spin");
}

function renderAll() {
  renderBanner();
  renderHeader();
  renderKPIs();
  renderCats();
  renderHist();
  renderMoves();
  renderAlerts();
  loadLineup();
  renderRosters();
  renderHeat();
}

/* ------------------------------------------------------------------ header */
function renderHeader() {
  const L = S.league;
  const [ws, we] = L.week_range || [];
  $("#league-meta").innerHTML = `
    <span><b>${esc(L.name || "League")}</b></span>
    <span>Week ${esc(L.week)} · ${esc(shortDate(ws))}–${esc(shortDate(we))}</span>
    <span><b>${esc(L.me || "You")}</b> vs <b>${esc(L.opponent || "—")}</b></span>`;
  $("#updated").textContent = "Updated " + ago(S.last_refresh);
  const R = S.readiness || [];
  const bad = R.filter((r) => !r.ok).length;
  $("#ready-btn").innerHTML = `<span class="status ${bad ? (bad > 2 ? "st-serious" : "st-warning") : "st-good"}"><span class="dot"></span>${bad ? `${bad} data issue${bad > 1 ? "s" : ""}` : "Data fresh"}</span>`;
  const fails = Object.entries(S.pipeline || {}).filter(([, v]) => !v.ok);
  $("#ready-pop").innerHTML = `<div style="font-weight:600;margin-bottom:6px">Data readiness</div>` +
    R.map((r) => `<div class="row"><span style="color:${r.ok ? "var(--good)" : "var(--serious)"}">${r.ok ? ICON.check : ICON.warn}</span><span>${esc(r.name)}</span><span class="muted">${esc(r.detail)}</span></div>`).join("") +
    (fails.length ? `<div style="font-weight:600;margin:10px 0 4px">Failed pipeline steps</div>` +
      fails.map(([k, v]) => `<div class="row"><span style="color:var(--critical)">${ICON.x}</span><span>${esc(k)}</span><span class="muted">${esc(v.error)}</span></div>`).join("") : "") +
    `<div class="small muted" style="margin-top:8px">Numbers are only as good as these inputs. When a check fails, treat confidence levels as optimistic.</div>`;
}

function shortDate(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T12:00:00");
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/* ------------------------------------------------------------------ KPIs */
function renderKPIs() {
  const m = S.matchup, rec = S.recommendations, L = S.league;
  const tiles = [];
  if (m) {
    const days = (m.dates || []).length;
    tiles.push(kpi("Win probability this week", pct(m.win_prob), `vs ${esc(L.opponent || "opponent")} · ${days} day${days === 1 ? "" : "s"} left`, m.win_prob));
    if (L.scoring_mode === "points" && m.points) {
      const [me, op] = m.points.projected;
      tiles.push(kpi("Projected final score", `${Math.round(me)} <small>– ${Math.round(op)}</small>`,
        `so far ${Math.round(m.points.so_far[0])}–${Math.round(m.points.so_far[1])} · likely range ${Math.round(m.points.range_me[0])}–${Math.round(m.points.range_me[1])}`, null));
    }
    if (L.scoring_mode === "categories") {
      const cp = m.cat_probs || {};
      const exp = Object.values(cp).reduce((a, b) => a + b, 0);
      const lab = (k) => L.categories.find((c) => c.key === k)?.label || k;
      const toss = Object.entries(cp).filter(([, p]) => p > 0.35 && p < 0.65).map(([k]) => lab(k));
      tiles.push(kpi("Expected categories won", `${fmt(exp, 1)} <small>of ${L.categories.length}</small>`,
        toss.length ? `Toss-ups: ${esc(toss.join(", "))}` : "No toss-up categories", exp / L.categories.length));
    }
  } else tiles.push(kpi("Win probability this week", "–", S.manual ? "pick this week's opponent in Setup" : "run a refresh", null));
  if (rec?.base) {
    const table = S.league_table || [];
    const rank = table.findIndex((t) => t.is_mine) + 1;
    tiles.push(kpi("Typical-week strength", pct(rec.base.season_strength),
      `P(win) vs a league-average team${rank && table.length > 1 ? ` · #${rank} of ${table.length}` : ""}`, rec.base.season_strength));
  }
  const best = rec?.moves?.[0];
  tiles.push(kpi("Best move available", best ? `${signed(best.score, 2)} <small>wins</small>` : "None",
    best ? `Add ${esc(best.add_name)}, drop ${esc(best.drop_name)}` : "your roster is already the best use of free agents", null));
  $("#kpis").innerHTML = tiles.join("");
}

function kpi(label, value, foot, meter) {
  return `<div class="card kpi"><div class="label">${esc(label)}</div><div class="value num">${value}</div>
    ${meter != null ? `<div class="meter"><span style="width:${Math.max(2, Math.min(100, meter * 100))}%"></span></div>` : ""}
    <div class="foot">${foot}</div></div>`;
}

/* ------------------------------------------------------------------ category odds */
function catLabel(k) { return S.league.categories.find((c) => c.key === k)?.label || k; }
function fmtCat(k, v) {
  if (v == null) return "–";
  return k.endsWith("_pct") ? v.toFixed(3).replace(/^0/, "") : Math.round(v);
}

function renderCats() {
  const m = S.matchup;
  const el = $("#cat-rows");
  const points = S.league.scoring_mode === "points";
  $("#cat-title").textContent = points ? "Where the points come from" : "Category odds this week";
  $("#cat-legend").innerHTML = points
    ? `<span><i style="background:var(--me)"></i>You</span><span><i style="background:var(--opp)"></i>Opponent</span>`
    : `<span><i style="background:var(--div-pos)"></i>Favours you</span><span><i style="background:var(--div-neg)"></i>Favours opponent</span>`;
  if (!m) {
    $("#cat-sub").textContent = "";
    el.innerHTML = S.manual
      ? `<div class="empty">Pick this week's opponent in <a href="#setup" onclick="showTab('setup');return false">Setup</a> to see matchup odds.</div>`
      : `<div class="empty">No matchup yet.</div>`;
    return;
  }
  if (points && m.points) { renderPointsBreakdown(m.points, el); return; }
  if (S.league.scoring_mode !== "categories") {
    el.innerHTML = `<div class="empty">${S.league.scoring_mode === "points" ? "Points league: see the win probability above." : "No matchup yet."}</div>`;
    return;
  }
  $("#cat-sub").textContent = "projected totals: opponent · you";
  el.innerHTML = S.league.categories.map(({ key, label, higher }) => {
    const p = m.cat_probs[key] ?? 0.5;
    const [mine, theirs] = m.expected?.[key] || [];
    const strength = Math.min(1, Math.abs(p - 0.5) * 2);
    const op = 0.35 + 0.65 * strength;
    const bar = p >= 0.5
      ? `<div class="bar" style="left:50%;width:${(p - 0.5) * 100}%;background:var(--div-pos);opacity:${op}"></div>`
      : `<div class="bar" style="left:${p * 100}%;width:${(0.5 - p) * 100}%;background:var(--div-neg);opacity:${op}"></div>`;
    const tip = `${label}: you win ${pct(p)} of simulations. Projected final: you ${fmtCat(key, mine)}, opponent ${fmtCat(key, theirs)}${higher ? "" : " (lower is better)"}.`;
    return `<div class="cat-row" data-tip="${esc(tip)}">
      <span class="name">${esc(label)}</span>
      <span class="val num">${fmtCat(key, theirs)}</span>
      <div class="cat-track"><div class="mid"></div>${bar}</div>
      <span class="val num mine">${fmtCat(key, mine)}</span>
      <span class="pct num">${pct(p)}</span></div>`;
  }).join("");
}

function renderHist() {
  const m = S.matchup;
  const el = $("#hist");
  if (S.league.scoring_mode === "points") { renderMarginHist(m, el); return; }
  $("#hist-title").textContent = "How many categories you win";
  if (!m?.cats_won_hist) { el.innerHTML = `<div class="empty" style="width:100%">No simulation yet.</div>`; return; }
  const h = m.cats_won_hist, tot = h.reduce((a, b) => a + b, 0) || 1, n = h.length - 1;
  const max = Math.max(...h);
  const mode = h.indexOf(max);
  el.setAttribute("aria-label", `Distribution of categories won out of ${n}`);
  el.innerHTML = h.map((c, k) => {
    const share = c / tot;
    const cls = k > n / 2 ? "win" : "";
    return `<div class="col ${cls}" data-tip="Win ${k} of ${n} categories: ${pct(share, 1)} of simulations">
      ${k === mode ? `<span class="num" style="color:var(--ink-2)">${pct(share)}</span>` : ""}
      <div class="b" style="height:${(c / max) * 80}%"></div><span>${k}</span></div>`;
  }).join("");
  const winShare = h.reduce((a, c, k) => a + (k > n / 2 ? c : k === n / 2 ? c / 2 : 0), 0) / tot;
  $("#hist-note").innerHTML = `<span class="legend"><span><i style="background:var(--me)"></i>Weeks you win (${pct(winShare)})</span><span><i style="background:var(--axis)"></i>Weeks you lose</span></span>`;
}

/* ------------------------------------------------------------------ moves */
function renderMoves() {
  const moves = S.recommendations?.moves || [];
  const el = $("#moves");
  if (!moves.length) { el.innerHTML = `<div class="empty">No free agent improves your expected wins right now.</div>`; return; }
  const nToday = moves.filter((m) => m.urgency === "Do today").length;
  const nWorth = moves.filter((m) => m.urgency === "Worth doing").length;
  const summary = nToday
    ? `<div class="note" style="border-left:3px solid var(--serious)"><b>${nToday} move${nToday > 1 ? "s" : ""} to make today.</b> The rest can wait.</div>`
    : nWorth
      ? `<div class="note"><b>Nothing urgent.</b> ${nWorth} worthwhile upgrade${nWorth > 1 ? "s" : ""}, no rush on any of them.</div>`
      : `<div class="note"><b>Nothing worth doing today.</b> Every option below is a small or uncertain gain; holding is fine.</div>`;
  const B = S.recommendations?.add_budget;
  const budget = B && B.limit ? `<div class="small ${B.left === 0 ? "delta-down" : "muted"}" style="margin:6px 0">Adds this week: <b>${B.used} of ${B.limit}</b> used${B.left === 0 ? ": none left, so nothing below can be done until next week" : B.left === 1 ? ": one left, save it for the best move" : ""}.</div>` : "";
  const OS = S.recommendations?.open_spot;
  const open = OS && OS.adds?.length ? `<div class="card" style="margin:6px 0 10px;padding:10px 12px"><b>You have ${OS.spots} open roster spot${OS.spots > 1 ? "s" : ""}</b> <span class="small muted">(a player on IL doesn't count toward your limit)</span>: add without dropping anyone.
    <ul class="small" style="margin:6px 0 0">${OS.adds.map((a) => `<li><a href="#" data-pid="${a.add}">${esc(a.add_name)}</a> (${esc(a.add_team || "")}) ${(a.score >= 0 ? "+" : "") + fmt(a.score, 2)} expected wins${a.availability?.status === "W" ? ` · on waivers until ${esc(shortDate(a.availability.until))}` : ""}</li>`).join("")}</ul></div>` : "";
  el.innerHTML = summary + budget + open + moves.map((m, i) => `
    <div class="move">
      <div class="swap">
        ${m.urgency ? `<span class="status ${m.urgency === "Do today" ? "st-serious" : m.urgency === "Worth doing" ? "st-good" : ""}" data-tip="${esc(m.urgency_reason || "")}"><span class="dot" ${m.urgency === "Marginal" ? 'style="background:var(--axis)"' : ""}></span>${esc(m.urgency)}</span>` : ""}
        ${m.availability?.status === "W" ? `<span class="chip" style="color:var(--serious)" title="On waivers: needs a claim">Waivers → ${esc(shortDate(m.availability.until))}</span>` : ""}
        <span class="chip k-${m.kind}" data-tip="${m.kind === "stream" ? "Stream: helps this week's matchup; judged on the rest of this week" : "Upgrade: helps this week and beyond; judged over the next 21 days"}">${m.kind === "stream" ? "Stream" : "Upgrade"}</span>
        <span class="add"><span class="sign">+</span> <a href="#" data-pid="${m.add}">${esc(m.add_name)}</a> <span class="pteam">${esc(m.add_team || "")} · ${m.add_games} g</span></span>
        <span class="drop"><span class="sign">−</span> <a href="#" data-pid="${m.drop}">${esc(m.drop_name)}</a> <span class="pteam">${esc(m.drop_team || "")} · ${m.drop_games} g</span></span>
      </div>
      <div class="nums num">
        <div data-tip="Change in this week's win probability (same simulated games for everyone else). Within about ±1.5pp is simulation noise, shown as ≈0."><b>${Math.abs(m.week_dwin * 100) < 1.5 ? "≈0" : signed(m.week_dwin * 100, 1)}</b><span>pp this week</span></div>
        <div data-tip="Extra matchup wins over the rest of the season (future weeks discounted)"><b>${signed(m.season_dwins, 2)}</b><span>wins ROS</span></div>
      </div>
      <ul>${m.urgency_reason ? `<li><b>${esc(m.urgency)}:</b> ${esc(m.urgency_reason)}</li>` : ""}${(m.rationale || []).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <div class="actions">
        <span class="conf" data-tip="Chance the added player actually outproduces the dropped one over the move's horizon${S.calibration?.n ? ", calibrated on " + S.calibration.n + " scored moves" : " (uncalibrated until moves are scored)"}">
          Confidence <span class="meter"><span style="width:${m.confidence * 100}%"></span></span><b class="num">${pct(m.confidence)}</b></span>
        <span style="margin-left:auto"></span>
        <button class="btn" data-act="executed" data-i="${i}">${ICON.check.replace("<svg", '<svg style="width:14px;height:14px"')} ${m.availability?.status === "W" ? "I put in this claim" : "I made this move"}</button>
        <button class="btn ghost" data-act="dismissed" data-i="${i}">Dismiss</button>
        <button class="btn ghost" data-watch="${m.add}" data-vs="${m.drop}" title="Track how he does vs ${esc(m.drop_name)} without making the move">☆ Watch</button>
      </div>
    </div>`).join("");
  $$("#moves [data-act]").forEach((b) => b.addEventListener("click", async () => {
    const m = moves[+b.dataset.i];
    const found = await api(`/api/moves/lookup?add=${m.add}&drop=${m.drop}`);
    if (!found.id) { b.textContent = "Not in ledger yet (only top 3 are logged)"; return; }
    const res = await api(`/api/moves/${found.id}`, { method: "POST", body: JSON.stringify({ status: b.dataset.act }) }).catch((e) => ({ error: e.message }));
    b.closest(".actions").innerHTML = `<span class="small muted">${res.error ? "Couldn't apply: " + esc(res.error)
      : res.claimed ? `Claim noted. Your roster isn't changed yet: once it clears${res.until ? " (" + esc(shortDate(res.until)) + ")" : ""}, paste the transaction log and it's applied and logged.`
      : b.dataset.act === "executed" ? (res.roster_updated ? "Roster updated and logged; it'll be scored against real box scores. Re-running the model…" : "Logged as made: it'll be scored against real box scores.")
      : "Dismissed: still scored as a counterfactual, so the model learns either way."}</span>`;
    if (res.roster_updated) afterManualChange();
  }));
  $$("#moves [data-watch]").forEach((b) => b.addEventListener("click", async () => {
    await watchPlayer(+b.dataset.watch, +b.dataset.vs);
    b.textContent = "★ Watching";
    b.disabled = true;
  }));
  bindPlayerLinks(el);
}

/* ------------------------------------------------------------------ alerts */
let alertFilter = "all";
const ALERT_KIND = {
  skill_not_needed: ["Skill not needed", "st-critical", ICON.down],
  minutes_cut: ["Minutes cut", "st-serious", ICON.down],
  role_expansion: ["Role growing", "st-good", ICON.up],
};

function renderAlerts() {
  const all = S.alerts || [];
  const counts = { all: all.length, mine: all.filter((a) => a.owner === "mine").length, fa: all.filter((a) => a.owner === "free agent").length };
  $("#alert-filters").innerHTML = [["all", "All"], ["mine", "My players"], ["fa", "Free agents"]].map(([k, l]) =>
    `<button aria-pressed="${alertFilter === k}" data-f="${k}">${l} · ${counts[k]}</button>`).join("");
  $$("#alert-filters button").forEach((b) => b.addEventListener("click", () => { alertFilter = b.dataset.f; renderAlerts(); }));
  const list = all.filter((a) => alertFilter === "all" || (alertFilter === "mine" ? a.owner === "mine" : a.owner === "free agent"));
  const el = $("#alerts");
  if (!list.length) {
    el.innerHTML = `<div class="empty">No alerts. A player needs at least 3 games this season before his role can be compared to his baseline.</div>`;
    return;
  }
  el.innerHTML = list.slice(0, 14).map((a) => {
    const [label, st, icon] = ALERT_KIND[a.kind] || [a.kind, "", ICON.dot];
    const d = a.detail || {};
    const why = [...(d.explanation || [])];
    if (d.team_changed && !why.some((w) => w.includes("new team"))) why.push(`new team (${d.from_team} → ${d.team})`);
    return `<div class="alert ${a.kind}" data-pid="${a.nba_id}">
      <div class="row1"><span class="status ${st}"><span class="dot"></span>${label}</span>
        <span class="pname">${esc(a.name)}</span><span class="pteam">${esc(a.team || "")}</span>
        <span class="chip ${a.owner === "mine" ? "mine" : ""}">${a.owner === "mine" ? "Your player" : esc(a.owner)}</span></div>
      <div class="t">${esc(a.title)}</div>
      ${why.length ? `<div class="why">Possible cause: ${esc(why.join("; "))}</div>` : ""}
    </div>`;
  }).join("");
  $$("#alerts .alert").forEach((x) => x.addEventListener("click", () => openPlayer(+x.dataset.pid)));
}

function renderStartSit() {
  const ss = S.start_sit || [];
  $("#startsit-card").classList.toggle("hidden", !ss.length);
  $("#startsit").innerHTML = ss.map((p) => `<span class="chip" style="margin-right:6px">${esc(p.name)}</span>`).join("") +
    `<div class="small muted" style="margin-top:8px">Positions aren't checked: confirm eligibility in Yahoo.</div>`;
}

/* ------------------------------------------------------------------ rosters */
function injuryStatus(inj) {
  if (!inj) return `<span class="status st-good"><span class="dot"></span>Active</span>`;
  const s = (inj.status || "").toLowerCase();
  const cls = s === "out" || s === "suspension" ? "st-critical" : s === "doubtful" ? "st-serious" : "st-warning";
  return `<span class="status ${cls}" data-tip="${esc(inj.comment || "")}"><span class="dot"></span>${esc(inj.status)}</span>`;
}

function rosterTable(rows, el, withSlot) {
  const cats = [["min", "MIN"], ["pts", "PTS"], ["reb", "REB"], ["ast", "AST"], ["stl", "STL"], ["blk", "BLK"], ["fg3m", "3PM"], ["tov", "TO"], ["fg_pct", "FG%"], ["ft_pct", "FT%"]];
  const maxVar = Math.max(1, ...rows.map((r) => Math.abs(r.var || 0)));
  const points = S.league.scoring_mode === "points";
  el.innerHTML = `<thead><tr><th>Player</th>${withSlot ? "<th>Slot</th>" : ""}<th>Status</th><th class="r" title="Games left this week (expected, after injury and rest risk)">Games</th>
    ${cats.map(([, l]) => `<th class="r">${l}</th>`).join("")}<th class="r" title="${points ? "Projected fantasy points per game" : "Value above replacement level"}">${points ? "FP/G" : "Value"}</th><th class="r" title="Last 5 games vs projection">Form</th></tr></thead>
    <tbody>${rows.map((r) => `<tr class="click" data-pid="${r.nba_id}">
      <td><span class="pname">${esc(r.name)}</span>${r.take ? '<span class="take-badge" title="Using your take, not the model\'s projection">YOUR TAKE</span>' : ""}<span class="pteam">${esc(r.team || "")}${r.positions ? " · " + esc(r.positions) : ""}</span></td>
      ${withSlot ? `<td><span class="slot">${esc(r.slot || "")}</span></td>` : ""}
      <td>${injuryStatus(r.injury)}</td>
      <td class="r num">${r.games_left}${r.expected_games < r.games_left - 0.05 ? ` <span class="muted small">(${fmt(r.expected_games, 1)})</span>` : ""}</td>
      ${cats.map(([k]) => {
        const v = r.proj?.[k];
        const z = r.z?.[k];
        const txt = k.endsWith("_pct") ? (v == null ? "–" : v.toFixed(3).replace(/^0/, "")) : fmt(v, 1);
        return `<td class="r num" ${z != null ? `data-tip="z-score vs the draftable pool: ${signed(z, 1)}"` : ""}>${txt}</td>`;
      }).join("")}
      <td class="r num"><span style="display:inline-flex;align-items:center;gap:6px">
        <span style="width:44px;height:6px;background:var(--surface-2);border-radius:3px;display:inline-block;position:relative">
          <span style="position:absolute;left:0;top:0;bottom:0;border-radius:3px;width:${Math.max(0, (r.var || 0) / maxVar) * 100}%;background:var(--me)"></span></span>${points ? fmt(r.fp, 1) : fmt(r.var, 1)}</span></td>
      <td class="r num">${r.trend == null ? "–" : `<span class="${r.trend >= 0 ? "delta-up" : "delta-down"}">${r.trend >= 0 ? "▲" : "▼"} ${fmt(Math.abs(r.trend), 1)}</span>`}</td>
    </tr>`).join("")}</tbody>`;
  $$("tr.click", el).forEach((tr) => tr.addEventListener("click", () => openPlayer(+tr.dataset.pid)));
}

function renderRosters() {
  $("#my-roster-title").textContent = S.league.me || "My roster";
  $("#opp-roster-title").textContent = "Opponent: " + (S.league.opponent || "—");
  rosterTable(S.roster || [], $("#my-roster"), true);
  rosterTable(S.opp_roster || [], $("#opp-roster"), false);
}

/* ------------------------------------------------------------------ league heatmap */
function renderHeat() {
  const T = S.league_table || [];
  const el = $("#heat");
  if (!T.length) { el.innerHTML = `<tr><td class="empty">No league data.</td></tr>`; return; }
  const cats = Object.keys(T[0].ranks || {});
  const N = T.length;
  const steps = ["--seq-6", "--seq-5", "--seq-4", "--seq-3", "--seq-2", "--seq-1"];
  const cell = (rank) => {
    const i = Math.min(steps.length - 1, Math.floor(((rank - 1) / Math.max(1, N - 1)) * (steps.length - 1) + 0.0001));
    const darkText = document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
    const light = i >= 3;
    const color = darkText ? (light ? "var(--ink-2)" : "#fff") : (light ? "var(--ink)" : "#fff");
    return `background:var(${steps[i]});color:${color}`;
  };
  const lab = (t, k) => t.labels?.[k] || (k === "_points" ? "PTS" : catLabel(k));
  const points = S.league.scoring_mode === "points";
  $("#heat-title").textContent = points ? "Where each team's points come from" : "League category map";
  $("#heat-sub").textContent = points ? "rank by fantasy points from each stat in a typical week · 1 = most, shown in the strongest colour" : "rank in a typical week · 1 = best, shown in the strongest colour";
  el.innerHTML = `<thead><tr><th>Team</th><th class="r">Record</th><th class="r" title="Expected share of weeks won vs the rest of this league">Strength</th>
    ${points ? '<th class="r" title="Projected fantasy points in a typical week">FP/wk</th>' : ""}
    ${cats.map((k) => `<th class="r">${esc(lab(T[0], k))}</th>`).join("")}</tr></thead>
    <tbody>${T.map((t) => `<tr class="${t.is_mine ? "mine" : ""} ${t.is_opp ? "opp" : ""}">
      <td><span class="pname">${esc(t.name)}</span>${t.is_mine ? ' <span class="chip mine">You</span>' : ""}${t.is_opp ? ' <span class="chip">This week</span>' : ""}</td>
      <td class="r num muted">${t.record?.every((x) => x != null) ? t.record.join("-") : "–"}</td>
      <td class="r num"><b>${pct(t.strength)}</b></td>
      ${points ? `<td class="r num">${fmt(t.total_fp, 0)}</td>` : ""}
      ${cats.map((k) => `<td class="h num" style="${cell(t.ranks[k])}" data-tip="${esc(lab(t, k))}: #${t.ranks[k]} of ${N} (${points ? fmt(t.cats[k], 0) + " fantasy pts" : fmtCat(k, t.cats[k])} in a typical week)">${t.ranks[k]}</td>`).join("")}
    </tr>`).join("")}</tbody>`;
}

/* ------------------------------------------------------------------ news */
let newsFilter = "all", newsCache = [];
async function loadNews() {
  newsCache = await api("/api/news?limit=120");
  renderNews();
}
const EV_LABEL = { injury: "Injury", return: "Return", rest: "Rest", minutes_up: "Minutes up", minutes_down: "Minutes down", role_up: "Bigger role", role_down: "Smaller role", lineup_start: "Starting", lineup_bench: "To bench", trade: "Trade", suspension: "Suspension", other: "Note" };

function renderNews() {
  const f = { all: () => true, mine: (n) => n.players.some((p) => p.mine), rostered: (n) => n.players.some((p) => p.rostered), events: (n) => n.events.length };
  const counts = Object.fromEntries(Object.entries(f).map(([k, fn]) => [k, newsCache.filter(fn).length]));
  $("#news-filters").innerHTML = [["all", "All"], ["mine", "My players"], ["rostered", "Rostered"], ["events", "Fantasy events"]].map(([k, l]) =>
    `<button aria-pressed="${newsFilter === k}" data-f="${k}">${l} · ${counts[k]}</button>`).join("");
  $$("#news-filters button").forEach((b) => b.addEventListener("click", () => { newsFilter = b.dataset.f; renderNews(); }));
  $("#news-count").textContent = counts.mine || "";
  const method = newsCache.find((n) => n.method)?.method;
  $("#news-sub").textContent = method === "keywords" ? "events tagged by keyword rules (add ANTHROPIC_API_KEY for Claude extraction)" : "events extracted by Claude";
  const list = newsCache.filter(f[newsFilter]);
  $("#news").innerHTML = list.length ? list.map((n) => `
    <div class="news-item">
      <div class="meta"><span>${esc(n.source)}</span><span>${esc(ago(n.published))}</span>
        ${n.players.map((p) => `<a href="#" class="chip ${p.mine ? "mine" : ""}" data-pid="${p.nba_id}">${esc(p.name)}</a>`).join("")}</div>
      <a class="title" href="${esc(n.url)}" target="_blank" rel="noopener noreferrer">${esc(n.title)}</a>
      ${n.summary ? `<div class="sum">${esc(n.summary)}</div>` : ""}
      ${n.events.length ? `<div class="tags">${n.events.map((e) => {
        const st = e.direction > 0 ? "st-good" : e.direction < 0 ? "st-critical" : "st-warning";
        return `<span class="status ${st}" data-tip="${esc(e.takeaway)}${e.games_out ? ` · ~${e.games_out} games out` : ""}${e.minutes_multiplier ? ` · minutes ×${e.minutes_multiplier}` : ""}"><span class="dot"></span>${esc(EV_LABEL[e.event_type] || e.event_type)}: ${esc(e.player)}</span>`;
      }).join("")}</div>` : ""}
    </div>`).join("") : `<div class="empty">Nothing here yet.</div>`;
  bindPlayerLinks($("#news"));
}

async function submitReport() {
  const text = $("#report-text").value.trim();
  if (text.length < 50) { $("#report-status").textContent = "Paste a longer report."; return; }
  $("#report-status").textContent = "Extracting…";
  try {
    const r = await api("/api/reports", { method: "POST", body: JSON.stringify({ text, source: $("#report-source").value || "pasted" }) });
    $("#report-status").textContent = r.status === "ok" ? `Saved profiles for: ${r.players.join(", ") || "(no known players found)"}` : (r.detail || r.status);
  } catch (e) { $("#report-status").textContent = "Failed: " + e.message; }
}

/* ------------------------------------------------------------------ player drawer */
function bindPlayerLinks(root) {
  $$("[data-pid]", root).forEach((a) => {
    if (a.classList.contains("alert") || a.tagName === "TR") return;
    a.addEventListener("click", (e) => { e.preventDefault(); openPlayer(+a.dataset.pid); });
  });
}

async function openPlayer(pid) {
  const root = $("#drawer-root");
  root.innerHTML = `<div class="drawer-bg"></div><aside class="drawer" role="dialog" aria-label="Player detail"><div class="empty">Loading…</div></aside>`;
  const close = () => { root.innerHTML = ""; Object.keys(charts).filter((k) => k.startsWith("p_")).forEach((k) => { charts[k].destroy(); delete charts[k]; }); };
  $(".drawer-bg", root).addEventListener("click", close);
  document.addEventListener("keydown", function onEsc(e) { if (e.key === "Escape") { close(); document.removeEventListener("keydown", onEsc); } });
  const p = await api(`/api/player/${pid}`);
  const cats = S.league.categories;
  const z = p.z || {};
  const zmax = Math.max(2, ...Object.values(z).map(Math.abs));
  $(".drawer", root).innerHTML = `
    <button class="btn ghost close" aria-label="Close">${ICON.x.replace("<svg", '<svg style="width:16px;height:16px"')}</button>
    <h3>${esc(p.name)} <button class="btn ghost small" id="drawer-watch" style="margin-left:8px;vertical-align:middle">☆ Watch</button>
      ${(S.roster || []).some((r) => r.nba_id === pid) ? "" : '<button class="btn ghost small" id="drawer-check" style="vertical-align:middle">Check as pickup</button>'}</h3>
    <div class="ink2" style="margin-top:4px;display:flex;gap:10px;flex-wrap:wrap;align-items:center">${esc(p.team || "")}${p.positions ? " · " + esc(p.positions) : ""} ${injuryStatus(p.injury)}
      <span class="muted small">${p.n_games} games in sample · ${p.games_left} left this week</span></div>
    ${p.injury?.comment ? `<div class="note" style="margin-top:10px">${esc(p.injury.comment)}</div>` : ""}
    ${(p.alerts || []).map((a) => `<div class="alert ${a.kind}" style="margin-top:10px;cursor:default"><div class="row1"><span class="status ${(ALERT_KIND[a.kind] || [])[1]}"><span class="dot"></span>${(ALERT_KIND[a.kind] || [a.kind])[0]}</span></div><div class="t">${esc(a.title)}</div></div>`).join("")}
    ${takeBox(p)}
    <h4>Projection per game</h4>
    <div class="num ink2">${p.proj ? `${fmt(p.proj.min, 1)} min · ${fmt(p.proj.pts, 1)} pts · ${fmt(p.proj.reb, 1)} reb · ${fmt(p.proj.ast, 1)} ast · ${fmt(p.proj.stl, 1)} stl · ${fmt(p.proj.blk, 1)} blk · ${fmt(p.proj.fg3m, 1)} 3pm · ${fmt(p.proj.tov, 1)} to` : "–"}</div>
    ${Object.keys(z).length ? `<h4>Category impact <span class="muted small" style="font-weight:400">z-score vs the draftable pool</span></h4>
    <div class="zbars">${cats.filter((c) => z[c.key] != null).map((c) => {
      const v = z[c.key], w = (Math.abs(v) / zmax) * 50;
      return `<div class="zrow"><span>${esc(c.label)}</span><div class="ztrack"><div class="mid"></div><div class="bar" style="${v >= 0 ? `left:50%;width:${w}%;background:var(--div-pos)` : `left:${50 - w}%;width:${w}%;background:var(--div-neg)`}"></div></div><span class="num r" style="text-align:right">${signed(v, 1)}</span></div>`;
    }).join("")}</div>` : ""}
    <h4>Skill usage <span class="muted small" style="font-weight:400">per 36 minutes, last 30 games · dashed line = baseline</span></h4>
    ${(p.skills || []).filter((s) => s.dates).map((s, i) => `<div class="small ink2" style="margin:6px 0 2px">${esc(cap(s.skill_label))}</div><div class="chart-box sm"><canvas id="sk${i}"></canvas></div>`).join("") || `<div class="empty">Not enough games.</div>`}
    <h4>Minutes</h4><div class="chart-box sm"><canvas id="mins"></canvas></div>
    ${(p.scouting || []).length ? `<h4>Scouting</h4>` + p.scouting.map((s) => `<div class="note" style="margin-bottom:8px"><div><b>Role:</b> ${esc(s.role)}</div>
      <div><b>Strengths:</b> ${esc(s.strengths.join("; "))}</div><div><b>Weaknesses:</b> ${esc(s.weaknesses.join("; "))}</div>
      <div><b>Fantasy:</b> ${esc(s.fantasy_notes)}</div><div class="muted" style="margin-top:4px">${esc(s.source)} · ${esc(ago(s.created))}</div></div>`).join("") : ""}
    ${(p.events || []).length ? `<h4>News events</h4>` + p.events.map((e) => `<div class="small" style="padding:4px 0"><span class="muted">${esc(e.date)}</span> · <b>${esc(EV_LABEL[e.event_type] || e.event_type)}</b> · ${esc(e.takeaway)}</div>`).join("") : ""}
    <h4>Recent games</h4>
    <div class="tbl-wrap"><table><thead><tr><th>Date</th><th>Opp</th><th class="r">MIN</th><th class="r">PTS</th><th class="r">REB</th><th class="r">AST</th><th class="r">STL</th><th class="r">BLK</th><th class="r">3PM</th><th class="r">TO</th><th class="r">FG</th></tr></thead>
      <tbody>${(p.game_log || []).map((g) => `<tr><td class="num">${esc(g.date)}</td><td class="muted">${esc(g.matchup.slice(-6))}</td>${["min", "pts", "reb", "ast", "stl", "blk", "fg3m", "tov"].map((k) => `<td class="r num">${Math.round(g[k])}</td>`).join("")}<td class="r num">${esc(g.fg)}</td></tr>`).join("")}</tbody></table></div>`;
  $(".drawer .close", root).addEventListener("click", close);
  bindTakeBox(p, pid);
  const chk = $("#drawer-check");
  if (chk) chk.addEventListener("click", () => { close(); showTab("check"); checkPlayer(pid); });
  $("#drawer-watch").addEventListener("click", async (e) => {
    const r = await watchPlayer(pid);
    e.target.textContent = r.error ? r.error : "★ Watching";
    e.target.disabled = true;
  });
  (p.skills || []).filter((s) => s.dates).forEach((s, i) => { charts["p_sk" + i] = skillChart($("#sk" + i), s); });
  const s0 = (p.skills || []).find((s) => s.dates);
  if (s0) charts.p_mins = minutesChart($("#mins"), s0);
}
const cap = (s) => (s || "").charAt(0).toUpperCase() + (s || "").slice(1);

function chartDefaults() {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false }, tooltip: { backgroundColor: css("--ink"), titleColor: css("--surface"), bodyColor: css("--surface"), displayColors: false } },
    scales: {
      x: { grid: { display: false }, ticks: { color: css("--muted"), maxTicksLimit: 6, font: { size: 11 } }, border: { color: css("--axis") } },
      y: { grid: { color: css("--grid") }, ticks: { color: css("--muted"), font: { size: 11 }, maxTicksLimit: 5 }, border: { display: false }, beginAtZero: true },
    },
  };
}

function skillChart(canvas, s) {
  // With a baseline/recent split: grey = baseline games, blue = recent window.
  // Without one (too few games this season): plain blue line vs the average shown.
  const recentStart = s.recent_start ? s.dates.indexOf(s.dates.find((d) => d >= s.recent_start)) : 0;
  const me = css("--me"), muted = css("--axis");
  const base = s.baseline?.mean ?? s.rate.reduce((a, b) => a + b, 0) / Math.max(1, s.rate.length);
  return new Chart(canvas, {
    type: "line",
    data: {
      labels: s.dates.map((d) => d.slice(5)),
      datasets: [
        { label: "per 36", data: s.rate, borderColor: me, borderWidth: 2, tension: 0.25,
          pointRadius: s.rate.map((_, i) => (recentStart >= 0 && i >= recentStart ? 3.5 : 2)),
          pointBackgroundColor: s.rate.map((_, i) => (recentStart >= 0 && i >= recentStart ? me : muted)), pointBorderWidth: 0,
          segment: { borderColor: (ctx) => (recentStart >= 0 && ctx.p1DataIndex >= recentStart ? me : muted) } },
        ...(base != null ? [{ label: s.baseline ? "baseline" : "average", data: s.dates.map(() => base), borderColor: css("--ink-2"), borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0 }] : []),
      ],
    },
    options: { ...chartDefaults(), plugins: { ...chartDefaults().plugins, tooltip: { ...chartDefaults().plugins.tooltip,
      callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y.toFixed(1)}${c.datasetIndex === 0 ? ` (${s.team[c.dataIndex]}, ${s.minutes[c.dataIndex]} min)` : ""}` } } } },
  });
}

function minutesChart(canvas, s) {
  return new Chart(canvas, {
    type: "bar",
    data: { labels: s.dates.map((d) => d.slice(5)), datasets: [{ label: "minutes", data: s.minutes, backgroundColor: css("--me"), borderRadius: 3, maxBarThickness: 14 }] },
    options: chartDefaults(),
  });
}

/* ------------------------------------------------------------------ trade lab */
const trade = { give: [], get: [] };
function initTrade() {
  ["give", "get"].forEach((side) => {
    const el = $("#pick-" + side);
    el.innerHTML = `<input placeholder="Search players…" aria-label="Search players to ${side}"><div class="opts card hidden"></div><div class="picked"></div>`;
    const input = $("input", el), opts = $(".opts", el);
    let t;
    input.addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        if (input.value.trim().length < 2) { opts.classList.add("hidden"); return; }
        const scope = side === "give" ? "mine" : "others";
        const hits = await api(`/api/players/search?q=${encodeURIComponent(input.value.trim())}&scope=${scope}`);
        opts.innerHTML = hits.map((h) => `<div class="opt" data-id="${h.nba_id}" data-name="${esc(h.name)}"><span><b>${esc(h.name)}</b> <span class="pteam">${esc(h.team || "")}</span></span><span class="muted small">${esc(h.owner)}</span></div>`).join("") || `<div class="opt muted">No match</div>`;
        opts.classList.remove("hidden");
        $$(".opt[data-id]", opts).forEach((o) => o.addEventListener("click", () => {
          if (!trade[side].some((x) => x.id === +o.dataset.id)) trade[side].push({ id: +o.dataset.id, name: o.dataset.name });
          input.value = ""; opts.classList.add("hidden"); renderPicked();
        }));
      }, 200);
    });
    input.addEventListener("blur", () => setTimeout(() => opts.classList.add("hidden"), 200));
  });
  $("#trade-btn").addEventListener("click", evalTrade);
  $("#tf-run").addEventListener("click", runTradeFinder);
}

function renderPicked() {
  ["give", "get"].forEach((side) => {
    $(`#pick-${side} .picked`).innerHTML = trade[side].map((p, i) => `<span class="chip" data-i="${i}" title="Remove">${esc(p.name)} ✕</span>`).join("");
    $$(`#pick-${side} .picked .chip`).forEach((c) => c.addEventListener("click", () => { trade[side].splice(+c.dataset.i, 1); renderPicked(); }));
  });
  $("#trade-btn").disabled = !(trade.give.length && trade.get.length);
}

async function evalTrade() {
  const el = $("#trade-result");
  el.classList.remove("hidden");
  const resp = await fetch("/api/trade", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ give: trade.give.map((p) => p.id), get: trade.get.map((p) => p.id) }) });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    el.innerHTML = `<div class="bd"><span class="status st-critical"><span class="dot"></span>${esc(err.detail || "Trade could not be evaluated")}</span></div>`;
    return;
  }
  const r = await resp.json();
  const shift = Object.entries(r.cat_shift || {});
  const smax = Math.max(1, ...shift.map(([, v]) => Math.abs(v)));
  const verdictSt = r.verdict === "accept" ? "st-good" : r.verdict === "decline" ? "st-critical" : "st-warning";
  el.innerHTML = `<div class="hd"><h2>Verdict</h2><span class="status ${verdictSt}"><span class="dot"></span>${esc(cap(r.verdict))}</span></div>
    <div class="bd"><div class="grid cols-4">
      <div class="kpi" style="padding:0"><div class="label">You</div><div class="value num">${signed(r.my_dwins, 2)} <small>wins ROS</small></div></div>
      <div class="kpi" style="padding:0"><div class="label">${esc(r.other_team_name || "Them")}</div><div class="value num">${r.their_dwins == null ? "–" : signed(r.their_dwins, 2)} <small>wins ROS</small></div>
        <div class="foot">${r.their_dwins != null && r.their_dwins < 0 ? "Bad for them: expect a no unless they value something else" : ""}</div></div></div>
    ${shift.length ? `<h4 style="margin:16px 0 8px;font-size:13px">Category shift (z-score per game)</h4><div class="zbars">${shift.map(([k, v]) => {
      const w = (Math.abs(v) / smax) * 50;
      return `<div class="zrow"><span>${esc(catLabel(k))}</span><div class="ztrack"><div class="mid"></div><div class="bar" style="${v >= 0 ? `left:50%;width:${w}%;background:var(--div-pos)` : `left:${50 - w}%;width:${w}%;background:var(--div-neg)`}"></div></div><span class="num" style="text-align:right">${signed(v, 1)}</span></div>`;
    }).join("")}</div>` : ""}
    ${r.auto_filled?.length ? `<div class="note" style="margin-top:12px">Open roster spots filled with the best free agents: ${esc(r.auto_filled.join(", "))}</div>` : ""}</div>`;
}

/* ------------------------------------------------------------------ learning */
let learnData = null;
async function loadLearning() {
  learnData = await api("/api/learning");
  renderLearning(learnData);
}

function renderLearning(d) {
  const moves = d.moves || [];
  const scored = moves.filter((m) => m.eval);
  const final = scored.filter((m) => m.eval.final);
  const hits = scored.filter((m) => m.eval.success).length;
  const mm = d.matchups || {};
  const bt = d.backtests?.[0];
  $("#learn-kpis").innerHTML = [
    kpi("Moves tracked", moves.length, "suggestions + your own roster changes", null),
    kpi("Moves scored", `${scored.length} <small>${final.length} final</small>`, "scored 7 days in (provisional) and at horizon end", null),
    kpi("Hit rate", scored.length ? pct(hits / scored.length) : "–", scored.length ? `avg stated confidence ${pct(scored.reduce((a, m) => a + (m.raw_confidence || 0), 0) / scored.length)}` : "no scored moves yet", null),
    kpi("Matchup forecast", mm.n ? fmt(mm.brier, 3) : (d.matchup_backtest ? fmt(d.matchup_backtest.calibrated.brier, 3) : "–"),
      mm.n ? `Brier score over ${mm.n} live week${mm.n > 1 ? "s" : ""} (coin flip = 0.250, lower is better)`
        : d.matchup_backtest ? `backtest: ${d.matchup_backtest.n_matchups} matchups last season, picked the winner ${pct(d.matchup_backtest.calibrated.accuracy)} (coin flip = 0.250)` : "first score after week 1 resolves", null),
  ].join("");
  const MB = d.matchup_backtest;
  $("#mb-note").innerHTML = MB ? `<b>Weekly win probability, backtested on ${MB.n_matchups} matchups from last season</b> (simulated 12-team leagues, predicted using only data available before each week, scored on real box scores).
    Brier ${fmt(MB.calibrated.brier, 3)} vs 0.250 for a coin flip; picks the winner ${pct(MB.calibrated.accuracy)} of the time.
    Learned from it: players with no injury listing still miss ${pct(MB.miss_rates.short, 0)} of games in short absences; weekly form spread ${MB.form_sd}; final calibration temperature ${fmt(MB.T_used, 2)}.
    <div class="legend" style="margin-top:6px">${(MB.bins_calibrated || []).map((b) => `<span>said ${pct(b.mean_p)} → won ${pct(b.win_rate)} <span class="muted">(n=${b.n})</span></span>`).join("")}</div>` : "";

  // calibration
  const bins = (d.calibration?.bins || []).filter((b) => b.n > 0);
  if (charts.calib) charts.calib.destroy();
  const ctxC = $("#calib-chart");
  charts.calib = new Chart(ctxC, {
    type: "scatter",
    data: { datasets: [
      { label: "perfect calibration", type: "line", data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: css("--axis"), borderDash: [4, 4], borderWidth: 1.5, pointRadius: 0 },
      { label: "actual", data: bins.map((b) => ({ x: (b.lo + Math.min(b.hi, 1)) / 2, y: b.hits / b.n, n: b.n })), backgroundColor: css("--me"), pointRadius: bins.map((b) => Math.min(14, 5 + Math.sqrt(b.n) * 2)), pointBorderColor: css("--surface"), pointBorderWidth: 2 },
    ] },
    options: { ...chartDefaults(), interaction: { mode: "nearest", intersect: true },
      plugins: { ...chartDefaults().plugins, tooltip: { ...chartDefaults().plugins.tooltip, callbacks: { label: (c) => c.datasetIndex ? `said ~${pct(c.raw.x)}, happened ${pct(c.raw.y)} (n=${c.raw.n})` : "" } } },
      scales: { x: { ...chartDefaults().scales.x, type: "linear", min: 0, max: 1, title: { display: true, text: "stated confidence", color: css("--muted") }, ticks: { ...chartDefaults().scales.x.ticks, callback: (v) => pct(v) } },
        y: { ...chartDefaults().scales.y, min: 0, max: 1, title: { display: true, text: "actually succeeded", color: css("--muted") }, ticks: { ...chartDefaults().scales.y.ticks, callback: (v) => pct(v) } } } },
  });
  $("#calib-note").textContent = bins.length
    ? "Dots above the dashed line: the model was too cautious. Below: overconfident. Confidence shown on new moves is already corrected by this record (shrunk toward the raw estimate while the sample is small)."
    : "No moves scored yet. A move is first scored 7 days after it's suggested (dismissed ones too, as counterfactuals), so this fills in over the first weeks.";

  // backtests
  const B = [...(d.backtests || [])].reverse();
  if (charts.bt) charts.bt.destroy();
  charts.bt = new Chart($("#bt-chart"), {
    type: "line",
    data: { labels: B.map((b) => new Date(b.created).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" })),
      datasets: [
        { label: "Model", data: B.map((b) => b.model_err), borderColor: css("--me"), backgroundColor: css("--me"), borderWidth: 2, pointRadius: 3 },
        { label: "Flat season average", data: B.map((b) => b.baseline_err), borderColor: css("--opp"), backgroundColor: css("--opp"), borderWidth: 2, pointRadius: 3 },
      ] },
    options: { ...chartDefaults(), plugins: { ...chartDefaults().plugins, legend: { display: true, position: "bottom", labels: { color: css("--ink-2"), boxWidth: 10, boxHeight: 10 } } },
      scales: { ...chartDefaults().scales, y: { ...chartDefaults().scales.y, beginAtZero: false } } },
  });
  $("#bt-note").textContent = bt ? `Latest: ${pct(1 - bt.model_err / bt.baseline_err, 1)} lower error than projecting from a flat season average, on ${bt.n.toLocaleString()} out-of-sample player-games. Re-tuned daily.` : "Not tuned yet.";

  // parameters
  const P = d.params || {};
  const rows = [
    ["half_life_minutes", "Minutes memory", (v) => `${v} games`, "How fast projected minutes follow recent games. Short = minutes changes (injuries, rotations, trades) get picked up fast."],
    ["half_life_games", "Production memory", (v) => `${v} games`, "Recency weighting for per-minute production within this season."],
    ["prior_strength_games", "Last season's production counts as", (v) => `${v} games`, "In October, per-minute production leans on last season; by mid-season, this season takes over."],
    ["prior_strength_minutes", "Last season's minutes count as", (v) => `${v} games`, "Minutes change across the offseason (trades, new roles), so last year's minutes are trusted far less."],
    ["future_week_discount", "Future-week discount", (v) => `${v}`, "How much an edge next week counts vs this week (projections drift; you'll make more moves). A judgment call, not learned."],
    ["miss_rate_short", "Unannounced absences", (v) => pct(v, 1), "Share of games a player with no injury listing still misses (rest, illness, knocks). Counted from box scores."],
    ["weekly_form_sd", "Weekly form spread", (v) => `${v}`, "How much a player's level drifts within a week. Tuned on the first half of last season, checked on the second."],
    ["win_prob_temperature", "Win-probability calibration", (v) => fmt(v, 2), "Final correction from the matchup backtest: 1.0 = none needed, above 1 = pull forecasts toward 50%."],
  ];
  const nm = Object.entries(P.news_multipliers || {});
  $("#params").innerHTML = `<thead><tr><th>Parameter</th><th class="r">Learned value</th><th>What it means</th></tr></thead><tbody>
    ${rows.map(([k, l, f, m]) => `<tr><td class="pname">${esc(l)}</td><td class="r num">${P[k] != null ? esc(f(P[k])) : "–"}</td><td class="ink2 small">${esc(m)}</td></tr>`).join("")}
    <tr><td class="pname">News impact</td><td class="r num">${nm.length ? nm.map(([t, v]) => `${esc(EV_LABEL[t] || t)} ×${fmt(v, 2)}`).join("<br>") : "–"}</td>
      <td class="ink2 small">Learned minutes multiplier per news event type (actual vs projected minutes in the next 5 games). Needs 3+ events of a type. ${(d.news_events_by_type || []).map((e) => `${esc(EV_LABEL[e.event_type] || e.event_type)}: ${e.n}`).join(", ")}</td></tr></tbody>`;

  // ledger
  $("#ledger").innerHTML = moves.length ? `<thead><tr><th>Date</th><th>Type</th><th>Add</th><th>Drop</th><th>Status</th><th class="r">Confidence</th><th class="r">Predicted</th><th>Outcome</th></tr></thead><tbody>
    ${moves.map((m) => {
      const e = m.eval;
      const out = !e ? `<span class="muted small">pending</span>` :
        `<span class="status ${e.success ? "st-good" : "st-critical"}"><span class="dot"></span>${e.success ? "Worked" : "Didn't"}</span> <span class="small muted num">${signed(e.realized_edge, 1)} value · ${e.add_games} vs ${e.drop_games} g${e.final ? "" : " · provisional"}</span>`;
      return `<tr><td class="num small">${esc(m.created.slice(5, 10))}</td><td><span class="chip k-${esc(m.kind || "upgrade")}">${esc(cap(m.kind || "upgrade"))}</span></td>
        <td class="pname">${esc(m.add_name)}</td><td class="ink2">${esc(m.drop_name)}</td>
        <td><span class="chip ${m.status === "executed" || m.status === "manual" ? "mine" : ""}">${esc(m.status === "manual" ? "your move" : m.status)}</span></td>
        <td class="r num">${pct(m.confidence)}</td><td class="r num small">${m.pred_season_dwins != null ? signed((m.pred_week_dwin || 0) + m.pred_season_dwins, 2) + " wins" : "–"}</td><td>${out}</td></tr>`;
    }).join("")}</tbody>` : `<tr><td class="empty">The top 3 suggestions from each refresh land here, along with roster changes you make in Yahoo.</td></tr>`;
}

/* ------------------------------------------------------------------ tooltips */
function initTips() {
  const tip = $("#tip");
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest("[data-tip]");
    if (!t || !t.dataset.tip) { tip.classList.add("hidden"); return; }
    tip.textContent = t.dataset.tip;
    tip.classList.remove("hidden");
  });
  document.addEventListener("mousemove", (e) => {
    if (tip.classList.contains("hidden")) return;
    const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
    const y = e.clientY + 16 + tip.offsetHeight > innerHeight ? e.clientY - tip.offsetHeight - 10 : e.clientY + 16;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  });
}

/* ------------------------------------------------------------------ manual league setup */
function renderBanner() {
  const b = $("#rosters-banner");
  const M = S.manual;
  if (!M) { b.classList.add("hidden"); return; }
  const others = M.teams.filter((t) => !t.is_mine).length;
  const needed = M.num_teams - 1;
  const mine = M.teams.some((t) => t.is_mine);
  let msg = "";
  if (!mine) msg = "Paste your own team in League setup to get started.";
  else if (others < needed) msg = `Only ${others} of ${needed} other teams pasted: recommended pickups may already be on someone's roster, and the league-average comparison is approximate. Paste the rest in League setup.`;
  else if (!M.opponent_key) msg = "Pick this week's opponent in League setup to see matchup odds.";
  b.classList.toggle("hidden", !msg);
  b.innerHTML = msg ? `${ICON.warn.replace("<svg", '<svg style="width:16px;height:16px;color:var(--warning)"')}<span>${esc(msg)}</span>
    <button class="btn" style="margin-left:auto" onclick="showTab('setup')">Setup</button>` : "";
}

let setupData = null;
async function loadSetup() {
  setupData = await api("/api/manual");
  renderSetup();
  api("/api/add-limit").then((b) => { if (b.limit) $("#cfg-adds").value = b.limit; });
  renderFaStatus(await api("/api/free-agents"));
  renderAlertSettings(await api("/api/alerts"));
}

function renderSetup() {
  const M = setupData;
  const pw = M.point_weights || {};
  const W = [["pts", "Points"], ["reb", "Total rebounds"], ["ast", "Assists"], ["stl", "Steals"], ["blk", "Blocked shots"], ["tov", "Turnovers"]];
  $("#settings-form").innerHTML = `
    <div class="setting-sep">Points per stat</div>
    ${W.map(([k, l]) => `<div class="setting-row"><span>${l}</span><input data-w="${k}" type="number" step="0.1" value="${pw[k] ?? ""}"></div>`).join("")}
    <div class="setting-sep">Roster</div>
    <div class="setting-row"><span>Teams in league</span><input id="cfg-teams" type="number" value="${M.num_teams}"></div>
    <div class="setting-row"><span>Starting slots</span><input id="cfg-active" type="number" value="${M.active_slots}"></div>
    <div class="setting-row"><span>Roster size (excl. IL)</span><input id="cfg-size" type="number" value="${M.roster_size}"></div>
    <div class="setting-row"><span>Max adds per week <span class="muted small">(blank = no limit)</span></span><input id="cfg-adds" type="number" min="0" placeholder="none"></div>
    <div style="margin-top:10px;display:flex;gap:8px;align-items:center"><button class="btn" id="cfg-save">Save settings</button><span class="small muted" id="cfg-status"></span></div>`;
  $("#cfg-save").addEventListener("click", async () => {
    const weights = {};
    $$("#settings-form [data-w]").forEach((i) => { if (i.value !== "") weights[i.dataset.w] = +i.value; });
    setupData = await api("/api/manual/config", { method: "POST", body: JSON.stringify({
      num_teams: +$("#cfg-teams").value, active_slots: +$("#cfg-active").value, roster_size: +$("#cfg-size").value, point_weights: weights }) });
    await api("/api/add-limit", { method: "POST", body: JSON.stringify({ max_adds_week: $("#cfg-adds").value === "" ? null : +$("#cfg-adds").value }) });
    $("#cfg-status").textContent = "Saved. Re-running the model…";
    afterManualChange();
  });
  const T = M.teams;
  $("#teams-table").innerHTML = T.length ? `<thead><tr><th>Team</th><th class="r">Players</th><th>Updated</th><th>Opponent this week</th><th></th></tr></thead><tbody>
    ${T.map((t) => `<tr><td><span class="pname">${esc(t.name)}</span>${t.is_mine ? ' <span class="chip mine">You</span>' : ""}</td>
      <td class="r num">${t.players}</td><td class="small ${t.updated && (Date.now() - new Date(t.updated)) > 7 * 864e5 ? "delta-down" : "muted"}">${esc(ago(t.updated))}</td>
      <td>${t.is_mine ? "" : `<label class="small" style="display:flex;gap:6px;align-items:center"><input type="radio" name="opp" value="${esc(t.team_key)}" ${M.opponent_key === t.team_key ? "checked" : ""}> this week</label>`}</td>
      <td class="r"><button class="btn ghost small" data-del="${esc(t.team_key)}">Remove</button></td></tr>`).join("")}</tbody>`
    : `<tr><td class="empty">No teams yet. Start with yours.</td></tr>`;
  $$("#teams-table input[name=opp]").forEach((r) => r.addEventListener("change", async () => {
    await api("/api/manual/opponent", { method: "POST", body: JSON.stringify({ team_key: r.value }) });
    afterManualChange();
  }));
  $$("#teams-table [data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Remove this team?")) return;
    await fetch(`/api/manual/team/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
    loadSetup(); afterManualChange();
  }));
  if (!T.some((t) => t.is_mine)) $("#team-mine").checked = true;
  const sel = $("#edit-team"), current = sel.value;
  sel.innerHTML = T.map((t) => `<option value="${esc(t.team_key)}">${esc(t.name)}${t.is_mine ? " (you)" : ""}</option>`).join("");
  sel.value = T.some((t) => t.team_key === current) ? current : (T.find((t) => t.is_mine)?.team_key || T[0]?.team_key || "");
  loadEditor(sel.value);
}

async function importTeam() {
  const name = $("#team-name").value.trim(), text = $("#team-text").value;
  const st = $("#team-status");
  if (!name) { st.textContent = "Enter the team's name."; return; }
  if (text.length < 20) { st.textContent = "Paste the team page first."; return; }
  st.textContent = "Importing…";
  const r = await fetch("/api/manual/team", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, text, mine: $("#team-mine").checked }) });
  const d = await r.json();
  if (!r.ok) { st.textContent = d.detail || "Import failed"; return; }
  st.innerHTML = `Imported ${d.players} players.${d.unmatched?.length ? ` <span class="delta-down">Couldn't match: ${esc(d.unmatched.join(", "))}</span>` : ""}`;
  $("#team-name").value = ""; $("#team-text").value = ""; $("#team-mine").checked = false;
  loadSetup(); afterManualChange();
}

async function afterManualChange() {
  // the server re-runs the model in the background; pick up the result when it lands
  const before = S?.last_refresh;
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 2500));
    const st = await api("/api/state");
    if (st.last_refresh !== before) { S = st; renderAll(); break; }
  }
}

function renderPointsBreakdown(P, el) {
  $("#cat-sub").textContent = "projected fantasy points by stat, full week";
  const LBL = { pts: "Points", reb: "Rebounds", ast: "Assists", stl: "Steals", blk: "Blocks", tov: "Turnovers", fg3m: "3PM", oreb: "Off. reb", dreb: "Def. reb", dd: "Double-dbl", td: "Triple-dbl", fgm: "FGM", fga: "FGA", ftm: "FTM", fta: "FTA" };
  const rows = Object.entries(P.breakdown);
  const max = Math.max(1, ...rows.flatMap(([, v]) => v.map(Math.abs)));
  const [me, op] = P.projected;
  el.innerHTML = `<div class="score-line"><span class="big num" style="color:var(--me)">${Math.round(me)}</span><span class="vs">vs</span>
      <span class="big num" style="color:var(--opp)">${Math.round(op)}</span><span class="small muted">projected final · ${pct(S.matchup.win_prob)} to win</span></div>
    <div class="pair-rows">${rows.map(([k, [a, b]]) => `
      <div class="pair-row" data-tip="${esc(LBL[k] || k)}: you ${fmt(a, 0)} fantasy pts, opponent ${fmt(b, 0)}">
        <span>${esc(LBL[k] || k)}</span>
        <div class="pair-bars"><span style="width:${Math.abs(a) / max * 100}%;background:var(--me);opacity:${a < 0 ? .5 : 1}"></span>
          <span style="width:${Math.abs(b) / max * 100}%;background:var(--opp);opacity:${b < 0 ? .5 : 1}"></span></div>
        <span class="nums num">${fmt(a, 0)}<br>${fmt(b, 0)}</span></div>`).join("")}</div>
    <div class="small muted" style="margin-top:8px">Turnovers count against you, shown faded.</div>`;
}

function renderMarginHist(m, el) {
  $("#hist-title").textContent = "Final margin (you − opponent)";
  const H = m?.points?.margin_hist;
  if (!H) { el.innerHTML = `<div class="empty" style="width:100%">No simulation yet.</div>`; $("#hist-note").innerHTML = ""; return; }
  const tot = H.counts.reduce((a, b) => a + b, 0) || 1, max = Math.max(...H.counts);
  el.setAttribute("aria-label", "Distribution of the final points margin");
  el.innerHTML = H.counts.map((c, i) => {
    const lo = H.edges[i], hi = H.edges[i + 1], mid = (lo + hi) / 2;
    return `<div class="col ${mid > 0 ? "win" : ""}" data-tip="Margin ${Math.round(lo)} to ${Math.round(hi)}: ${pct(c / tot, 1)} of simulations">
      <div class="b" style="height:${(c / max) * 80}%"></div><span>${i % 5 === 0 ? Math.round(mid) : ""}</span></div>`;
  }).join("");
  $("#hist-note").innerHTML = `<span class="legend"><span><i style="background:var(--me)"></i>You win (${pct(m.win_prob)})</span><span><i style="background:var(--axis)"></i>You lose</span></span>`;
}

/* ------------------------------------------------------------------ watchlist */
async function watchPlayer(pid, vs) {
  const r = await fetch("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ nba_id: pid, compare_id: vs ?? null }) });
  const d = await r.json();
  return r.ok ? d : { error: d.detail || "failed" };
}

function initWatchPicker() {
  const el = $("#watch-picker");
  el.innerHTML = `<input placeholder="Search any player to watch…" aria-label="Search players to watch"><div class="opts card hidden"></div>`;
  const input = $("input", el), opts = $(".opts", el);
  let t;
  input.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      if (input.value.trim().length < 2) { opts.classList.add("hidden"); return; }
      const hits = await api(`/api/players/search?q=${encodeURIComponent(input.value.trim())}&scope=others`);
      opts.innerHTML = hits.map((h) => `<div class="opt" data-id="${h.nba_id}"><span><b>${esc(h.name)}</b> <span class="pteam">${esc(h.team || "")}</span></span><span class="muted small">${esc(h.owner)}</span></div>`).join("") || `<div class="opt muted">No match</div>`;
      opts.classList.remove("hidden");
      $$(".opt[data-id]", opts).forEach((o) => o.addEventListener("click", async () => {
        input.value = ""; opts.classList.add("hidden");
        const r = await watchPlayer(+o.dataset.id);
        if (r.error) alert(r.error);
        loadWatch();
      }));
    }, 200);
  });
  input.addEventListener("blur", () => setTimeout(() => opts.classList.add("hidden"), 200));
}

async function loadWatch() {
  const d = await api("/api/watchlist");
  const el = $("#watch-list");
  if (!d.items.length) {
    el.innerHTML = `<div class="card"><div class="empty">Nothing watched yet. Search above, or press ☆ Watch on a recommended move or in a player's detail view.</div></div>`;
    return;
  }
  const unit = d.unit === "fantasy pts" ? "pts" : "value";
  el.innerHTML = d.items.map((w) => {
    const up = w.diff >= 0;
    const drift = w.proj_then != null ? w.proj_now - w.proj_then : null;
    const all = [...w.log, ...w.compare_log].map((x) => Math.abs(x.fp));
    const max = Math.max(1, ...all);
    const spark = (log, color) => `<div class="spark">${log.map((x) => `<span title="${esc(x.d)}: ${fmt(x.fp, 1)}" style="height:${Math.max(4, Math.abs(x.fp) / max * 100)}%;background:${color};opacity:${x.fp < 0 ? .45 : 1}"></span>`).join("") || `<span class="muted small" style="background:none">no games yet</span>`}</div>`;
    return `<div class="card watch-card">
      <div class="top"><a href="#" class="pname" data-pid="${w.nba_id}">${esc(w.name)}</a><span class="pteam">${esc(w.team || "no NBA team")}</span>
        <span class="chip ${w.status === "free agent" ? "" : "mine"}">${esc(w.status)}</span>${w.injury ? `<span class="status st-warning"><span class="dot"></span>${esc(w.injury)}</span>` : ""}
        <button class="btn ghost small" style="margin-left:auto" data-unwatch="${w.id}" title="Stop watching">✕</button></div>
      <div><div class="diff num ${w.games || w.compare_games ? (up ? "delta-up" : "delta-down") : "muted"}">${w.games || w.compare_games ? signed(w.diff, 1) + " " + unit : "–"}</div>
        <div class="small muted">since ${esc(shortDate(w.since))}, if you'd swapped him in for
          <select data-compare="${w.id}">${d.my_roster.map((p) => `<option value="${p.nba_id}" ${p.nba_id === w.compare_id ? "selected" : ""}>${esc(p.name)}</option>`).join("")}</select></div></div>
      <div class="vs">
        <div>${esc(w.name)}<b class="num">${fmt(w.points, 1)}</b>${w.games} g${w.per_game != null ? ` · ${fmt(w.per_game, 1)}/g` : ""}${spark(w.log, "var(--me)")}</div>
        <div>${esc(w.compare_name || "—")}<b class="num">${fmt(w.compare_points, 1)}</b>${w.compare_games} g${w.compare_per_game != null ? ` · ${fmt(w.compare_per_game, 1)}/g` : ""}${spark(w.compare_log, "var(--opp)")}</div>
      </div>
      <div class="small ink2">Projection: ${fmt(w.proj_then, 1)} → <b>${fmt(w.proj_now, 1)}</b> ${unit}/g
        ${drift != null && Math.abs(drift) >= 0.5 ? `<span class="${drift > 0 ? "delta-up" : "delta-down"}">(${signed(drift, 1)})</span>` : "(unchanged)"}
        · ${esc(w.compare_name || "")} now ${fmt(w.compare_proj_now, 1)}</div>
    </div>`;
  }).join("");
  $$("#watch-list [data-unwatch]").forEach((b) => b.addEventListener("click", async () => {
    await fetch(`/api/watchlist/${b.dataset.unwatch}`, { method: "DELETE" }); loadWatch();
  }));
  $$("#watch-list [data-compare]").forEach((s) => s.addEventListener("change", async () => {
    await api(`/api/watchlist/${s.dataset.compare}`, { method: "POST", body: JSON.stringify({ compare_id: +s.value }) }); loadWatch();
  }));
  bindPlayerLinks(el);
}

/* ------------------------------------------------------------------ roster editor (manual mode) */
async function loadEditor(key) {
  const el = $("#roster-editor");
  if (!key) { el.innerHTML = `<div class="empty">No teams yet.</div>`; return; }
  const d = await api(`/api/manual/roster/${encodeURIComponent(key)}`);
  el.innerHTML = `<div class="picker" id="editor-add" style="margin-bottom:10px"></div>
    ${d.players.map((p) => `<div class="editor-row">
      <select data-slot="${p.nba_id}">${d.slots.map((s) => `<option ${s === p.slot ? "selected" : ""}>${s}</option>`).join("")}</select>
      <span><a href="#" class="pname" data-pid="${p.nba_id}">${esc(p.name)}</a> <span class="pteam">${esc(p.team || "no NBA team")}</span></span>
      <button class="btn ghost small" data-drop="${p.nba_id}">Drop</button></div>`).join("")}
    <div class="small muted" style="margin-top:8px">${d.players.length} players. On your team, a drop followed by an add is logged as one move and scored like any other.</div>`;
  const pk = $("#editor-add");
  pk.innerHTML = `<input placeholder="Add a player to this team…"><div class="opts card hidden"></div>`;
  const input = $("input", pk), opts = $(".opts", pk);
  let t;
  input.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      if (input.value.trim().length < 2) { opts.classList.add("hidden"); return; }
      const hits = await api(`/api/players/search?q=${encodeURIComponent(input.value.trim())}`);
      opts.innerHTML = hits.map((h) => `<div class="opt" data-id="${h.nba_id}"><span><b>${esc(h.name)}</b> <span class="pteam">${esc(h.team || "")}</span></span><span class="muted small">${esc(h.owner)}</span></div>`).join("");
      opts.classList.remove("hidden");
      $$(".opt[data-id]", opts).forEach((o) => o.addEventListener("click", () => editRoster(key, { add: +o.dataset.id })));
    }, 200);
  });
  input.addEventListener("blur", () => setTimeout(() => opts.classList.add("hidden"), 200));
  // Drop: inline choices on the row (no browser pop-ups: they're easy to dismiss by accident
  // and Chrome can silently block repeated ones). On your team you can also hold the drop
  // and pair it with your next add, so the swap is scored as one move.
  const mineTeam = setupData?.teams.find((t) => t.team_key === key)?.is_mine;
  if (pendingDrop && pendingDrop.key === key) {
    const pd = d.players.find((p) => p.nba_id === pendingDrop.pid);
    pk.insertAdjacentHTML("beforebegin", `<div class="note" style="border-left:3px solid var(--warning);margin-bottom:8px">
      <b>${esc(pd ? pd.name : "Player")}</b> will be dropped when you add someone below (logged as one swap).
      <button class="btn ghost small" id="pending-now">Drop him now instead</button> <button class="btn ghost small" id="pending-cancel">Cancel</button></div>`);
    $("#pending-now").addEventListener("click", () => { const pid = pendingDrop.pid; pendingDrop = null; editRoster(key, { drop: pid }); });
    $("#pending-cancel").addEventListener("click", () => { pendingDrop = null; loadEditor(key); });
  }
  $$("[data-drop]", el).forEach((b) => b.addEventListener("click", () => {
    const pid = +b.dataset.drop;
    const cell = b.parentElement;
    b.outerHTML = `<span style="display:inline-flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">
      <button class="btn small" data-act="now" style="color:var(--critical)">Drop now</button>
      ${mineTeam ? '<button class="btn ghost small" data-act="pair" title="Hold the drop until you add someone: logged as one swap">Drop with my next add</button>' : ""}
      <button class="btn ghost small" data-act="cancel">Cancel</button></span>`;
    $("[data-act=now]", cell).addEventListener("click", () => editRoster(key, { drop: pid }));
    const pair = $("[data-act=pair]", cell);
    if (pair) pair.addEventListener("click", () => { pendingDrop = { key, pid }; loadEditor(key); });
    $("[data-act=cancel]", cell).addEventListener("click", () => loadEditor(key));
  }));
  $$("[data-slot]", el).forEach((s) => s.addEventListener("change", async () => {
    await api("/api/manual/slot", { method: "POST", body: JSON.stringify({ team_key: key, nba_id: +s.dataset.slot, slot: s.value }) });
    afterManualChange();
  }));
  bindPlayerLinks(el);
}

let pendingDrop = null;  // {key, pid}: a drop held until the next add on that team
async function editRoster(key, change) {
  const body = { team_key: key, ...change };
  if (change.add && pendingDrop && pendingDrop.key === key) {  // the held drop goes with this add
    body.drop = pendingDrop.pid;
    pendingDrop = null;
  }
  const r = await fetch("/api/manual/roster", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    $("#roster-editor").insertAdjacentHTML("afterbegin", `<div class="note" style="border-left:3px solid var(--critical);margin-bottom:8px">${esc(d.detail || "Edit failed")}</div>`);
    return;
  }
  loadEditor(key); loadSetup(); afterManualChange();
}

/* ------------------------------------------------------------------ lineup optimizer */
let lineupData = null, lineupDay = 0;
async function loadLineup() {
  try { lineupData = await api("/api/lineup"); } catch (e) { return; }
  renderLineup();
}

function renderLineup() {
  const L = lineupData;
  const el = $("#lineup");
  if (!L || !L.days?.length) { el.innerHTML = `<div class="empty">No games left this week.</div>`; $("#lineup-days").innerHTML = ""; return; }
  lineupDay = Math.min(lineupDay, L.days.length - 1);
  const unit = L.unit === "fantasy pts" ? "pts" : "value";
  $("#lineup-days").innerHTML = L.days.map((d, i) => {
    const label = d.date === L.today ? "Today" : new Date(d.date + "T12:00:00").toLocaleDateString(undefined, { weekday: "short" });
    return `<button aria-pressed="${i === lineupDay}" data-day="${i}">${label}${d.gain > 0.5 ? ` · +${fmt(d.gain, 0)}` : ""}</button>`;
  }).join("");
  $$("#lineup-days button").forEach((b) => b.addEventListener("click", () => { lineupDay = +b.dataset.day; renderLineup(); }));
  const D = L.days[lineupDay];
  const isToday = D.date === L.today;
  const changes = D.start.length || D.sit.length;
  $("#lineup-sub").textContent = `${fmt(D.ev_optimal, 0)} expected ${unit} · ${D.gain > 0.5 ? `+${fmt(D.gain, 1)} vs your current lineup` : "your current lineup is already optimal"}${L.week_gain > 0.5 ? ` · +${fmt(L.week_gain, 0)} over the rest of the week` : ""}`;
  el.innerHTML = `
    ${changes && D.gain > 0.5 ? `<div class="note" style="border-left:3px solid var(--serious);margin-bottom:10px"><b>${isToday ? "Change today" : "Planned changes"}:</b>
      ${D.start.length ? `start ${esc(D.start.join(", "))}` : ""}${D.start.length && D.sit.length ? "; " : ""}${D.sit.length ? `bench ${esc(D.sit.join(", "))}` : ""}
      <span class="muted">(+${fmt(D.gain, 1)} expected ${unit})</span>
      ${isToday ? `<button class="btn" id="lineup-apply" style="margin-left:8px">I set this in Yahoo</button>` : ""}</div>` : ""}
    <div class="tbl-wrap"><table><thead><tr><th>Slot</th><th>Player</th><th>Game</th><th class="r" title="Chance he plays, after injuries and rest risk">Plays</th><th class="r">Exp. ${unit}</th><th></th></tr></thead><tbody>
    ${D.lineup.map((x) => x.empty ? `<tr><td><span class="slot">${esc(x.slot)}</span></td><td class="muted" colspan="5">empty: nobody eligible with a game</td></tr>` : `<tr>
      <td><span class="slot">${esc(x.slot)}</span></td>
      <td><a href="#" data-pid="${x.nba_id}" class="pname">${esc(x.name)}</a> <span class="pteam">${esc(x.positions.join(","))}${x.pos_source !== "yahoo" ? ` (${esc(x.pos_source)})` : ""}</span></td>
      <td class="small">${esc(x.game || "")}${x.tip ? ` <span class="muted">${esc(x.tip)}</span>` : ""}</td><td class="r num small">${pct(x.avail)}</td><td class="r num">${fmt(x.ev, 1)}</td>
      <td class="small">${x.locked ? '<span class="muted" title="His game has started: Yahoo has locked him">🔒 locked</span>' : x.current_slot === "BN" ? '<span class="delta-up">▲ start</span>' : ""}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="small muted" style="margin-top:8px">Bench: ${D.bench.map((b) => `${esc(b.name)} <span class="muted">(${esc(b.reason)})</span>`).join(", ") || "nobody"}</div>
    ${D.unknown_positions.length ? `<div class="small delta-down" style="margin-top:4px">Unknown positions (Util only): ${esc(D.unknown_positions.join(", "))}. Re-paste that team to fix.</div>` : ""}
    <div class="small muted" style="margin-top:4px">${D.first_tip ? `First tip-off ${isToday ? "today" : "that day"}: <b>${esc(D.first_tip)}</b>. ` : ""}Yahoo locks each player when his game starts${D.locked && D.locked.length ? ` (locked now: ${esc(D.locked.join(", "))})` : ""}. Set this before tip-off, then press "I set this in Yahoo" so the week-to-date score follows the right starters.</div>`;
  const ap = $("#lineup-apply");
  if (ap) ap.addEventListener("click", async () => {
    await api("/api/lineup/apply", { method: "POST" });
    ap.textContent = "Saved"; ap.disabled = true;
    afterManualChange().then(loadLineup);
  });
  bindPlayerLinks(el);
}

/* ------------------------------------------------------------------ pickup check */
function initCheckPicker() {
  const el = $("#check-picker");
  el.innerHTML = `<input placeholder="Search any player…" aria-label="Search a player to check"><div class="opts card hidden"></div>`;
  const input = $("input", el), opts = $(".opts", el);
  let t;
  input.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      if (input.value.trim().length < 2) { opts.classList.add("hidden"); return; }
      const hits = await api(`/api/players/search?q=${encodeURIComponent(input.value.trim())}&scope=others`);
      opts.innerHTML = hits.map((h) => `<div class="opt" data-id="${h.nba_id}"><span><b>${esc(h.name)}</b> <span class="pteam">${esc(h.team || "no team")}</span></span><span class="muted small">${esc(h.owner)}</span></div>`).join("") || `<div class="opt muted">No match</div>`;
      opts.classList.remove("hidden");
      $$(".opt[data-id]", opts).forEach((o) => o.addEventListener("click", () => {
        input.value = ""; opts.classList.add("hidden"); checkPlayer(+o.dataset.id);
      }));
    }, 200);
  });
  input.addEventListener("blur", () => setTimeout(() => opts.classList.add("hidden"), 200));
}

async function checkPlayer(pid) {
  const el = $("#check-result");
  el.innerHTML = `<div class="card"><div class="empty">Simulating…</div></div>`;
  const e = await api("/api/evaluate", { method: "POST", body: JSON.stringify({ nba_id: pid }) });
  const unit = S.league.scoring_mode === "points" ? "fantasy pts/game" : "value/game";
  const best = e.options[0];
  const V = {
    "Do today": ["st-serious", "Pick him up today"], "Worth doing": ["st-good", "Worth picking up, no rush"],
    "Marginal": ["", "Marginal: small or uncertain gain"], "not worth it": ["st-critical", "Not worth it: no drop makes your team better"],
    "owned": ["st-warning", "Not available: on another team"], "already yours": ["", "Already on your team"],
  }[e.verdict] || ["", e.verdict];
  const manual = !!S.manual;
  el.innerHTML = `<div class="card"><div class="hd">
      <h2><a href="#" class="pname" data-pid="${e.nba_id}">${esc(e.name)}</a></h2><span class="pteam">${esc(e.team || "no NBA team")} · ${fmt(e.per_game, 1)} ${unit}</span>
      <span class="status ${V[0]}" style="margin-left:auto"><span class="dot" ${V[0] ? "" : 'style="background:var(--axis)"'}></span>${esc(V[1])}</span></div>
    <div class="bd">
      ${e.warnings.map((w) => `<div class="note" style="border-left:3px solid var(--warning);margin-bottom:8px">${esc(w)}</div>`).join("")}
      ${best && e.verdict !== "owned" ? `<p style="margin:4px 0 12px">Best option: <b>drop ${esc(best.drop_name)}</b> → ${signed(best.score, 2)} expected wins
        (${Math.abs(best.week_dwin * 100) < 1.5 ? "≈0" : signed(best.week_dwin * 100, 1)} pp this week, ${signed(best.season_dwins, 2)} wins rest of season), ${pct(best.confidence)} confident.
        ${best.urgency_reason ? `<span class="muted">${esc(best.urgency)}: ${esc(best.urgency_reason)}.</span>` : ""}</p>` : ""}
      ${e.options.length ? `<div class="tbl-wrap"><table><thead><tr><th>If you drop</th><th class="r">This week</th><th class="r">Rest of season</th><th class="r">Total</th><th class="r">Confidence</th><th></th></tr></thead><tbody>
        ${e.options.map((o, i) => `<tr ${i === 0 && e.verdict !== "owned" ? 'style="background:var(--surface-2)"' : ""}>
          <td><span class="pname">${esc(o.drop_name)}</span> <span class="pteam">${esc(o.drop_team || "")} · ${o.drop_games} g left</span></td>
          <td class="r num">${Math.abs(o.week_dwin * 100) < 1.5 ? "≈0" : signed(o.week_dwin * 100, 1) + "pp"}</td>
          <td class="r num">${signed(o.season_dwins, 2)}</td>
          <td class="r num"><b class="${o.score > 0.01 ? "delta-up" : o.score < -0.01 ? "delta-down" : ""}">${signed(o.score, 2)}</b></td>
          <td class="r num">${pct(o.confidence)}</td>
          <td class="r">${manual && e.verdict !== "owned" && o.score > 0 ? `<button class="btn ghost small" data-do="${o.drop}">Made this move</button>` : ""}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="small muted" style="margin-top:8px">"Total" is expected matchup wins added: this week's win-probability change plus the rest-of-season gain. This-week changes within ±1.5pp are simulation noise (≈0).</div>` : ""}
      <div style="margin-top:10px;display:flex;gap:8px">
        ${e.verdict !== "already yours" ? `<button class="btn" id="check-watch">☆ Watch${best ? ` vs ${esc(best.drop_name)}` : ""}</button>` : ""}
      </div>
    </div></div>`;
  bindPlayerLinks(el);
  const w = $("#check-watch");
  if (w) w.addEventListener("click", async () => {
    const r = await watchPlayer(e.nba_id, best ? best.drop : null);
    w.textContent = r.error ? r.error : "★ Watching"; w.disabled = true;
  });
  $$("#check-result [data-do]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Add ${e.name} and drop this player on your roster here? (Make the move in Yahoo too.)`)) return;
    const r = await fetch("/api/manual/roster", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ team_key: S.manual.teams.find((t) => t.is_mine).team_key, add: e.nba_id, drop: +b.dataset.do, slot: "BN" }) });
    const d = await r.json();
    b.textContent = r.ok ? "Done: logged" : (d.detail || "failed"); b.disabled = true;
    if (r.ok) afterManualChange();
  }));
}

/* ------------------------------------------------------------------ history */
const tx = { ins: [], outs: [] };
function txPicker(el, side, scope) {
  el.innerHTML = `<input placeholder="Search…"><div class="opts card hidden"></div><div class="picked"></div>`;
  const input = $("input", el), opts = $(".opts", el);
  let t;
  input.addEventListener("input", () => {
    clearTimeout(t);
    t = setTimeout(async () => {
      if (input.value.trim().length < 2) { opts.classList.add("hidden"); return; }
      const hits = await api(`/api/players/search?q=${encodeURIComponent(input.value.trim())}&scope=${scope()}`);
      opts.innerHTML = hits.map((h) => `<div class="opt" data-id="${h.nba_id}" data-name="${esc(h.name)}"><span><b>${esc(h.name)}</b> <span class="pteam">${esc(h.team || "")}</span></span><span class="muted small">${esc(h.owner)}</span></div>`).join("") || `<div class="opt muted">No match</div>`;
      opts.classList.remove("hidden");
      $$(".opt[data-id]", opts).forEach((o) => o.addEventListener("click", () => {
        if (!tx[side].some((x) => x.id === +o.dataset.id)) tx[side].push({ id: +o.dataset.id, name: o.dataset.name });
        input.value = ""; opts.classList.add("hidden"); renderTxPicked();
      }));
    }, 200);
  });
  input.addEventListener("blur", () => setTimeout(() => opts.classList.add("hidden"), 200));
}

function renderTxPicked() {
  [["ins", "#tx-in"], ["outs", "#tx-out"]].forEach(([side, sel]) => {
    $(`${sel} .picked`).innerHTML = tx[side].map((p, i) => `<span class="chip" data-i="${i}" title="Remove">${esc(p.name)} ✕</span>`).join("");
    $$(`${sel} .picked .chip`).forEach((c) => c.addEventListener("click", () => { tx[side].splice(+c.dataset.i, 1); renderTxPicked(); }));
  });
}

function initHistoryForm() {
  $("#tx-date").value = new Date().toISOString().slice(0, 10);
  $("#tx-date").max = $("#tx-date").value;
  txPicker($("#tx-in"), "ins", () => "others");
  txPicker($("#tx-out"), "outs", () => "mine");
  $("#tx-kind").addEventListener("change", async () => {
    const trade = $("#tx-kind").value === "trade";
    $("#tx-other").classList.toggle("hidden", !trade);
    $("#tx-in-label").textContent = trade ? "You got (from the other team)" : "You got";
    if (trade && !$("#tx-other").options.length) {
      const m = await api("/api/manual").catch(() => null);
      const teams = (m?.teams || S?.league_table || []).filter((t) => !t.is_mine);
      $("#tx-other").innerHTML = `<option value="">Other team…</option>` + teams.map((t) => `<option value="${esc(t.team_key)}">${esc(t.name)}</option>`).join("");
    }
  });
  $("#tx-save").addEventListener("click", async () => {
    const st = $("#tx-status");
    const body = { kind: $("#tx-kind").value, ins: tx.ins.map((p) => p.id), outs: tx.outs.map((p) => p.id),
      effective: $("#tx-date").value, other_key: $("#tx-other").value || null, note: $("#tx-note").value };
    st.textContent = "Recording…";
    const r = await fetch("/api/history", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok) { st.textContent = d.detail || "Failed"; return; }
    st.textContent = "Recorded."; tx.ins = []; tx.outs = []; $("#tx-note").value = ""; renderTxPicked();
    loadHistory(); afterManualChange();
  });
}

async function loadHistory() {
  loadTakes();
  loadModelTeam();
  const H = await api("/api/history");
  const unit = H.unit === "fantasy pts" ? "pts" : "value";
  const sc = H.scorecard;
  const line = (lbl, x) => `<div class="setting-row" style="grid-template-columns:1fr auto"><span>${lbl}</span><span class="num">${x.n ? `<b>${x.hits}/${x.n}</b> worked · net ${signed(x.net, 0)} ${unit}` : '<span class="muted">none finished yet</span>'}</span></div>`;
  $("#tx-score").innerHTML = line("Your own calls", sc.your_calls) + line("Suggestions you followed", sc.followed_suggestions) +
    `<div class="small muted" style="margin-top:8px">A move "worked" if the players you got outscored the players you gave up over the next 21 days (28 for trades). Your own calls also feed the model's confidence calibration.</div>`;
  const SRC = { manual: "you entered", editor: "roster editor", paste: "re-paste", suggestion: "followed a suggestion", yahoo: "Yahoo" };
  const items = H.items.map((t) => ({ date: t.effective, html: txCard(t, unit, SRC) }));
  const weeks = H.weeks.filter((w) => w.result != null || w.win_prob != null).map((w) => ({ date: "", week: w.week, html: `<div class="tx week">
      <div class="when">Week ${w.week}</div>
      <div>vs <b>${esc(w.opponent || "?")}</b> <span class="muted small">· forecast ${pct(w.win_prob)} to win</span></div>
      <div class="res">${w.result == null ? '<span class="muted">in progress</span>' : `<span class="status ${w.result === 1 ? "st-good" : w.result === 0 ? "st-critical" : "st-warning"}"><span class="dot"></span>${w.result === 1 ? "Won" : w.result === 0 ? "Lost" : "Tied"}</span>`}</div></div>` }));
  const el = $("#tx-list");
  el.innerHTML = (items.length || weeks.length) ? [...weeks, ...items].map((x) => x.html).join("")
    : `<div class="empty">Nothing yet. Moves you make show up here: from the roster editor, re-pastes, suggestions you follow, or the form above.</div>`;
  $$("#tx-list [data-txdate]").forEach((i) => i.addEventListener("change", async () => {
    await api(`/api/history/${i.dataset.txdate}`, { method: "POST", body: JSON.stringify({ effective: i.value }) }); loadHistory();
  }));
  $$("#tx-list [data-txdel]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Remove this entry and undo its roster change?")) return;
    const r = await (await fetch(`/api/history/${b.dataset.txdel}`, { method: "DELETE" })).json();
    if (r.note) alert(r.note);
    loadHistory();
    if (r.roster_reverted) afterManualChange();
  }));
  bindPlayerLinks(el);
}

function txCard(t, unit, SRC) {
  const p = t.prediction || {}, e = t.eval;
  const kind = { "add/drop": "Add / drop", add: "Add", drop: "Drop", trade: "Trade", "roster change": "Roster change" }[t.kind] || t.kind;
  const said = p.score != null ? `model: ${signed(p.score, 2)} wins${p.confidence != null ? `, ${pct(p.confidence)} confident` : ""}`
    : p.season_dwins != null ? `model: ${signed(p.season_dwins, 2)} wins ROS` : "";
  const pg = p.per_game_diff != null ? `${signed(p.per_game_diff, 1)} ${unit}/game at the time` : "";
  let res = '<span class="muted">no games yet</span>';
  if (e && (e.games_in || e.games_out)) {
    const ok = e.diff > 0;
    res = `<b class="num ${ok ? "delta-up" : "delta-down"}">${signed(e.diff, 1)} ${unit}</b>
      ${fmt(e.in, 0)} in (${e.games_in} g) vs ${fmt(e.out, 0)} out (${e.games_out} g)<br>
      ${e.final ? `<span class="status ${ok ? "st-good" : "st-critical"}"><span class="dot"></span>${ok ? "Worked" : "Didn't work"}</span>` : `<span class="muted">so far · final ${esc(shortDate(e.horizon_end))}</span>`}`;
  }
  return `<div class="tx">
    <div class="when"><input type="date" value="${esc(t.effective)}" max="${new Date().toISOString().slice(0, 10)}" data-txdate="${t.id}" class="btn" style="padding:2px 4px;font-size:11px;cursor:text" title="When it happened (change to re-score)">
      <div style="margin-top:4px"><span class="chip">${esc(kind)}</span></div></div>
    <div class="players">
      ${t.in.map((x) => `<div class="in">+ <a href="#" data-pid="${x.nba_id}">${esc(x.name)}</a> <span class="pteam">${esc(x.team || "")}</span></div>`).join("")}
      ${t.out.map((x) => `<div class="out">− <a href="#" data-pid="${x.nba_id}">${esc(x.name)}</a> <span class="pteam">${esc(x.team || "")}</span></div>`).join("")}
      <div class="small muted">${esc(SRC[t.source] || t.source)}${t.other_team ? ` · with ${esc(t.other_team)}` : ""}${said ? ` · ${esc(said)}` : ""}${pg ? ` · ${esc(pg)}` : ""}${t.note ? ` · "${esc(t.note)}"` : ""}</div>
    </div>
    <div class="res">${res}<div><button class="btn ghost small" data-txdel="${t.id}" style="margin-top:6px">Remove</button></div></div>
  </div>`;
}

/* ------------------------------------------------------------------ your take */
function takeBox(p) {
  const T = p.take || {};
  if (T.model_per_game == null) return "";
  const unit = S.league.scoring_mode === "points" ? "pts/game" : "value";
  const a = T.active;
  const mode = a ? a.kind : "minutes";
  const val = a ? a.value : Math.round((T.model_min || 0) * 10) / 10;
  return `<div class="take-box" id="take-box" data-mode="${mode}">
    <div class="row"><b>Your take</b>
      <span class="muted small">model: ${fmt(T.model_min, 1)} min, ${fmt(T.model_per_game, 1)} ${unit}${a ? ` · <b style="color:var(--opp)">using yours: ${fmt(T.your_per_game, 1)} ${unit}</b>` : ""}</span></div>
    <div class="row">
      <span class="seg"><button data-k="minutes" aria-pressed="${mode === "minutes"}">Minutes per game</button><button data-k="pct" aria-pressed="${mode === "pct"}">Production ±%</button></span>
      <input type="number" id="take-val" step="0.5" value="${val}"> <span class="small muted" id="take-unit">${mode === "minutes" ? "min" : "%"}</span>
      <span class="small" id="take-preview"></span>
    </div>
    <div class="row"><input type="text" id="take-note" placeholder="Why? (e.g. more minutes after last game)" value="${esc(a?.note || "")}">
      <button class="btn primary small" id="take-save">${a ? "Update" : "Use my take"}</button>
      ${a ? '<button class="btn ghost small" id="take-clear">Back to the model</button>' : ""}</div>
    <div class="small muted">Used everywhere (lineups, moves, simulations). The model keeps its own number too, and History shows whose was closer.</div>
  </div>`;
}

function bindTakeBox(p, pid) {
  const box = $("#take-box");
  if (!box) return;
  const T = p.take;
  const unit = S.league.scoring_mode === "points" ? "pts/game" : "value";
  const preview = () => {
    const v = parseFloat($("#take-val").value);
    if (isNaN(v)) { $("#take-preview").textContent = ""; return; }
    const f = box.dataset.mode === "minutes" ? v / Math.max(T.model_min, 1) : 1 + v / 100;
    const yours = T.model_per_game * Math.min(Math.max(f, 0.3), 2.5);
    $("#take-preview").innerHTML = `→ <b>${fmt(yours, 1)} ${unit}</b> <span class="${yours >= T.model_per_game ? "delta-up" : "delta-down"}">(${signed(yours - T.model_per_game, 1)})</span>`;
  };
  $$(".seg button", box).forEach((b) => b.addEventListener("click", () => {
    box.dataset.mode = b.dataset.k;
    $$(".seg button", box).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("#take-val").value = b.dataset.k === "minutes" ? Math.round(T.model_min * 10) / 10 : 0;
    $("#take-unit").textContent = b.dataset.k === "minutes" ? "min" : "%";
    preview();
  }));
  $("#take-val").addEventListener("input", preview);
  preview();
  $("#take-save").addEventListener("click", async () => {
    const r = await fetch("/api/take", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ nba_id: pid, kind: box.dataset.mode, value: parseFloat($("#take-val").value), note: $("#take-note").value }) });
    const d = await r.json();
    if (!r.ok) { $("#take-preview").textContent = d.detail || "Couldn't save"; return; }
    $("#take-save").textContent = "Saved: updating projections…"; $("#take-save").disabled = true;
    afterManualChange().then(() => openPlayer(pid));
  });
  const clr = $("#take-clear");
  if (clr) clr.addEventListener("click", async () => {
    await fetch(`/api/take/${pid}`, { method: "DELETE" });
    clr.textContent = "Cleared: updating…"; clr.disabled = true;
    afterManualChange().then(() => openPlayer(pid));
  });
}

async function loadTakes() {
  const R = await api("/api/takes");
  const unit = R.unit === "fantasy pts" ? "pts" : "value";
  const el = $("#takes-list");
  if (!R.items.length) { el.innerHTML = `<div class="empty">No takes yet. Open any player and use "Your take" when you disagree with the model.</div>`; return; }
  el.innerHTML = `<div class="small ink2" style="margin-bottom:8px">${R.judged ? `<b>Closer than the model in ${R.yours_closer} of ${R.judged}</b> takes with 3+ games played.` : "Takes are judged once the player has played 3+ games since you set them."}</div>` +
    R.items.map((t) => `<div class="setting-row" style="grid-template-columns:1fr auto;border-bottom:1px solid var(--grid);padding:8px 0">
      <span><a href="#" data-pid="${t.nba_id}" class="pname">${esc(t.name)}</a> <span class="muted small">${t.kind === "minutes" ? `${fmt(t.value, 1)} min` : signed(t.value, 0) + "%"} · since ${esc(shortDate(t.since))}${t.active ? "" : " · cleared"}${t.note ? ` · "${esc(t.note)}"` : ""}</span><br>
        <span class="small">you ${fmt(t.your_per_game, 1)} · model ${fmt(t.model_per_game, 1)}${t.actual_per_game != null ? ` · actual <b>${fmt(t.actual_per_game, 1)}</b> ${unit}/g over ${t.games} g (${fmt(t.actual_min, 1)} min)` : " · no games yet"}</span></span>
      <span class="small">${t.closer ? `<span class="status ${t.closer === "you" ? "st-good" : "st-warning"}"><span class="dot"></span>${t.closer === "you" ? "You're closer" : "Model's closer"}</span>${t.games < 3 ? '<br><span class="muted">early</span>' : ""}` : ""}</span></div>`).join("");
  bindPlayerLinks(el);
}

/* ------------------------------------------------------------------ phone alerts */
function renderAlertSettings(A) {
  const el = $("#alerts-settings");
  const T = A.types;
  const labels = { lineup: "Lineup changes (late morning)", moves: '"Do today" moves', news: "News on your players", injury: "Injury status changes" };
  el.innerHTML = `
    <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
      <label style="display:flex;gap:6px;align-items:center;font-weight:600"><input type="checkbox" id="al-on" ${A.enabled ? "checked" : ""}> Send alerts to my phone</label>
      ${A.enabled ? `<button class="btn small" id="al-test">Send a test alert</button><span class="small muted" id="al-status"></span>` : ""}
    </div>
    ${A.enabled ? `
    <ol class="small ink2" style="margin:10px 0 6px;padding-left:18px;line-height:1.7">
      <li>Install <b>ntfy</b> from the App Store or Google Play (free, no account).</li>
      <li>Tap <b>+</b> (subscribe to topic) and enter exactly: <code style="user-select:all;background:var(--surface-2);padding:2px 6px;border-radius:4px">${esc(A.topic)}</code></li>
      <li>Press "Send a test alert" above to check it arrives.</li>
    </ol>
    <div style="display:flex;gap:14px;flex-wrap:wrap;margin-top:6px">${Object.keys(labels).map((k) =>
      `<label class="small" style="display:flex;gap:6px;align-items:center"><input type="checkbox" data-al="${k}" ${T[k] ? "checked" : ""}> ${labels[k]}</label>`).join("")}</div>
    <div class="small muted" style="margin-top:8px">Only short alert text is sent, via ntfy.sh. The channel name is long and random so others can't guess it;
      <a href="#" id="al-rotate">make a new one</a> if you ever share it by mistake. Nothing is sent between 11pm and 8am.</div>` :
      `<div class="small muted" style="margin-top:6px">Free push notifications through the ntfy app. Turn on to get the setup steps.</div>`}`;
  $("#al-on").addEventListener("change", async (e) => {
    renderAlertSettings(await api("/api/alerts", { method: "POST", body: JSON.stringify({ enabled: e.target.checked }) }));
  });
  $$("[data-al]", el).forEach((c) => c.addEventListener("change", async () => {
    const types = {}; $$("[data-al]", el).forEach((x) => { types[x.dataset.al] = x.checked; });
    await api("/api/alerts", { method: "POST", body: JSON.stringify({ types }) });
  }));
  const t = $("#al-test");
  if (t) t.addEventListener("click", async () => {
    $("#al-status").textContent = "Sending…";
    const r = await fetch("/api/alerts/test", { method: "POST" });
    $("#al-status").textContent = r.ok ? "Sent. Check your phone." : ((await r.json()).detail || "Failed");
  });
  const rot = $("#al-rotate");
  if (rot) rot.addEventListener("click", async (e) => {
    e.preventDefault();
    if (!confirm("Make a new channel name? You'll need to re-subscribe in the ntfy app.")) return;
    renderAlertSettings(await api("/api/alerts", { method: "POST", body: JSON.stringify({ new_topic: true }) }));
  });
}

/* ------------------------------------------------------------------ trade finder */
async function runTradeFinder() {
  const out = $("#tf-out"), btn = $("#tf-run");
  btn.disabled = true;
  await api("/api/trades/find", { method: "POST" });
  let st;
  for (let i = 0; i < 240; i++) {
    st = await api("/api/trades/find");
    out.innerHTML = `<div class="small muted">Searching… ${st.total ? `${st.done} of ${st.total} candidate trades evaluated` : "screening every combination"}</div>
      <div class="meter"><span style="width:${st.total ? (st.done / st.total) * 100 : 3}%"></span></div>`;
    if (!st.running && (st.result || st.error)) break;
    await new Promise((r) => setTimeout(r, 1000));
  }
  btn.disabled = false;
  if (st.error) { out.innerHTML = `<div class="note" style="border-left:3px solid var(--critical)">${esc(st.error)}</div>`; return; }
  renderTradeFinder(st.result);
}

const STAT_LBL = { pts: "scoring", reb: "rebounds", ast: "assists", stl: "steals", blk: "blocks", tov: "turnovers" };
function renderTradeFinder(R) {
  const out = $("#tf-out");
  if (!R.trades.length) { out.innerHTML = `<div class="empty">No trade found that helps you without hurting them (${R.evaluated} checked).</div>`; return; }
  const pl = (x) => `<a href="#" data-pid="${x.nba_id}">${esc(x.name)}</a> <span class="pteam">${esc(x.team || "")} · ${fmt(x.per_game, 1)}</span>${x.status ? ` <span class="status st-warning" title="${esc(x.note || "")}"><span class="dot"></span>${esc(x.status)}</span>` : ""}`;
  out.innerHTML = `<div class="small muted" style="margin-bottom:8px">${R.qualifying} of ${R.evaluated} evaluated trades qualify; best ${R.trades.length} shown (at most 2 per team). Numbers are fantasy pts/game.</div>` +
    R.trades.map((t, i) => {
      const shift = Object.entries(t.stat_shift).filter(([, v]) => Math.abs(v) >= 1).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 3)
        .map(([k, v]) => `${STAT_LBL[k] || k} ${signed(v, 1)}`).join(", ");
      return `<div class="move" style="margin-bottom:8px">
        <div class="swap" style="display:grid;gap:4px">
          <div><span class="chip">${esc(t.shape)}</span> with <b>${esc(t.other_team)}</b></div>
          <div class="add"><span class="sign">+</span> ${t.get.map(pl).join(" &nbsp; ")}</div>
          <div class="drop"><span class="sign">−</span> ${t.give.map(pl).join(" &nbsp; ")}</div>
        </div>
        <div class="nums num">
          <div><b class="delta-up">${signed(t.my_dwins, 2)}</b><span>wins for you</span></div>
          <div><b>${signed(t.their_dwins, 2)}</b><span>for them</span></div>
        </div>
        <ul>${shift ? `<li>Per game: ${esc(shift)}</li>` : ""}
          ${t.auto_filled.length ? `<li>Frees a roster spot: best free agent added (${esc(t.auto_filled.join(", "))})</li>` : ""}
          ${t.auto_dropped.length ? `<li>Roster limit: you'd drop ${esc(t.auto_dropped.join(", "))}</li>` : ""}</ul>
        <div class="actions"><button class="btn small" data-tf="${i}">Open in the evaluator below</button></div>
      </div>`;
    }).join("");
  bindPlayerLinks(out);
  $$("[data-tf]", out).forEach((b) => b.addEventListener("click", () => {
    const t = R.trades[+b.dataset.tf];
    trade.give = t.give.map((x) => ({ id: x.nba_id, name: x.name }));
    trade.get = t.get.map((x) => ({ id: x.nba_id, name: x.name }));
    renderPicked(); evalTrade();
    $("#trade-result").scrollIntoView({ behavior: "smooth" });
  }));
}

/* ------------------------------------------------------------------ free-agent list */
function renderFaStatus(F) {
  $("#fa-sub").textContent = F.pasted
    ? `${F.players} players listed, ${F.on_waivers} on waivers · pasted ${ago(F.created)}${F.fresh ? "" : " (stale: re-paste)"}`
    : "not pasted yet: availability is inferred from rosters";
}

async function importFa(append) {
  const st = $("#fa-status"), text = $("#fa-text").value;
  if (text.length < 50) { st.textContent = "Paste the Yahoo page first."; return; }
  st.textContent = "Importing…";
  const r = await fetch("/api/free-agents", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, append }) });
  const d = await r.json();
  if (!r.ok) { st.textContent = d.detail || "Import failed"; return; }
  const dup = d.appended && d.new === 0;
  const bits = [dup
    ? `All ${d.added_now} players were already on the list: that's the same page again. In Yahoo, click Next 25, wait for the names to change, then copy.`
    : `${d.added_now} players read, ${d.new} new ${d.appended ? "added to the list" : "(new list)"}: ${d.players} players now, ${d.on_waivers} on waivers. Next page: click Next 25 in Yahoo, copy, paste, Import page.`];
  if (d.removed_from_outdated_rosters.length) bits.push(`Removed from outdated pasted rosters: ${d.removed_from_outdated_rosters.join(", ")}.`);
  if (d.listed_but_on_your_roster.length) bits.push(`On your roster but listed as available (did you drop them?): ${d.listed_but_on_your_roster.join(", ")}.`);
  if (d.unmatched.length) bits.push(`Couldn't match: ${d.unmatched.join(", ")}.`);
  st.innerHTML = esc(bits.join(" "));
  $("#fa-text").value = "";
  renderFaStatus(await api("/api/free-agents"));
  afterManualChange();
}

/* ------------------------------------------------------------------ Yahoo transaction log */
async function importTxns() {
  const st = $("#txn-status"), text = $("#txn-text").value;
  if (text.length < 30) { st.textContent = "Paste the Yahoo transactions page first."; return; }
  st.textContent = "Importing…";
  const r = await fetch("/api/yahoo-log", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
  const d = await r.json();
  if (!r.ok) { st.textContent = d.detail || "Import failed"; return; }
  const part = (label, xs) => xs && xs.length ? `<div><b>${label}</b> ${esc(xs.join("; "))}</div>` : "";
  st.innerHTML = `<div style="line-height:1.6">`
    + part("Rosters updated:", d.applied)
    + part("Your moves added to History:", d.your_moves_logged)
    + part("Already up to date:", d.already_reflected)
    + (d.already_seen ? `<div>${d.already_seen} imported before, skipped.</div>` : "")
    + part("Skipped:", d.skipped)
    + part("Not understood (send these to Claude):", d.unrecognized)
    + (!d.applied.length && !d.your_moves_logged.length ? "<div>Nothing new: everything was already up to date.</div>" : "")
    + `</div>`;
  $("#txn-text").value = "";
  loadHistory(); afterManualChange();
}

/* ------------------------------------------------------------------ the model's own team */
async function loadModelTeam() {
  const M = await api("/api/model-team");
  const el = $("#mt-body");
  if (!M.started) { el.innerHTML = '<span class="muted small">Not started.</span>'; return; }
  const r = M.record;
  $("#mt-sub").textContent = `started ${shortDate(M.started)} from your roster · manages itself: adds/drops, IL, lineups (no trades) · max 4 adds/week`;
  const weeks = M.weeks.map((w) => `<tr><td>${esc(shortDate(w.week_start))}${w.final ? "" : ' <span class="muted small">(in progress)</span>'}</td>
    <td>${esc(w.opp_name || "?")}</td><td class="r num">${fmt(w.model, 0)}</td><td class="r num">${fmt(w.you, 0)}</td><td class="r num">${w.opp == null ? "?" : fmt(w.opp, 0)}</td>
    <td class="r num ${w.model >= w.you ? "delta-up" : "delta-down"}">${(w.model - w.you >= 0 ? "+" : "") + fmt(w.model - w.you, 0)}</td></tr>`).join("");
  const moves = M.moves.slice(0, 12).map((m) => `<tr><td>${esc(shortDate(m.day))}</td><td>${esc(m.kind)}${m.status === "pending" ? ` <span class="muted small">(clears ${esc(shortDate(m.until))})</span>` : m.status === "failed" ? ' <span class="delta-down small">(failed)</span>' : ""}</td>
    <td>${m.add_name ? "+" + esc(m.add_name) : ""}${m.drop_name ? " / −" + esc(m.drop_name) : ""}${m.reason && !m.add_name ? esc(m.reason) : ""}</td>
    <td class="small">${m.add_pts_since != null ? `${fmt(m.add_pts_since, 0)} pts since` + (m.drop_pts_since != null ? ` vs ${fmt(m.drop_pts_since, 0)} for ${esc(m.drop_name)}` : "") : ""}</td>
    <td class="small">${m.add_name ? (m.you_too ? "you did too" : '<span class="delta-down">you didn\'t</span>') : ""}</td></tr>`).join("");
  el.innerHTML = `
    <div style="display:flex;gap:24px;flex-wrap:wrap;margin-bottom:10px">
      <div><div class="small muted">Model team record</div><div class="num" style="font-size:1.4em">${r.model[0]}–${r.model[1]}</div></div>
      <div><div class="small muted">Your record (same opponents)</div><div class="num" style="font-size:1.4em">${r.you[0]}–${r.you[1]}</div></div>
      <div style="flex:1;min-width:220px"><div class="small muted">Its roster now</div><div class="small">${esc(M.roster.join(", "))}${M.il.length ? ` · IL: ${esc(M.il.join(", "))}` : ""}${M.pending.length ? ` · claims: ${esc(M.pending.map((c) => c.add).join(", "))}` : ""}</div></div>
    </div>
    ${weeks ? `<div class="tbl-wrap"><table><thead><tr><th>Week of</th><th>Opponent</th><th class="r">Model</th><th class="r">You</th><th class="r">Opponent</th><th class="r">Model − you</th></tr></thead><tbody>${weeks}</tbody></table></div>` : '<div class="small muted">No finished days yet: the first scores appear the morning after its first games.</div>'}
    ${moves ? `<h3 style="margin:14px 0 6px;font-size:1em">Its moves</h3><div class="tbl-wrap"><table><thead><tr><th>Day</th><th>Move</th><th>Players</th><th>Since then</th><th></th></tr></thead><tbody>${moves}</tbody></table></div>` : ""}
    <div class="small muted" style="margin-top:8px">A scoreboard, not something the model learns from. Its paper claims always succeed and it never trades; your score uses your recorded lineups (or the best lineup by projection on days you didn't record one). One season of one team is noisy: read the record as a rough signal.</div>`;
}
