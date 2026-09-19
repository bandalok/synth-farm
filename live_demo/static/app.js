/* Synth Farm LIVE frontend — talks to the real running simulation. */
const $ = (id) => document.getElementById(id);
const CLUSTER_COLORS = ["#6edcaa", "#f2b544", "#7bb8f2", "#c792ea", "#e56b6b", "#8bd450"];
const state = {
  status: null, agents: [], homeAgent: null, homeData: null,
  selectedAgent: null, journey: null, journeyTimer: null, showTrails: true,
};

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
  });
});

/* ---------- controls ---------- */
$("btn-play").addEventListener("click", async () => {
  const running = state.status && state.status.running;
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: running ? "pause" : "play" }) });
  refreshStatus();
});
$("btn-step").addEventListener("click", async () => {
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: "step" }) });
  refreshAll();
});
$("btn-reset").addEventListener("click", async () => {
  if (!confirm("Reset the simulation to day 0?")) return;
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: "reset" }) });
  state.homeData = null; state.selectedAgent = null;
  $("agent-detail").style.display = "none";
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
        if (m.feed && m.feed.length) {
          const html = m.feed.map((e) =>
            `<span class="ev"><b>${e.archetype.replace(/_/g, " ")}</b> ${e.type === "search" ? "searched" : "played"} <span class="t">${esc(e.title)}</span> <span style="opacity:.6">(${e.genre})</span></span>`
          ).join("");
          $("ticker").innerHTML = html;
        }
        if ($("tab-journey").classList.contains("active")) fetchJourney();
      } else if (m.kind === "click") {
        logEvent(`<span class="e-click">click</span> ${esc(m.title)} <span style="opacity:.6">(${m.genre})</span>`, true);
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
    c.addEventListener("click", () => selectAgent(c.dataset.id)));
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
async function selectAgent(id) {
  document.querySelectorAll("#agent-cards .card").forEach((c) =>
    c.classList.toggle("selected", c.dataset.id === id));
  const a = await api("/api/agent?id=" + encodeURIComponent(id));
  state.selectedAgent = a;
  const d = $("agent-detail");
  d.style.display = "block";
  d.innerHTML = `
    <h3>${esc(a.archetype_pretty)} · <span style="color:var(--dim)">${a.id}</span></h3>
    <div class="kv">
      <div><b>Emergent cluster</b><span style="color:${CLUSTER_COLORS[a.cluster % 6]};font-weight:700">Cluster ${a.cluster}</span></div>
      <div><b>Top genre now</b>${esc(a.top_genre)}</div>
      <div><b>Sessions / week</b>${a.sessions_per_week}</div>
      <div><b>Search propensity</b>${a.search_propensity}</div>
      <div><b>Clickiness</b>${a.clickiness}</div>
      <div><b>Completion</b>${a.completion_propensity}</div>
      <div><b>Events recorded</b>${a.n_events}</div>
      <div><b>Apps</b>${a.subscribed_apps.slice(0, 4).join(", ")}</div>
    </div>
    <h3 style="font-size:14px;margin:12px 0 6px">Live taste vector</h3>
    ${tasteBars(a.taste)}
    <h3 style="font-size:14px;margin:12px 0 6px">Recent plays</h3>
    <div class="play-list">${a.recent_plays.map((p) =>
      `<b>${esc(p.title)}</b> (${esc(p.genre)}, day ${p.day})`).join("<br>") || "—"}</div>`;
}
function tasteBars(taste) {
  const entries = Object.entries(taste).sort((a, b) => b[1] - a[1]);
  return entries.map(([g, w]) => `
    <div class="bar-row"><div class="lbl">${esc(g)}</div>
    <div class="bar"><div class="fill" style="width:${Math.round(w * 100)}%"></div></div>
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
  const a = state.homeData, hs = a.home_screen;
  const rails = [
    ["Personalized for you", `<span class="why">ranked live against this agent's taste</span>`, hs.personalized],
    [`Because of your interest in ${hs.interest.genre}`, "", hs.interest.items],
  ];
  if (hs.because_watched.title)
    rails.push([`Because you watched ${hs.because_watched.title}`, "", hs.because_watched.items]);
  rails.push(["Trending now", `<span class="why">across the live simulation</span>`, hs.trending]);
  rails.push(["Continue watching", "", hs.continue]);
  $("rails").innerHTML = rails.map(([t, why, items]) => `
    <div class="rail"><h3>${esc(t)} ${why}</h3>
    <div class="tiles">${items.map(tileHTML).join("") || '<span class="hint">empty</span>'}</div></div>`).join("");
  document.querySelectorAll("#rails .tile").forEach((el) =>
    el.addEventListener("click", () => clickTile(el.dataset.id)));
  $("taste-bars").innerHTML = tasteBars(a.taste);
  $("home-hint").innerHTML =
    `Showing <b>${esc(a.archetype_pretty)}</b> (${a.id}) — day ${state.status.day}, cluster ${a.cluster}. Click any tile.`;
}
async function clickTile(itemId) {
  const a = await api("/api/click", { method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ agent_id: state.homeAgent, item_id: itemId }) });
  state.homeData = a;
  const last = a.recent_plays[0];
  logEvent(`<span class="e-play">▶ play</span> <b>${esc(last.title)}</b> <span style="opacity:.6">(${last.genre})</span> → taste updated`, true);
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
  setInterval(() => { if (!$("tab-journey").classList.contains("active")) refreshStatus(); }, 3000);
})();
