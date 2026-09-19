/* Synth Farm LIVE frontend — talks to the real running simulation. */
const $ = (id) => document.getElementById(id);
const CLUSTER_COLORS = ["#6edcaa", "#f2b544", "#7bb8f2", "#c792ea", "#e56b6b", "#8bd450"];
const state = {
  status: null, agents: [], homeAgent: null, homeData: null, lastTickDay: 0,
  detailId: null, detailAgent: null, journey: null, journeyTimer: null, showTrails: true,
};

function genreColor(g) {
  let h = 0;
  for (const c of g) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h}, 62%, 52%)`;
}
const GPRETTY = {"sci-fi": "Sci-Fi", "k-drama": "K-Drama", "true-crime": "True Crime",
  "reality-tv": "Reality TV", "romcom": "Rom-Com"};
function gpretty(g) {
  return GPRETTY[g] || String(g).replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

/* ---------- tabs ---------- */
document.querySelectorAll("nav.tabs button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("nav.tabs button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "journey") startJourney(); else stopJourney();
    if (b.dataset.tab === "home") loadHome();
  });
});

/* ---------- controls ---------- */
async function togglePlay() {
  const running = state.status && state.status.running;
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: running ? "pause" : "play" }) });
  refreshStatus();
}
$("btn-play").addEventListener("click", togglePlay);
$("day-counter").addEventListener("click", togglePlay);
$("btn-step").addEventListener("click", async () => {
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: "step" }) });
  refreshAll();
});
$("btn-reset").addEventListener("click", async () => {
  if (!confirm("Reset the simulation to day 0?")) return;
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: "reset" }) });
  state.homeData = null; state.detailId = null; state.detailAgent = null;
  closeAgentPage();
  refreshAll();
});
$("sel-speed").addEventListener("change", async (e) => {
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: "speed", tick_seconds: parseFloat(e.target.value) }) });
});

/* ---------- live event ticker (SSE) ---------- */
function connectStream() {
  const es = new EventSource("/api/stream");
  es.onmessage = (ev) => {
    try {
      const m = JSON.parse(ev.data);
      if (m.kind === "tick") {
        refreshStatus();
        if (m.day !== state.lastTickDay) {
          state.lastTickDay = m.day;
          if (state.homeAgent && $("tab-home").classList.contains("active")) loadHome();
        }
        if (state.detailId) refreshDetail();
        if (m.feed && m.feed.length) {
          const html = m.feed.map((e) =>
            `<span class="ev"><b>${e.archetype.replace(/_/g, " ")}</b> ${e.type === "search" ? "searched" : "played"} <span class="t">${esc(e.title)}</span> <span style="opacity:.6">(${esc(gpretty(e.genre))})</span></span>`
          ).join("");
          $("ticker").innerHTML = html;
        }
        if ($("tab-journey").classList.contains("active")) fetchJourney();
      } else if (m.kind === "click") {
        logEvent(`<span class="e-click">click</span> ${esc(m.title)} <span style="opacity:.6">(${esc(gpretty(m.genre))})</span>`, true);
        if (state.detailId) refreshDetail();
      }
    } catch (e) { /* ignore */ }
  };
  es.onerror = () => setTimeout(connectStream, 3000);
}
function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

/* ---------- status / clusters ---------- */
async function refreshStatus() {
  state.status = await api("/api/status");
  $("day-num").textContent = state.status.day;
  $("btn-play").textContent = state.status.running ? "⏸ Pause" : "▶ Play";
  renderClusters();
}
function renderClusters() {
  const cl = state.status.clusters;
  $("cluster-cards").innerHTML = cl.map((c, i) => `
    <div class="card" style="border-top: 3px solid ${CLUSTER_COLORS[i % 6]}">
      <h3>${esc(c.name)}</h3>
      <div class="meta">${c.size} agents · anchored in ${esc(c.top_genre)}</div>
      <div class="bar-row" style="margin-top:10px"><div class="bar"><div class="fill" style="width:${Math.round(100 * c.size / state.status.n_agents)}%;background:${CLUSTER_COLORS[i % 6]}"></div></div><div class="val">${c.size}</div></div>
    </div>`).join("");
}

/* ---------- agents ---------- */
async function refreshAgents() {
  const d = await api("/api/agents");
  state.agents = d.agents;
  $("agent-cards").innerHTML = d.agents.map((a) => `
    <div class="card" data-id="${a.id}">
      <h3>${esc(a.archetype_pretty)}</h3>
      <div class="meta">${a.id} · cluster <b style="color:${CLUSTER_COLORS[a.cluster % 6]}">${a.cluster}</b></div>
      <div class="meta">into <b>${esc(a.top_genre)}</b> · ${a.n_plays} plays</div>
    </div>`).join("");
  document.querySelectorAll("#agent-cards .card").forEach((c) =>
    c.addEventListener("click", () => openAgentPage(c.dataset.id)));
  const pick = $("home-agent-pick");
  const cur = pick.value;
  pick.innerHTML = d.agents.map((a) =>
    `<option value="${a.id}">${esc(a.archetype_pretty)} — ${a.id}</option>`).join("");
  if (cur) pick.value = cur;
  if (!state.homeAgent && d.agents.length) {
    state.homeAgent = d.agents[0].id;
    pick.value = state.homeAgent;
    loadHome();
  }
}
/* ---------- agent data-model page ---------- */
async function openAgentPage(id) {
  state.detailId = id;
  await refreshDetail();
  $("agent-page").style.display = "block";
}
function closeAgentPage() {
  $("agent-page").style.display = "none";
  state.detailId = null;
  state.detailAgent = null;
}
async function refreshDetail() {
  if (!state.detailId) return;
  try {
    const a = await api("/api/agent?id=" + encodeURIComponent(state.detailId));
    state.detailAgent = a;
    const pg = $("agent-page");
    const st = pg.scrollTop;
    renderAgentPage(a);
    pg.scrollTop = st;
  } catch (e) { /* sim may be resetting */ }
}
function renderAgentPage(a) {
  const p = a.profile, ec = a.event_counts, lu = a.last_taste_update;
  const delta = lu ? (lu.after - lu.before) * 100 : 0;
  const luHtml = lu ? `<div class="update-box">
      <b>Day ${lu.day}</b> · ${lu.kind === "search" ? "searched for" : "watched"}
      <b>${esc(lu.title)}</b> →
      <b style="color:${genreColor(lu.genre)}">${esc(gpretty(lu.genre))}</b>
      moved <b>${(lu.before * 100).toFixed(1)}% → ${(lu.after * 100).toFixed(1)}%</b>
      <span class="delta">(${delta >= 0 ? "+" : ""}${delta.toFixed(2)} pts)</span>
    </div>`
    : `<div class="hint">No taste updates yet — press Play and let this agent watch something.</div>`;
  $("agent-page-body").innerHTML = `
  <div class="ap-head">
    <button class="back-btn" id="ap-back">← All agents</button>
    <h2>${esc(a.archetype_pretty)} <span class="dim">· ${a.id}</span></h2>
    <span class="chip" style="border-color:${CLUSTER_COLORS[a.cluster % 6]};color:${CLUSTER_COLORS[a.cluster % 6]}">Cluster ${a.cluster}</span>
    <span class="chip">leaning ${esc(gpretty(a.top_genre))}</span>
  </div>
  <p class="sub">This is the agent's <b>live data model</b> — everything the platform has
  collected or inferred about this viewer, updating in real time while the simulation runs.</p>
  <div class="ap-grid">
    <div class="card"><h3>1 · Identity — captured at signup</h3>
      <div class="kv">
        <div><b>Viewer id</b>${a.id}</div>
        <div><b>Archetype</b>${esc(a.archetype_pretty)}</div>
        <div><b>Age band</b>${p.age_band}</div>
        <div><b>Region</b>${p.region}</div>
        <div><b>Primary device</b>${p.primary_device}</div>
        <div><b>Household size</b>${p.household_size}</div>
        <div><b>Subscribed apps</b>${a.subscribed_apps.slice(0, 4).join(", ")}</div>
      </div></div>
    <div class="card"><h3>2 · Behavioral DNA — inferred from usage</h3>
      <div class="kv">
        <div><b>Sessions / week</b>${a.sessions_per_week}<span class="note">how often they show up</span></div>
        <div><b>Search propensity</b>${a.search_propensity}<span class="note">browse vs. search</span></div>
        <div><b>Clickiness</b>${a.clickiness}<span class="note">tile click-through rate</span></div>
        <div><b>Completion</b>${a.completion_propensity}<span class="note">finishes what they start</span></div>
        <div><b>Titles / session</b>${a.mean_units}<span class="note">binge depth</span></div>
      </div></div>
  </div>
  <h3 class="ap-sec">3 · Taste vector — learned, all Gracenote genres</h3>
  <p class="sub">Every play and search nudges these weights. They always sum to 100%.</p>
  <div class="card">${tasteBars(a.taste)}</div>
  <h3 class="ap-sec">4 · How data is collected</h3>
  <div class="pipe">
    <div class="step"><div class="n">1</div><b>Observe.</b><p>Every impression, click, play and
      search is logged with day, title, genre and query. <b>${a.n_events} events</b> so far for this viewer.</p></div>
    <div class="step"><div class="n">2</div><b>Learn.</b><p>Each event nudges the Gracenote-genre taste
      vector: <span class="mono">new = 0.88 × old + 0.12 × title</span>. Latest update:</p>${luHtml}</div>
    <div class="step"><div class="n">3</div><b>Cluster.</b><p>Every simulated day, k-means re-fits
      ${state.status && state.status.n_clusters ? state.status.n_clusters : ""} clusters over all ${state.status ? state.status.n_agents : ""} live taste vectors.
      This viewer sits in <b>cluster ${a.cluster}</b>.</p></div>
    <div class="step"><div class="n">4</div><b>Rank.</b><p><b>${a.home_screen.rails.length} collections</b>
      are re-scored against the fresh vector — the Home Screen tab is this step, executing live.</p></div>
  </div>
  <table class="tax">
    <tr><th>Event</th><th>How it's captured</th><th>Fields stored</th><th>Count</th></tr>
    <tr><td><span class="e-imp">impression</span></td><td>tile rendered on screen</td><td class="mono">day · title · genre</td><td>${ec.impression}</td></tr>
    <tr><td><span class="e-click">click</span></td><td>tile tapped</td><td class="mono">day · title · genre</td><td>${ec.click}</td></tr>
    <tr><td><span class="e-play">play</span></td><td>watch started</td><td class="mono">day · title · genre</td><td>${ec.play}</td></tr>
    <tr><td><span class="e-search">search</span></td><td>query submitted</td><td class="mono">day · query · genre · title picked</td><td>${ec.search}</td></tr>
  </table>
  <h3 class="ap-sec">Live event stream</h3>
  <div class="ev-stream">${a.recent_events.map((e) => `
    <div>[day ${e.day}] <span class="e-${e.type}">${e.type}</span> <b>${esc(e.title)}</b>
    <span class="dim">(${esc(gpretty(e.genre))})</span>${e.query ? ` <span class="dim">query: &ldquo;${esc(e.query)}&rdquo;</span>` : ""}</div>`).join("")
    || '<span class="hint">nothing recorded yet</span>'}</div>`;
  $("ap-back").addEventListener("click", closeAgentPage);
}
function tasteBars(taste) {
  const entries = Object.entries(taste).sort((a, b) => b[1] - a[1]);
  return entries.map(([g, w]) => `
    <div class="bar-row"><div class="lbl">${esc(gpretty(g))}</div>
    <div class="bar"><div class="fill" style="width:${Math.round(w * 100)}%;background:${genreColor(g)}"></div></div>
    <div class="val">${Math.round(w * 100)}%</div></div>`).join("");
}

/* ---------- home screen ---------- */
$("home-agent-pick").addEventListener("change", (e) => {
  state.homeAgent = e.target.value;
  loadHome();
});
async function loadHome() {
  if (!state.homeAgent) return;
  const a = await api("/api/agent?id=" + encodeURIComponent(state.homeAgent));
  state.homeData = a;
  renderHome();
}
function tileHTML(t) {
  const prov = (t.providers || []).slice(0, 2).join(" · ");
  return `<div class="tile" data-id="${t.id}" title="${esc(t.title)}">
    ${t.poster ? `<img loading="lazy" src="${t.poster}" alt="">` : `<div style="width:150px;height:225px;background:#000"></div>`}
    <div class="ti"><b>${esc(t.title)}</b><span>${esc(t.genre)}</span>
    ${prov ? `<span class="prov">${esc(prov)}</span>` : ""}</div></div>`;
}
function renderHome() {
  const a = state.homeData;
  $("rails").innerHTML = a.home_screen.rails.map((r) => `
    <div class="rail"><h3>${esc(r.title)}${r.why ? ` <span class="why">${esc(r.why)}</span>` : ""}</h3>
    <div class="tiles">${r.items.map(tileHTML).join("")}</div></div>`).join("");
  document.querySelectorAll("#rails .tile").forEach((el) =>
    el.addEventListener("click", () => clickTile(el.dataset.id)));
  $("taste-bars").innerHTML = tasteBars(a.taste);
  $("home-hint").innerHTML =
    `Showing <b>${esc(a.archetype_pretty)}</b> (${a.id}) — day ${state.status.day}, cluster ${a.cluster}, ${a.home_screen.rails.length} collections. Click any tile.`;
}
async function clickTile(itemId) {
  const a = await api("/api/click", { method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ agent_id: state.homeAgent, item_id: itemId }) });
  state.homeData = a;
  const last = a.recent_plays[0];
  logEvent(`<span class="e-play">▶ play</span> <b>${esc(last.title)}</b> <span style="opacity:.6">(${esc(gpretty(last.genre))})</span> → taste updated`, true);
  renderHome();
}
function logEvent(html, prepend) {
  const el = $("eventlog");
  const div = document.createElement("div");
  div.innerHTML = `[day ${state.status ? state.status.day : 0}] ${html}`;
  if (prepend && el.firstChild) el.insertBefore(div, el.firstChild);
  else el.appendChild(div);
}
/* search */
let searchTimer = null;
$("search-box").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value;
  searchTimer = setTimeout(async () => {
    if (!q.trim()) { $("search-results").style.display = "none"; return; }
    const d = await api("/api/search?q=" + encodeURIComponent(q) +
      (state.homeAgent ? "&agent=" + encodeURIComponent(state.homeAgent) : ""));
    $("search-results").style.display = "block";
    $("search-tiles").innerHTML = d.results.map(tileHTML).join("") || '<span class="hint">no matches</span>';
    document.querySelectorAll("#search-tiles .tile").forEach((el) =>
      el.addEventListener("click", async () => {
        logEvent(`<span class="e-search">⌕ search</span> "${esc(q)}" → <b>${esc(el.title)}</b>`, true);
        await clickTile(el.dataset.id);
      }));
  }, 220);
});

/* ---------- journey ---------- */
function startJourney() {
  fetchJourney();
  stopJourney();
  state.journeyTimer = setInterval(fetchJourney, 2000);
}
function stopJourney() {
  if (state.journeyTimer) clearInterval(state.journeyTimer);
  state.journeyTimer = null;
}
async function fetchJourney() {
  state.journey = await api("/api/journey");
  drawJourney();
}
function drawJourney() {
  const cv = $("journey-canvas"), ctx = cv.getContext("2d");
  const W = cv.clientWidth, H = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  cv.width = W * dpr; cv.height = H * dpr; ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const j = state.journey;
  if (!j) return;
  const day = j.day, n = j.paths.length;
  const px = (x) => 40 + x * (W - 80), py = (y) => 30 + y * (H - 60);
  // trails
  if (state.showTrails && day > 0) {
    ctx.lineWidth = 1; ctx.globalAlpha = 0.25;
    for (let i = 0; i < n; i++) {
      const p = j.paths[i], c = CLUSTER_COLORS[j.labels[i] % 6];
      ctx.strokeStyle = c; ctx.beginPath();
      const upto = Math.min(day, p.length - 1);
      ctx.moveTo(px(p[0][0]), py(p[0][1]));
      for (let d = 1; d <= upto; d++) ctx.lineTo(px(p[d][0]), py(p[d][1]));
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }
  // dots
  for (let i = 0; i < n; i++) {
    const p = j.paths[i][Math.min(day, j.paths[i].length - 1)];
    ctx.fillStyle = CLUSTER_COLORS[j.labels[i] % 6];
    ctx.beginPath(); ctx.arc(px(p[0]), py(p[1]), 3.2, 0, 7); ctx.fill();
  }
  // cluster labels
  ctx.font = "12px sans-serif";
  j.clusters.forEach((c, k) => {
    const members = j.paths.filter((_, i) => j.labels[i] === k);
    if (!members.length) return;
    const d0 = Math.min(day, members[0].length - 1);
    let sx = 0, sy = 0;
    members.forEach((p) => { sx += p[d0][0]; sy += p[d0][1]; });
    ctx.fillStyle = CLUSTER_COLORS[k % 6];
    ctx.fillText(`${c.name} · ${c.size}`, px(sx / members.length) + 8, py(sy / members.length) - 8);
  });
  // legend
  $("journey-legend").innerHTML = j.clusters.map((c, k) => `
    <span class="lg"><span class="sw" style="background:${CLUSTER_COLORS[k % 6]}"></span>
    ${esc(c.name)} <span class="ct">(${c.size})</span></span>`).join("") +
    `<span class="lg ct">day ${day} · ${n} agents</span>
     <span class="lg"><label style="cursor:pointer"><input type="checkbox" id="trails-cb" ${state.showTrails ? "checked" : ""}> trails</label></span>`;
  const cb = $("trails-cb");
  if (cb) cb.addEventListener("change", (e) => { state.showTrails = e.target.checked; drawJourney(); });
}

/* ---------- boot ---------- */
async function refreshAll() {
  await refreshStatus();
  await refreshAgents();
  if (state.homeAgent) loadHome();
  if ($("tab-journey").classList.contains("active")) fetchJourney();
}
(async function boot() {
  connectStream();
  await refreshAll();
  setInterval(() => {
    if (!$("tab-journey").classList.contains("active")) refreshStatus();
    if (state.detailId) refreshDetail();
  }, 3000);
})();
