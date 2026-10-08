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
    if (b.dataset.tab === "collections") loadCollections();
    if (b.dataset.tab === "agents") {
      refreshAgents();
      // replay a recent energy blast if the user opens the tab mid-show
      if (performance.now() - masterEnergyStart < 8000) fireMasterEnergy();
    }
    if (b.dataset.tab === "master" && state.status) {
      renderCampaigns(state.status.campaigns || []);
      renderRecommendation(state.status.recommendation || null);
      renderCampaignTimeline();
      renderMasterLog(state.status.master_log || []);
      refreshMasterSections();
    }
  });
});
$("master-go").addEventListener("click", sendMaster);
$("camp-timeline-toggle").addEventListener("change", renderCampaignTimeline);
$("master-input").addEventListener("keydown", (e) => { if (e.key === "Enter") sendMaster(); });
$("explainer-x").addEventListener("click", () => { $("explainer").hidden = true; });

/* ---------- controls ---------- */
async function togglePlay() {
  const running = state.status && state.status.running;
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: running ? "pause" : "play" }) });
  refreshStatus();
}
$("btn-play").addEventListener("click", togglePlay);
$("day-counter").addEventListener("click", togglePlay);
$("btn-step-back").addEventListener("click", async () => {
  await api("/api/control", { method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ action: "step_back" }) });
  refreshAll();
});
$("btn-step-fwd").addEventListener("click", async () => {
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
      } else if (m.kind === "directed") {
        $("ticker").innerHTML = `<span class="ev">day ${m.day} ${esc(m.text)}</span>`;
        if ($("tab-master").classList.contains("active")) refreshStatus();
        if (m.n_targets > 0) {
          lastDirectedTargets = m.targets || [];
          fireMasterEnergy();
          showMasterBlast(m.day, m.text, m.n_targets);
        }
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
  // Day-stepping arrows only work while paused; ◀ also needs history.
  const paused = !state.status.running;
  $("btn-step-back").disabled = !(paused && state.status.can_step_back);
  $("btn-step-fwd").disabled = !paused;
  renderClusters();
  if ($("tab-master").classList.contains("active")) {
    renderCampaigns(state.status.campaigns || []);
    renderRecommendation(state.status.recommendation || null);
    renderCampaignTimeline();
    renderMasterLog(state.status.master_log || []);
    renderCampaignAnalytics();
    renderSponsorROI();
  }
  if ($("tab-agents").classList.contains("active")) {
    drawMasterPanel();
    // re-render the cards the moment campaign targeting changes (fire/expire)
    const sig = JSON.stringify((state.status.campaigns || [])
      .map((c) => [c.id, (c.targets || []).slice().sort((x, y) => x - y)]));
    if (sig !== state.targetSig) await refreshAgents();
  }
}
/* ---------- master agent ---------- */
const MASTER_SUGGESTIONS = [
  ["⚾", "Baseball blast", "pivot all users to baseball for 7 days"],
  ["🎃", "Horror nights", "pivot some users to horror for 3 days"],
  ["🚀", "Sci-fi weekend", "pivot half the users to sci-fi for 4 days"],
  ["💘", "Romance week", "pivot 30% of users to romance for 7 days"],
  ["🤣", "Comedy · cluster 2", "pivot cluster 2 to comedy for 7 days"],
];
function renderMasterSuggestions() {
  $("master-sugg").innerHTML = MASTER_SUGGESTIONS.map(([emo, label, text], i) =>
    `<button class="sugg-chip" data-i="${i}">${emo} ${esc(label)}</button>`).join("");
  $("master-sugg").querySelectorAll(".sugg-chip").forEach((b) =>
    b.addEventListener("click", () => {
      $("master-input").value = MASTER_SUGGESTIONS[+b.dataset.i][2];
      $("master-input").focus();
    }));
}
renderMasterSuggestions();
async function sendMaster() {
  const inp = $("master-input");
  const text = inp.value.trim();
  if (!text) return;
  const r = await api("/api/direct", { method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ text }) });
  $("master-msg").innerHTML = esc(r.message);
  renderCampaigns(r.campaigns || []);
  if (state.status) state.status.campaigns = r.campaigns || [];
  renderCampaignTimeline();
  renderMasterLog(r.log || []);
  inp.value = "";
}
/* user-facing campaign label: internal genre "Sports" displays as Baseball */
const campLabel = (g) => g === "Sports" ? "Baseball" : g;
function renderCampaigns(cs) {
  $("campaigns").innerHTML = cs.length ? cs.map((c) => {
    const strength = Math.round(100 * c.days_left / Math.max(c.days_total, 1));
    const sched = c.status === "scheduled";
    /* label by type: genre pivots by genre, promos by provider, title
       campaigns by their pinned titles, takeovers by headline */
    let label;
    if (c.campaign_type === "takeover") {
      label = `📺 Takeover: ${esc(c.headline || c.title_id || "")}`;
    } else if (c.campaign_type === "featured") {
      label = `⭐ Featured row (${(c.titles || []).length} titles)`;
    } else if (c.campaign_type === "search_boost") {
      label = `🔍 Search boost (${(c.titles || []).length} titles)`;
    } else if (c.campaign_type === "provider_promo") {
      const places = (c.placements || []).join(", ");
      label = `📱 ${esc(c.provider || "")}${places ? ` <span class="dim">on ${esc(places)}</span>` : ""}`;
    } else if (c.campaign_type === "elections") {
      label = `🗳️ Elections coverage (${(c.titles || []).length} titles)`;
    } else {
      label = esc((c.genres && c.genres.length)
        ? c.genres.map(campLabel).join(" + ") : (c.provider || ""));
    }
    return `
    <div class="card" style="border-top:3px solid ${sched ? "var(--teal)" : "var(--amber)"}">
      <h3>${label}${sched ? " <span class='pin'>📅 scheduled</span>" : ""}</h3>
      <div class="meta">${c.n_targets} agents · ${sched ? `<b>starts day ${c.start_day}</b>` : `<b>${c.days_left}</b> days left`}</div>
      <div class="bar-row" style="margin-top:8px"><div class="bar"><div class="fill" style="width:${strength}%;background:${sched ? "var(--teal)" : "var(--amber)"}"></div></div><div class="val">${strength}%</div></div>
      <div class="meta" style="opacity:.7">“${esc(c.text)}”</div>
      <div style="margin-top:8px"><button class="coll-btn danger" data-stop-camp="${c.id}">Stop campaign</button></div>
    </div>`;
  }).join("") : `<p class="sub">No live campaigns.</p>`;
  $("campaigns").querySelectorAll("button[data-stop-camp]").forEach((b) =>
    b.addEventListener("click", async () => {
      let r; try { r = await api("/api/stop_campaign", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: parseInt(b.dataset.stopCamp, 10) }) }); }
      catch (e) { return; }
      if (r.campaigns) { renderCampaigns(r.campaigns);
        if (state.status) state.status.campaigns = r.campaigns; }
    }));
}
/* ---------- recommended campaign ---------- */
function renderRecommendation(rec) {
  const box = $("recommendation");
  if (!rec) { box.innerHTML = ""; return; }
  if (rec.launchable) {
    box.innerHTML = `
    <div class="card" style="border-top:3px solid var(--amber)">
      <h3>${esc(rec.title)}</h3>
      <div class="meta">${esc(rec.description)}</div>
      <div style="margin-top:10px"><button class="go-btn" id="rec-launch">Launch</button></div>
    </div>`;
    /* reuse the existing start path: the button just feeds the Master
       Agent directive box and goes through sendMaster() -> /api/direct */
    $("rec-launch").addEventListener("click", () => {
      $("master-input").value = rec.directive;
      sendMaster().then(() => refreshStatus());
    });
  } else {
    const l = rec.live || {};
    box.innerHTML = `
    <div class="card" style="border-top:3px solid var(--amber)">
      <h3>${esc(rec.title)}</h3>
      <div class="meta"><span class="pin">● live</span> — ${l.n_targets} agents · ${l.days_left} days left</div>
    </div>`;
  }
}
function renderMasterLog(log) {
  $("master-log").innerHTML = log.slice().reverse().map((l) =>
    `<div class="ev"><span class="dim">day ${l.day}</span>${esc(l.text)}</div>`).join("");
}
/* ---------- master agent sub-sections ---------- */
$("ma-subnav").querySelectorAll("button").forEach((b) =>
  b.addEventListener("click", () => {
    $("ma-subnav").querySelectorAll("button").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    document.querySelectorAll(".ma-sec").forEach((s) => { s.hidden = true; });
    $("ma-" + b.dataset.sec).hidden = false;
  }));
/* reusable title picker: search -> chips */
function makeTitlePicker(elId, multi) {
  const el = $(elId);
  el.innerHTML = `
    <div class="search-row">
      <input type="text" placeholder="Search titles to pin…" autocomplete="off">
      <div class="picker-results"></div>
    </div>
    <div class="chip-row"></div>`;
  const input = el.querySelector("input");
  const results = el.querySelector(".picker-results");
  const chips = el.querySelector(".chip-row");
  const picked = [];
  const renderChips = () => {
    chips.innerHTML = picked.map((t, i) =>
      `<span class="chip">${esc(t.title)}<b data-i="${i}">×</b></span>`).join("");
    chips.querySelectorAll("b").forEach((x) => x.addEventListener("click", () => {
      picked.splice(+x.dataset.i, 1); renderChips();
    }));
  };
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = input.value.trim();
      if (q.length < 2) { results.innerHTML = ""; return; }
      let d; try { d = await api("/api/search?q=" + encodeURIComponent(q)); }
      catch (e) { return; }
      results.innerHTML = (d.results || []).slice(0, 8).map((t, i) =>
        `<button data-i="${i}">${esc(t.title)} <span class="dim">${esc(t.genre || "")}</span></button>`).join("");
      const list = d.results || [];
      results.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
        const t = list[+b.dataset.i];
        if (!picked.some((p) => p.id === t.id)) {
          if (!multi) picked.length = 0;
          picked.push(t); renderChips();
        }
        results.innerHTML = ""; input.value = ""; input.focus();
      }));
    }, 250);
  });
  return { picked, clear: () => { picked.length = 0; renderChips(); } };
}
const featPicker = makeTitlePicker("feat-picker", true);
const searchPicker = makeTitlePicker("search-picker", true);
const takeoverPicker = makeTitlePicker("takeover-picker", false);
async function launchCampaign(url, body, msgEl, picker) {
  const m = $(msgEl);
  m.textContent = "Launching…";
  let r; try { r = await api(url, { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) }); }
  catch (e) { m.textContent = "Launch failed."; return; }
  m.textContent = r.message || (r.ok ? "Launched." : "Launch failed.");
  if (r.ok && picker) picker.clear();
  if (r.campaigns) { renderCampaigns(r.campaigns);
    if (state.status) state.status.campaigns = r.campaigns; }
  renderCampaignTimeline();
  if (state.status) refreshStatus();
}
$("feat-go").addEventListener("click", () => launchCampaign("/api/featured_campaign",
  { titles: featPicker.picked.map((t) => t.id),
    coverage: parseFloat($("feat-coverage").value),
    days: parseInt($("feat-days").value, 10) || 7 }, "feat-msg", featPicker));
$("searchcamp-go").addEventListener("click", () => launchCampaign("/api/search_campaign",
  { titles: searchPicker.picked.map((t) => t.id),
    coverage: parseFloat($("searchcamp-coverage").value),
    days: parseInt($("searchcamp-days").value, 10) || 7 }, "searchcamp-msg", searchPicker));
$("takeover-go").addEventListener("click", () => launchCampaign("/api/takeover_campaign",
  { title: (takeoverPicker.picked[0] || {}).id || "",
    headline: $("takeover-headline").value,
    sponsor: $("takeover-sponsor").value,
    coverage: parseFloat($("takeover-coverage").value),
    days: parseInt($("takeover-days").value, 10) || 7 }, "takeover-msg", takeoverPicker));
/* ---------- collections management ---------- */
async function renderRailList() {
  const box = $("rail-list");
  let d; try { d = await api("/api/rail_config"); } catch (e) { return; }
  const hidden = new Set(d.hidden || []);
  const order = d.order && d.order.length ? d.order.slice()
    : d.rails.map((r) => r.kind);
  const byKind = Object.fromEntries(d.rails.map((r) => [r.kind, r.title]));
  const rows = order.filter((k) => byKind[k]).map((k) => ({ kind: k, title: byKind[k] }));
  for (const r of d.rails) if (!order.includes(r.kind)) rows.push(r);
  // fixed rows always lead, in order
  const FIXED_KINDS = ["featured", "apps", "continue"];
  rows.sort((a, b) => {
    const ai = FIXED_KINDS.indexOf(a.kind), bi = FIXED_KINDS.indexOf(b.kind);
    if (ai !== -1 || bi !== -1) return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    return 0;
  });
  const trends = d.trends || {};
  const pinned = d.pinned || {};
  box.innerHTML = rows.map((r, i) => {
    const isFixed = FIXED_KINDS.includes(r.kind);
    const pinPos = pinned[r.kind];
    const pos = isFixed ? `<span class="rail-pos">${FIXED_KINDS.indexOf(r.kind) + 1}</span>`
      : pinPos ? `<span class="rail-pos rail-pinned" title="Pinned at row ${pinPos}">#${pinPos}</span>`
      : `<span class="rail-pos rail-rotates" title="Rotates through the home screen">~</span>`;
    const moveBtns = isFixed
      ? `<span class="rail-fixed-tag">fixed</span>`
      : pinPos
      ? `<span class="rail-pinned-tag">pinned #${pinPos}</span>`
      : `<span class="rail-rotates-tag" title="This collection rotates through the home screen">rotates</span>`;
    let trend = "";
    if (!isFixed && trends[r.kind] !== undefined) {
      const v = trends[r.kind];
      if (v === "new") {
        trend = `<span class="rail-trend rail-new" title="Recently added — no performance history yet">NEW</span>`;
      } else {
        const cls = v > 0 ? "up" : v < 0 ? "down" : "flat";
        const arrow = v > 0 ? "▲" : v < 0 ? "▼" : "—";
        const sign = v > 0 ? "+" : "";
        trend = `<span class="rail-trend ${cls}" title="Plays vs prior 4 weeks">${arrow} ${sign}${v}% <span class="trend-4w">4w</span></span>`;
      }
    }
    const pinCtl = (!isFixed && !hidden.has(r.kind)) ? (pinPos
      ? `<button data-act="unpin" title="Unpin from row ${pinPos}">Unpin</button>`
      : `<span class="pin-ctl"><input type="number" min="4" max="30" placeholder="#" title="Row number (4+)"><button data-act="pin">Pin</button></span>`)
      : "";
    return `
    <div class="rail-row ${hidden.has(r.kind) ? "is-hidden" : ""}" data-kind="${r.kind}">
      ${pos}
      <span class="rail-name">${esc(r.title)}</span>
      ${trend}
      <span class="rail-kind">${esc(r.kind)}</span>
      ${moveBtns}
      ${pinCtl}
      <button data-act="hide">${hidden.has(r.kind) ? "Show" : "Hide"}</button>
    </div>`;
  }).join("");
  const save = async () => {
    const newOrder = [...box.querySelectorAll(".rail-row")].map((el) => el.dataset.kind);
    const newHidden = [...box.querySelectorAll(".rail-row.is-hidden")].map((el) => el.dataset.kind);
    const newPinned = {};
    box.querySelectorAll(".rail-row").forEach((el) => {
      const inp = el.querySelector(".pin-ctl input");
      if (inp && inp.value) {
        const pos = parseInt(inp.value, 10);
        if (pos >= 4) newPinned[el.dataset.kind] = pos;
      }
    });
    // preserve existing pins not being edited
    for (const [k, v] of Object.entries(pinned)) {
      if (!(k in newPinned)) newPinned[k] = v;
    }
    await api("/api/rail_config/update", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hidden: newHidden, order: newOrder, pinned: newPinned }) });
  };
  box.querySelectorAll("button").forEach((b) => b.addEventListener("click", async () => {
    const rowEl = b.closest(".rail-row");
    const act = b.dataset.act;
    if (act === "hide") rowEl.classList.toggle("is-hidden");
    else if (act === "up" && rowEl.previousElementSibling) rowEl.before(rowEl.previousElementSibling);
    else if (act === "down" && rowEl.nextElementSibling) rowEl.after(rowEl.nextElementSibling);
    else if (act === "pin") {
      const inp = rowEl.querySelector(".pin-ctl input");
      const pos = inp ? parseInt(inp.value, 10) : NaN;
      if (!(pos >= 4)) { inp.focus(); return; }
      await save(); renderRailList(); return;
    }
    else if (act === "unpin") {
      const kind = rowEl.dataset.kind;
      const d2 = await api("/api/rail_config");
      const cur = d2.pinned || {};
      delete cur[kind];
      const newHidden = [...box.querySelectorAll(".rail-row.is-hidden")].map((el) => el.dataset.kind);
      const newOrder = [...box.querySelectorAll(".rail-row")].map((el) => el.dataset.kind);
      await api("/api/rail_config/update", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hidden: newHidden, order: newOrder, pinned: cur }) });
      renderRailList(); return;
    }
    else return;
    await save(); renderRailList();
  }));
}
$("rail-reset").addEventListener("click", async () => {
  await api("/api/rail_config/update", { method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ hidden: [], order: [], pinned: {} }) });
  renderRailList();
});
/* ---------- AI recommended collections (seasonality) ---------- */
async function renderAiSeasonal() {
  const box = $("ai-seasonal-list");
  if (!box) return;
  let d; try { d = await api("/api/collections"); } catch (e) { return; }
  const cands = (d.ai_candidates || []).filter((c) => c.status === "pending");
  if (!cands.length) {
    box.innerHTML = `<p class="sub">No pending AI recommendations.</p>`;
    return;
  }
  box.innerHTML = `<table class="ai-table"><thead><tr>
    <th>Collection</th><th>Titles</th><th>AI recommendation</th><th>Action</th>
    </tr></thead><tbody>` + cands.map((c) => `
    <tr>
      <td><b>${esc(c.title)}</b>${c.hot ? ` <span class="hot-tag">🔥 highly recommended</span>` : ""}<br><span class="muted">${esc(c.description || "")}</span></td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${(c.title_count ?? 0).toLocaleString()}</td>
      <td>${esc(c.recommendation || "")}</td>
      <td><button class="coll-btn go" data-aid="${esc(c.id)}">Approve</button></td>
    </tr>`).join("") + `</tbody></table>`;
  box.querySelectorAll("button[data-aid]").forEach((b) => b.addEventListener("click", async () => {
    await api("/api/collections/approve", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: b.dataset.aid }) });
    renderAiSeasonal(); renderRailList();
  }));
}
/* ---------- apps campaign ---------- */
async function renderAppPicker() {
  const sel = $("appcamp-app");
  if (sel.options.length) return;
  let d; try { d = await api("/api/apps"); } catch (e) { return; }
  sel.innerHTML = (d.apps || []).map((a) => `<option>${esc(a)}</option>`).join("");
}
$("appcamp-go").addEventListener("click", () => {
  const placements = ["collection", "search", "featured"].filter(
    (p) => $("appcamp-place-" + p).checked);
  launchCampaign("/api/app_campaign",
    { provider: $("appcamp-app").value,
      coverage: parseFloat($("appcamp-coverage").value),
      days: parseInt($("appcamp-days").value, 10) || 7,
      placements }, "appcamp-msg");
});
/* ---------- calendar ---------- */
async function renderTentpoles() {
  const box = $("tentpole-list");
  let d; try { d = await api("/api/tentpoles"); } catch (e) { return; }
  box.innerHTML = (d.tentpoles || []).map((t, i) => `
    <div class="tp-row">
      <div><b>${esc(t.name)}</b> <span class="dim">${esc(t.date)}</span>
      <div class="sub">${esc(t.blurb)}</div></div>
      <button data-i="${i}">Plan campaign</button>
    </div>`).join("");
  box.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const t = d.tentpoles[+b.dataset.i];
    const isMidterms = /midterm|election/i.test(t.name);
    $("master-input").value = isMidterms
      ? `pivot all users to elections related content for 14 days`
      : `pivot all users to family for 14 days`;
    $("master-input").focus();
    $("master-msg").textContent = `Planning around ${t.name} (${t.date}) — edit the directive above, add "starting day N", then Command.`;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }));
}
function renderScheduledList() {
  const box = $("scheduled-list");
  const cs = (state.status && state.status.campaigns || []).filter((c) => c.status === "scheduled");
  box.innerHTML = cs.length ? cs.map((c) =>
    `<div class="ev"><span class="dim">starts day ${c.start_day}</span>${esc(c.text)}</div>`).join("")
    : `<p class="sub">None scheduled.</p>`;
}
/* lazy-load section data when the master tab opens */
function refreshMasterSections() {
  renderRailList(); renderAiSeasonal(); renderAppPicker(); renderTentpoles(); renderScheduledList();
}
/* ---------- campaign analytics (closed loop) ---------- */
async function renderCampaignAnalytics() {
  const box = $("campaign-analytics");
  let d;
  try { d = await api("/api/campaign_analytics"); }
  catch (e) { return; }
  const rows = d.analytics || [];
  if (!rows.length) {
    box.innerHTML = `<p class="sub">No campaigns yet — fire one above and it lands here.</p>`;
    return;
  }
  const liftCell = (v) => v == null ? `<span style="opacity:.5">—</span>`
    : `<span class="${v >= 0 ? "lift-pos" : "lift-neg"}">${v >= 0 ? "+" : ""}${Math.round(v * 100)}%</span>`;
  const shiftCell = (v) => v == null ? `<span style="opacity:.5">—</span>`
    : `<span class="${v >= 0 ? "lift-pos" : "lift-neg"}">${v >= 0 ? "+" : ""}${v}pp</span>`;
  const statusBadge = (s) => s === "live"
    ? `<span class="pin" style="color:var(--amber)">● live</span>`
    : s === "scheduled" ? `<span class="pin">📅 scheduled</span>` : `<span style="opacity:.6">ended</span>`;
  box.innerHTML = `<table class="analytics-table"><thead><tr>
    <th>Campaign</th><th>Status</th><th>Targets</th><th>Plays<br>(live window)</th>
    <th>Baseline</th><th>Lift</th><th>Taste shift</th><th>Clicks</th></tr></thead><tbody>` +
    rows.map((c) => `<tr>
      <td><b>${esc(c.genres.map(campLabel).join(" + "))}</b><br><span style="opacity:.65">${esc((c.text || "").slice(0, 60))}</span></td>
      <td>${statusBadge(c.status)}</td>
      <td>${c.n_targets}</td>
      <td>${c.plays_live}</td>
      <td>${c.plays_baseline}</td>
      <td>${liftCell(c.lift)}</td>
      <td>${shiftCell(c.taste_shift_pp)}</td>
      <td>${c.clicks}</td></tr>`).join("") +
    `</tbody></table>`;
}
/* ---------- sponsor ROI dashboard ---------- */
async function renderSponsorROI() {
  const box = $("sponsor-roi");
  let d;
  try { d = await api("/api/takeover_roi"); }
  catch (e) { return; }
  const rows = Array.isArray(d) ? d : [];
  if (!rows.length) {
    box.innerHTML = `<p class="sub">No takeover campaigns yet.</p>`;
    return;
  }
  const pct = (v) => v == null ? `<span style="opacity:.5">—</span>`
    : `${(v * 100).toFixed(1)}%`;
  const statusBadge = (s) => s === "live"
    ? `<span class="pin" style="color:var(--amber)">● live</span>`
    : s === "scheduled" ? `<span class="pin">📅 scheduled</span>` : `<span style="opacity:.6">ended</span>`;
  box.innerHTML = `<table class="analytics-table"><thead><tr>
    <th>Sponsor</th><th>Title</th><th>Status</th><th>Views</th>
    <th>Watches</th><th>CTR</th><th>Dismissals</th><th>Dismiss rate</th></tr></thead><tbody>` +
    rows.map((c) => `<tr>
      <td><b>${esc(c.sponsor || "—")}</b><br><span style="opacity:.65">${esc((c.headline || "").slice(0, 50))}</span></td>
      <td>${esc(c.title || c.title_id)}</td>
      <td>${statusBadge(c.status)}</td>
      <td>${c.views}</td>
      <td>${c.watches}</td>
      <td>${pct(c.ctr)}</td>
      <td>${c.dismissals}</td>
      <td>${pct(c.dismiss_rate)}</td></tr>`).join("") +
    `</tbody></table>`;
}
/* ---------- campaign timeline (checkbox-gated) ---------- */
function renderCampaignTimeline() {
  const box = $("camp-timeline");
  if ($("camp-timeline-toggle").checked) box.hidden = false;
  else { box.hidden = true; return; }
  const st = state.status || {};
  const day = st.day || 0;
  const rows = [];
  for (const c of (st.campaigns || []))
    rows.push({ genres: c.genres, n: c.n_targets, start: c.start_day,
                end: c.start_day + c.days_total, live: c.status === "live",
                left: c.days_left });
  for (const h of (st.campaign_history || []))
    rows.push({ genres: h.genres, n: h.n_targets, start: h.created_day,
                end: h.created_day + h.days_total, live: false });
  if (!rows.length) {
    box.innerHTML = `<p class="sub">No campaigns yet — fire one above and it lands here.</p>`;
    return;
  }
  rows.sort((a, b) => b.start - a.start || b.end - a.end);
  const lo = Math.min(day, ...rows.map((r) => r.start));
  const hi = Math.max(day + 1, ...rows.map((r) => r.end));
  const span = Math.max(hi - lo, 1);
  const pct = (d) => (100 * (d - lo) / span).toFixed(1);
  box.innerHTML = `
    <div class="tl-axis"><span>day ${lo}</span><span class="tl-today">▼ today · day ${day}</span><span>day ${hi}</span></div>
    ${rows.map((r) => {
      const l = pct(r.start), w = Math.max(pct(r.end) - pct(r.start), 1.5);
      const today = pct(day);
      const dot = r.live ? "🟢" : (r.start > day ? "📅" : "⚫");
      const sub = r.live ? ` · ${r.left}d left`
        : (r.start > day ? ` · starts day ${r.start}` : "");
      return `<div class="tl-row">
        <div class="tl-label">${dot} <b>${esc(r.genres.join(" + "))}</b>
          <span class="dim">${r.n} agents${sub}</span></div>
        <div class="tl-track">
          <div class="tl-todayline" style="left:${today}%"></div>
          <div class="tl-bar ${r.live ? "live" : (r.start > day ? "sched" : "done")}" style="left:${l}%;width:${w}%"
               title="days ${r.start}–${r.end}"></div>
        </div>
      </div>`;
    }).join("")}
    <p class="sub" style="margin-top:6px">🟢 live · 📅 scheduled — fires on its start day · ⚫ finished.</p>`;
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

/* ---------- master agent energy blast ---------- */
let masterToastEl = null;
function showMasterBlast(day, text, nTargets) {
  if (masterToastEl) masterToastEl.remove();
  const el = document.createElement("div");
  el.className = "master-toast";
  el.innerHTML = `<div class="mt-text">day ${day} ${esc(text)}</div><canvas width="720" height="260"></canvas>`;
  document.body.appendChild(el);
  masterToastEl = el;
  const cv = el.querySelector("canvas");
  const ctx = cv.getContext("2d");
  ctx.scale(2, 2);
  const W = 360, H = 130;
  const mx = 44, my = H / 2;
  const k = Math.min(nTargets, 14);
  const dots = [];
  for (let i = 0; i < k; i++) {
    const col = i % 2, rows = Math.ceil(k / 2), r = Math.floor(i / 2);
    dots.push({ x: 215 + col * 65, y: 16 + r * ((H - 32) / Math.max(rows - 1, 1)) });
  }
  const t0 = performance.now();
  const PULSE_DUR = 650, STAGGER = 90;
  function frame(now) {
    const t = now - t0;
    ctx.clearRect(0, 0, W, H);
    // master glow + node
    const g = ctx.createRadialGradient(mx, my, 2, mx, my, 36);
    g.addColorStop(0, "rgba(242,181,68,.45)");
    g.addColorStop(1, "rgba(242,181,68,0)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(mx, my, 36, 0, 7); ctx.fill();
    ctx.fillStyle = "#f2b544";
    ctx.beginPath(); ctx.arc(mx, my, 11, 0, 7); ctx.fill();
    ctx.fillStyle = "#111"; ctx.font = "11px sans-serif"; ctx.textAlign = "center";
    // strings
    ctx.strokeStyle = "rgba(242,181,68,.18)"; ctx.lineWidth = 1;
    ctx.beginPath();
    dots.forEach((d) => { ctx.moveTo(mx + 11, my); ctx.lineTo(d.x, d.y); });
    ctx.stroke();
    // energy pulses with trails
    let alive = false;
    dots.forEach((d, i) => {
      const pt = t - i * STAGGER;
      if (pt < 0) { alive = true; return; }
      const p = Math.min(pt / PULSE_DUR, 1);
      if (p < 1) alive = true;
      const pos = (pp) => {
        const e = pp * pp;
        return [mx + 11 + (d.x - mx - 11) * e, my + (d.y - my) * e];
      };
      for (let s = 2; s >= 1; s--) {
        const sp = Math.max(p - s * 0.07, 0);
        if (sp <= 0) continue;
        const [sx, sy] = pos(sp);
        ctx.fillStyle = `rgba(242,181,68,${0.22 * (1 - s / 3)})`;
        ctx.beginPath(); ctx.arc(sx, sy, 3, 0, 7); ctx.fill();
      }
      const [x, y] = pos(p);
      ctx.shadowBlur = 14; ctx.shadowColor = "#f2b544";
      ctx.fillStyle = "#ffd97a";
      ctx.beginPath(); ctx.arc(x, y, 4, 0, 7); ctx.fill();
      ctx.shadowBlur = 0;
      if (p >= 1) {
        const rt = (pt - PULSE_DUR) / 400;
        if (rt < 1) {
          alive = true;
          ctx.strokeStyle = `rgba(242,181,68,${0.7 * (1 - rt)})`;
          ctx.lineWidth = 2;
          ctx.beginPath(); ctx.arc(d.x, d.y, 4 + rt * 12, 0, 7); ctx.stroke();
        }
      }
    });
    // target dots light up on arrival
    dots.forEach((d, i) => {
      const arrived = t - i * STAGGER >= PULSE_DUR;
      ctx.fillStyle = arrived ? "#ffd97a" : "rgba(255,255,255,.35)";
      ctx.beginPath(); ctx.arc(d.x, d.y, 3, 0, 7); ctx.fill();
    });
    if (alive || t < 2600) requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  el.addEventListener("click", () => { el.remove(); masterToastEl = null; });
  setTimeout(() => {
    if (masterToastEl !== el) return;
    el.classList.add("out");
    setTimeout(() => { el.remove(); if (masterToastEl === el) masterToastEl = null; }, 550);
  }, 5200);
}

/* ---------- master agent puppet panel (agents tab) ---------- */
/* campaign energy on the agents-tab puppet panel: when a campaign fires,
   pulses of light race down the master agent's strings to the targets. */
let masterEnergyStart = 0;
let lastDirectedTargets = [];
function fireMasterEnergy() {
  masterEnergyStart = performance.now();
  if ($("tab-agents").classList.contains("active")) {
    requestAnimationFrame(masterEnergyFrame);
  }
}
function masterEnergyFrame(now) {
  drawMasterPanel();
  if (now - masterEnergyStart < 3000
      && $("tab-agents").classList.contains("active")) {
    requestAnimationFrame(masterEnergyFrame);
  } else {
    drawMasterPanel(); // settle back to the static panel
  }
}

function drawMasterPanel() {
  const cv = $("master-canvas");
  if (!cv || !state.agents || !state.agents.length) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = 132;
  if (!W) return;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const n = state.agents.length;
  const mx = W / 2, my = 22;
  const y = H - 16, pad = 16;
  const X = (i) => n === 1 ? mx : pad + (i * (W - 2 * pad)) / (n - 1);
  const targeted = new Set();
  const liveCamps = ((state.status && state.status.campaigns) || []);
  liveCamps.forEach((c) => (c.targets || []).forEach((i) => targeted.add(i)));
  const hasLive = targeted.size > 0;
  // banner: who is under live campaign energy right now
  const note = $("master-target-note");
  if (note) {
    if (hasLive) {
      note.innerHTML = `🎯 <b>${targeted.size}</b> agents under live campaign energy: ` +
        liveCamps.map((c) => `${esc((c.genres && c.genres.length)
          ? c.genres.map(campLabel).join(" + ") : (c.provider || ""))} <span style="opacity:.65">(${c.days_left}d left)</span>`).join(" · ");
      note.style.display = "";
    } else {
      note.style.display = "none";
    }
  }
  // one string per agent: faint for all, bright for live campaign targets
  ctx.lineWidth = 1;
  [[hasLive ? "rgba(245,180,90,0.05)" : "rgba(245,180,90,0.13)", false],
   ["rgba(245,180,90,0.45)", true]]
    .forEach(([style, want]) => {
      ctx.strokeStyle = style;
      ctx.beginPath();
      state.agents.forEach((a, i) => {
        if (targeted.has(i) !== want) return;
        ctx.moveTo(mx, my + 10);
        ctx.lineTo(X(i), y);
      });
      ctx.stroke();
    });
  // agent dots, cluster-colored — non-targets dim while a campaign is live
  state.agents.forEach((a, i) => {
    const x = X(i);
    const isT = targeted.has(i);
    ctx.globalAlpha = hasLive && !isT ? 0.25 : 1;
    ctx.fillStyle = CLUSTER_COLORS[a.cluster % 6];
    ctx.beginPath(); ctx.arc(x, y, isT ? 3.6 : 2.8, 0, 7); ctx.fill();
    if (isT) {
      ctx.shadowBlur = 8; ctx.shadowColor = "#4ade80";
      ctx.strokeStyle = "#4ade80"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 6, 0, 7); ctx.stroke();
      ctx.shadowBlur = 0;
    }
    if (a.pivot) {
      ctx.strokeStyle = "#f5b45a"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(x, y, 6.5, 0, 7); ctx.stroke();
    }
    ctx.globalAlpha = 1;
  });
  // master node on top
  ctx.fillStyle = "#f5b45a";
  ctx.beginPath(); ctx.arc(mx, my, 9, 0, 7); ctx.fill();
  ctx.fillStyle = "#111"; ctx.font = "10px sans-serif"; ctx.textAlign = "center";
  ctx.fillStyle = "#f5b45a"; ctx.font = "11px sans-serif"; ctx.textAlign = "left";
  ctx.fillText("MASTER AGENT", mx + 15, my + 4);
  // energy pulses: fresh campaign targets get hit with light down their strings
  const et = performance.now() - masterEnergyStart;
  if (et >= 0 && et < 3000 && lastDirectedTargets.length) {
    const tlist = lastDirectedTargets;
    // total stagger spread stays under ~2s so every pulse lands within the
    // 3s energy window, no matter how many agents a campaign targets
    const stagger = Math.max(4, Math.min(60, 1800 / Math.max(tlist.length, 1)));
    tlist.forEach((pi, k) => {
      if (pi < 0 || pi >= n) return;
      const pt = et - k * stagger;
      if (pt < 0) return;
      const p = Math.min(pt / 650, 1);
      const e = p * p; // accelerate away from the master
      const x0 = mx, y0 = my + 10, x1 = X(pi);
      for (let s = 2; s >= 1; s--) {
        const sp = Math.max(p - s * 0.08, 0);
        if (sp <= 0) continue;
        const se = sp * sp;
        ctx.fillStyle = `rgba(245,180,90,${0.25 * (1 - s / 3)})`;
        ctx.beginPath();
        ctx.arc(x0 + (x1 - x0) * se, y0 + (y - y0) * se, 2.5, 0, 7);
        ctx.fill();
      }
      ctx.shadowBlur = 10; ctx.shadowColor = "#f5b45a";
      ctx.fillStyle = "#ffd97a";
      ctx.beginPath(); ctx.arc(x0 + (x1 - x0) * e, y0 + (y - y0) * e, 3.4, 0, 7); ctx.fill();
      ctx.shadowBlur = 0;
      if (p >= 1) {
        const rt = (pt - 650) / 350;
        if (rt < 1) {
          ctx.strokeStyle = `rgba(245,180,90,${0.6 * (1 - rt)})`;
          ctx.lineWidth = 1.5;
          ctx.beginPath(); ctx.arc(x1, y, 3 + rt * 9, 0, 7); ctx.stroke();
        }
      }
    });
  }
}

/* ---------- agents ---------- */
async function refreshAgents() {
  const d = await api("/api/agents");
  state.agents = d.agents;
  $("agent-cards").innerHTML = d.agents.map((a) => `
    <div class="card${(a.targeted && a.targeted.length) ? " targeted" : ""}" data-id="${a.id}">
      <h3>${esc(a.archetype_pretty)}</h3>
      <div class="meta">${a.id} · cluster <b style="color:${CLUSTER_COLORS[a.cluster % 6]}">${a.cluster}</b>${pinBadge(a)}${(a.targeted && a.targeted.length) ? ` · <span class="tgt">🎯 ${esc(a.targeted.map(campLabel).join(" + "))} energy</span>` : ""}${(a.scheduled && a.scheduled.length) ? ` · <span class="pin">📅 ${esc(a.scheduled.map(campLabel).join(" + "))} scheduled</span>` : ""}</div>
      <div class="meta">into <b>${esc(a.top_genre)}</b> · ${a.n_plays} plays</div>
    </div>`).join("");
  // remember which campaign targeting the cards reflect, so the live poller
  // can re-render them the moment a campaign starts or ends
  state.targetSig = JSON.stringify(((state.status && state.status.campaigns) || [])
    .map((c) => [c.id, (c.targets || []).slice().sort((x, y) => x - y)]));
  document.querySelectorAll("#agent-cards .card").forEach((c) =>
    c.addEventListener("click", () => openAgentPage(c.dataset.id)));
  const pick = $("home-agent-pick");
  const cur = pick.value;
  pick.innerHTML = d.agents.map((a, i) =>
    `<option value="${a.id}">${a.pivot ? "★ " : ""}Agent ${i + 1} · ${esc(a.archetype_pretty)} — ${a.id}</option>`).join("");
  if (cur) pick.value = cur;
  if (!state.homeAgent && d.agents.length) {
    state.homeAgent = d.agents[0].id;
    pick.value = state.homeAgent;
    loadHome();
  }
  drawMasterPanel();
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
  <h3 class="ap-sec">Home screen right now</h3>
  <p class="sub">What this viewer actually sees when they open the app —
  <b>${a.home_screen.rails.length} collections</b>, including any promoted
  collections from live Master Agent campaigns (look for the Promoted badge).</p>
  <div class="ap-rails">${a.home_screen.rails.map(railHTML).join("")}</div>
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
  state.coldstart = null;
  loadHome();
});
async function loadHome() {
  if (!state.homeAgent) return;
  const a = await api("/api/agent?id=" + encodeURIComponent(state.homeAgent));
  state.homeData = a;
  renderHome();
}
const GENRE_EMOJI = { Sports: "⚾", Bollywood: "🎬", Horror: "👻", Comedy: "😂",
  Drama: "🎭", Documentary: "🎥", Reality: "📺", Music: "🎵", News: "📰",
  Kids: "🧸", Family: "👨‍👩‍👧", Action: "💥", "Science Fiction": "🚀", Fantasy: "🐉",
  Romance: "💕", Thriller: "🔪", Crime: "🚔", Mystery: "🔎", History: "🏛️",
  Adventure: "🧭", Animation: "✨", Western: "🤠" };
const PIN_EMOJI = { "Sports": "⚾", "Science Fiction": "🚀", "Bollywood": "★" };
function pinBadge(a) {
  if (a.pivot) return ` · <span style="color:var(--amber)">★ Bollywood pivot</span>`;
  if (a.pin) return ` · <span class="pin">${PIN_EMOJI[a.pin] || "📌"} ${esc(a.pin)} anchor</span>`;
  return "";
}
function tileHTML(t) {
  const provs = (t.providers || []).slice(0, 2);
  const prov = provs.map((p) => APP_LOGOS[p] ? logoImg(p, "prov-logo") : esc(p)).join("");
  // 16:9 cards: prefer landscape backdrop art, fall back to the poster crop
  const art_src = t.backdrop || t.poster;
  const art = art_src
    ? `<img loading="lazy" src="${art_src}" alt="">`
    : `<div class="tile-noposter"><span class="np-emoji">${GENRE_EMOJI[t.genre] || "🎞️"}</span><span class="np-title">${esc(t.title)}</span></div>`;
  // Hover "Why this?" tooltip: the native title tooltip is suppressed when
  // why-data is present so the two don't stack.
  const why = t.why_hover
    ? ` data-why="${esc(t.why_hover).replace(/\n/g, "&#10;")}"`
    : ` title="${esc(t.title)}"`;
  return `<div class="tile" data-id="${t.id}"${why}>
    ${t.sponsored ? `<span class="spon-badge">Sponsored</span>` : ""}
    ${t.promoted ? `<span class="promo-badge">Promoted</span>` : ""}
    ${t.live ? `<span class="live-badge">● LIVE</span>` : ""}
    ${art}
    <div class="ti"><b>${esc(t.title)}</b><span>${esc(t.genre)}</span>
    ${prov ? `<span class="prov prov-logos">${prov}</span>` : ""}</div></div>`;
}
const APP_LOGOS = {
  "Netflix": "netflix.svg",
  "Disney+": "disney.svg",
  "HBO Max": "hbomax.svg",
  "Hulu": "hulu.svg",
  "Prime Video": "primevideo.svg",
  "Apple TV+": "appletv.svg",
  "Peacock": "peacock.svg",
  "Paramount+": "paramount.svg",
  "Tubi": "tubi.svg",
  "Plex": "plex.svg",
  "FOX One": "foxone.svg",
  "Roku Channel": "roku.svg",
  "Pluto TV": "pluto.svg",
  "Crunchyroll": "crunchyroll.svg",
  "Britbox": "britbox.svg",
};
// Dark logos that need inverting to read on the dark tiles/cards
// (currently empty: Disney+, Apple TV+ and HBO Max ship as native white SVGs)
const LOGO_INVERT = new Set([]);
function logoImg(app, cls) {
  if (!APP_LOGOS[app]) return "";
  const inv = LOGO_INVERT.has(app) ? " logo-inv" : "";
  return `<img class="${cls}${inv}" src="/logos/${APP_LOGOS[app]}" alt="${esc(app)}" title="${esc(app)}" loading="lazy">`;
}
const APP_COLORS = {
  "Netflix": "#E50914",
  "Disney+": "#113CCF",
  "HBO Max": "#8B5CF6",
  "Hulu": "#1CE783",
  "Prime Video": "#00A8E1",
  "Apple TV+": "#A3A3A3",
  "Peacock": "#00D4FF",
  "Paramount+": "#0064FF",
  "Tubi": "#9D4EDD",
  "Plex": "#E5A00D",
  "FOX One": "#3B82F6",
  "Roku Channel": "#6F1EAA",
  "Pluto TV": "#F59E0B",
  "Crunchyroll": "#F47521",
  "Britbox": "#6366F1",
};
function appTileHTML(a) {
  const c = APP_COLORS[a.app] || "#64748b";
  const logo = logoImg(a.app, "app-logo");
  return `<div class="app-tile" data-app="${esc(a.app)}" style="--brand:${c}">${logo}<span>${esc(a.app)}</span></div>`;
}
function railHTML(r) {
  const tiles = r.kind === "apps"
    ? r.items.map(appTileHTML).join("")
    : r.items.map(tileHTML).join("");
  const cls = r.kind === "featured" ? "rail rail-featured" : "rail";
  return `<div class="${cls}"><h3>${esc(r.title)}${r.why ? ` <span class="why">${esc(r.why)}</span>` : ""}</h3>
    <div class="tiles">${tiles}</div></div>`;
}
function renderHome() {
  const a = state.homeData;
  $("rails").innerHTML = a.home_screen.rails.map(railHTML).join("");
  document.querySelectorAll("#rails .tile").forEach((el) =>
    el.addEventListener("click", () => clickTile(el.dataset.id)));
  $("taste-bars").innerHTML = tasteBars(a.taste);
  $("home-hint").innerHTML =
    `Showing <b>${esc(a.archetype_pretty)}</b> (${a.id}) — day ${state.status.day}, cluster ${a.cluster}, ${a.home_screen.rails.length} collections. Click any tile.`;
  maybeShowTakeover(a);
}
/* ---------- screen takeover overlay ---------- */
function maybeShowTakeover(a) {
  const to = a.home_screen && a.home_screen.takeover;
  if (!to) return;
  state.takeoverDismissed = state.takeoverDismissed || {};
  const key = `${a.id}-${to.campaign_id}-${state.status.day}`;
  if (state.takeoverDismissed[key]) return;
  let ov = $("takeover-overlay");
  if (!ov) {
    ov = document.createElement("div");
    ov.id = "takeover-overlay";
    document.body.appendChild(ov);
  }
  ov.innerHTML = `
    <div class="to-full" style="${to.backdrop ? `background-image:url('${esc(to.backdrop)}')` : ""}"></div>
    <div class="to-shade"></div>
    ${to.sponsor ? `<div class="to-sponsor">Sponsored by <span class="to-sponsor-brand">${esc(to.sponsor)}</span></div>` : ""}
    <div class="to-content">
      <div class="to-kicker">${to.sponsor ? esc(to.sponsor) + " presents" : "Featured presentation"}</div>
      <div class="to-title-big">${esc(to.title)}</div>
      ${to.headline ? `<div class="to-headline">${esc(to.headline)}</div>` : ""}
      <div class="to-meta">${esc(to.genre || "")}${(to.providers || []).length ? " · " + esc(to.providers.join(" / ")) : ""}</div>
      <div class="to-actions">
        <button id="to-watch" class="go-btn to-watch">▶ Watch</button>
        <button id="to-dismiss" class="to-home">Go to homescreen</button>
      </div>
    </div>`;
  ov.style.display = "block";
  const dismiss = async (watched) => {
    state.takeoverDismissed[key] = true;
    ov.style.display = "none";
    if (!watched) {
      try {
        await api("/api/takeover_dismiss", { method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ campaign_id: to.campaign_id, agent_id: a.id }) });
      } catch (e) { /* non-critical */ }
    }
  };
  $("to-dismiss").addEventListener("click", () => dismiss(false));
  $("to-watch").addEventListener("click", async () => {
    dismiss(true);
    await clickTile(to.id);
  });
}
async function clickTile(itemId) {
  const a = await api("/api/click", { method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ agent_id: state.homeAgent, item_id: itemId }) });
  state.homeData = a;
  const last = a.recent_plays[0];
  logEvent(`<span class="e-play">▶ play</span> <b>${esc(last.title)}</b> <span style="opacity:.6">(${esc(gpretty(last.genre))})</span> → taste updated`, true);
  renderHome();
  // Paused: show the why-recommended breakdown for the clicked title.
  if (state.status && !state.status.running) showExplainer(itemId);
}
async function showExplainer(itemId) {
  try {
    const ex = await api(`/api/explain?agent=${encodeURIComponent(state.homeAgent)}&item=${encodeURIComponent(itemId)}`);
    if (!ex.title) return;
    $("explainer-body").innerHTML = `
      <h4>Why “${esc(ex.title)}” was recommended</h4>
      <div class="dp-sub">${esc(ex.genre)} · for ${esc(state.homeAgent)} · every signal below is from this agent's own history</div>
      ${ex.poster ? `<img src="${esc(ex.poster)}" style="height:120px;border-radius:8px;margin-bottom:8px" alt="">` : ""}
      ${ex.signals.map((s) => `<div class="ex-signal">◆ ${esc(s)}</div>`).join("")}`;
    $("explainer").hidden = false;
  } catch (e) { /* ignore */ }
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
async function showTrending() {
  const box = $("trending-searches");
  if ($("search-box").value.trim()) { box.style.display = "none"; return; }
  let d; try { d = await api("/api/trending_searches"); } catch (e) { return; }
  const items = d.items || [];
  if (!items.length) { box.style.display = "none"; return; }
  box.innerHTML = `<span class="trend-label">Trending</span><div class="tiles trend-tiles">${items.map(tileHTML).join("")}</div>`;
  box.style.display = "block";
  box.querySelectorAll(".tile").forEach((el) =>
    el.addEventListener("click", async () => {
      box.style.display = "none";
      await clickTile(el.dataset.id);
    }));
}
$("search-box").addEventListener("focus", showTrending);
document.addEventListener("click", (e) => {
  if (!e.target.closest("#trending-searches") && e.target.id !== "search-box")
    $("trending-searches").style.display = "none";
});
$("search-box").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value;
  searchTimer = setTimeout(async () => {
    if (!q.trim()) {
      $("search-results").style.display = "none";
      showTrending();
      return;
    }
    $("trending-searches").style.display = "none";
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

/* ---------- cold-start showcase ---------- */
const COLD_QS = [
  { q: "What sounds like your Friday night?",
    opts: ["Action", "Comedy", "Horror", "Romance"] },
  { q: "Pick another vibe",
    opts: ["Science Fiction", "Drama", "Documentary", "Thriller"] },
  { q: "One more — what else?",
    opts: ["Bollywood", "Sports", "Animation", "Crime"] },
];
let coldPicks = [];
$("btn-coldstart").addEventListener("click", () => {
  coldPicks = [];
  renderColdQ(0);
  $("coldstart-modal").hidden = false;
});
$("coldstart-x").addEventListener("click", () => { $("coldstart-modal").hidden = true; });
function renderColdQ(i) {
  const step = COLD_QS[i];
  $("coldstart-body").innerHTML = `
    <h4>✨ Cold start — question ${i + 1} of 3</h4>
    <div class="dp-sub">${esc(step.q)} — 3 answers stand in for months of watch history.</div>
    <div class="cold-opts">${step.opts.map((g) =>
      `<button class="cold-opt" data-g="${esc(g)}">${esc(g)}</button>`).join("")}</div>
    ${i > 0 ? `<button class="cold-back" id="cold-back">← back</button>` : ""}`;
  document.querySelectorAll(".cold-opt").forEach((b) =>
    b.addEventListener("click", () => {
      coldPicks[i] = b.dataset.g;
      if (i < COLD_QS.length - 1) renderColdQ(i + 1);
      else finishColdstart();
    }));
  const back = $("cold-back");
  if (back) back.addEventListener("click", () => renderColdQ(i - 1));
}
async function finishColdstart() {
  $("coldstart-body").innerHTML = `<h4>✨ Bootstrapping…</h4><div class="dp-sub">Building a taste vector from 3 answers.</div>`;
  try {
    const d = await api("/api/coldstart", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ picks: coldPicks }) });
    state.coldstart = d;
    $("coldstart-modal").hidden = true;
    renderColdstartHome(d);
  } catch (e) {
    $("coldstart-body").innerHTML = `<h4>✨ Cold start failed</h4><div class="dp-sub">Try again.</div>`;
  }
}
function renderColdstartHome(d) {
  const tops = d.top_genres.slice(0, 3).join(" · ");
  $("home-hint").innerHTML =
    `✨ <b>Cold-start preview</b> — bootstrapped from 3 answers (<b>${esc(tops)}</b>), <b>0 minutes</b> of watch history. ` +
    `Hover any tile for “Why this?”. <button id="cold-exit" class="cold-back">← back to agents</button> ` +
    `<button id="cold-retake" class="cold-back">retake quiz</button>`;
  $("rails").innerHTML = d.home_screen.rails.map(railHTML).join("");
  // Preview mode: tiles don't play — the viewer doesn't exist in the sim yet.
  document.querySelectorAll("#rails .tile").forEach((el) =>
    el.addEventListener("click", () =>
      logEvent(`<span class="e-search">✨ preview</span> “${esc(el.title)}” — pick a real agent above to play titles`, true)));
  $("taste-bars").innerHTML = tasteBars(d.taste);
  $("cold-exit").addEventListener("click", () => {
    state.coldstart = null;
    loadHome();
  });
  $("cold-retake").addEventListener("click", () => {
    state.coldstart = null;
    coldPicks = [];
    renderColdQ(0);
    $("coldstart-modal").hidden = false;
  });
}

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
  // dots: positions computed first so the Master Agent's puppet strings
  // can anchor to them (clickable — positions cached for hit-testing)
  const dots = [];
  for (let i = 0; i < n; i++) {
    const p = j.paths[i][Math.min(day, j.paths[i].length - 1)];
    dots.push({ x: px(p[0]), y: py(p[1]), i });
  }
  // puppet strings: a very slight thin line from the Master Agent node to
  // every agent a live campaign is steering — drawn under the dots
  const camps = j.campaigns || [];
  const mx = W / 2, my = 16;
  if (camps.length) {
    ctx.strokeStyle = "rgba(245, 180, 90, 0.22)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    camps.forEach((c) => c.targets.forEach((i) => {
      const d = dots[i];
      if (d) { ctx.moveTo(mx, my); ctx.lineTo(d.x, d.y); }
    }));
    ctx.stroke();
  }
  state.journeyDots = [];
  for (const d of dots) {
    ctx.fillStyle = CLUSTER_COLORS[j.labels[d.i] % 6];
    ctx.beginPath(); ctx.arc(d.x, d.y, 3.2, 0, 7); ctx.fill();
    state.journeyDots.push(d);
  }
  // Master Agent node on top
  if (camps.length) {
    ctx.fillStyle = "#f5b45a";
    ctx.beginPath(); ctx.arc(mx, my, 6, 0, 7); ctx.fill();
    ctx.font = "11px sans-serif";
    ctx.fillText("MASTER AGENT", mx + 11, my + 4);
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
     <span class="lg"><label style="cursor:pointer"><input type="checkbox" id="trails-cb" ${state.showTrails ? "checked" : ""}> trails</label></span>` +
    ((j.campaigns || []).length ? `<span class="lg" style="color:var(--amber)">master agent pulling strings</span>` : "");
  const cb = $("trails-cb");
  if (cb) cb.addEventListener("change", (e) => { state.showTrails = e.target.checked; drawJourney(); });
}

/* ---------- boot ---------- */
function journeyDotAt(mx, my) {
  if (!state.journeyDots || !state.agents) return null;
  let best = null, bd = 14 * 14;
  for (const d of state.journeyDots) {
    const dd = (d.x - mx) * (d.x - mx) + (d.y - my) * (d.y - my);
    if (dd < bd) { bd = dd; best = d; }
  }
  return best;
}
function showDotPop(a, idx, mx, my) {
  const pop = $("dot-pop");
  const top = Object.entries(a.taste).sort((x, y) => y[1] - x[1]).slice(0, 5);
  const maxv = top.length ? top[0][1] : 1;
  const cname = (state.journey && state.journey.clusters[a.cluster])
    ? state.journey.clusters[a.cluster].name : ("cluster " + a.cluster);
  const plays = (a.recent_plays || []).slice(0, 3).map((p) =>
    `<div>▶ ${esc(p.title)} <span style="color:var(--teal-dim)">· day ${p.day}</span></div>`).join("");
  pop.innerHTML = `
    <button class="dp-x" id="dotpop-x">×</button>
    <h4>Agent #${idx + 1} ${a.is_pivot ? '<span class="star">★ Bollywood pivot</span>' : ""}</h4>
    <div class="dp-sub">${esc(a.archetype_pretty)} · ${esc(cname)}</div>
    <div class="dp-row"><span>Top genre</span><b>${esc(a.top_genre)}</b></div>
    <div class="dp-row"><span>Plays</span><b>${a.n_plays}</b></div>
    <div class="dp-row"><span>Events</span><b>${a.n_events}</b></div>
    <div style="margin:8px 0 4px;color:var(--dim)">Taste</div>
    ${top.map(([g, v]) => `
      <div class="dp-bar"><span class="g">${esc(g)}</span>
      <span class="tr"><span class="fl" style="display:block;width:${Math.round(100 * v / maxv)}%"></span></span>
      <span class="v">${v.toFixed(2)}</span></div>`).join("")}
    ${plays ? `<div style="margin:8px 0 4px;color:var(--dim)">Recent plays</div><div class="dp-plays">${plays}</div>` : ""}
    <div class="dp-actions"><button id="dotpop-full">Full agent page →</button></div>`;
  pop.hidden = false;
  const wrap = $("journey-wrap");
  const pw = 300, ph = Math.min(pop.offsetHeight || 380, 420);
  pop.style.left = Math.min(mx + 14, Math.max(8, wrap.clientWidth - pw - 8)) + "px";
  pop.style.top = Math.max(8, Math.min(my - 20, wrap.clientHeight - ph - 8)) + "px";
  $("dotpop-x").addEventListener("click", (ev) => { ev.stopPropagation(); pop.hidden = true; });
  $("dotpop-full").addEventListener("click", () => { pop.hidden = true; openAgentPage(a.id); });
}
function initJourneyClicks() {
  const cv = $("journey-canvas");
  const pos = (e) => {
    const r = cv.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };
  cv.addEventListener("click", async (e) => {
    const [mx, my] = pos(e);
    const d = journeyDotAt(mx, my);
    const pop = $("dot-pop");
    if (d && state.agents[d.i]) {
      try {
        const a = await api("/api/agent?id=" + encodeURIComponent(state.agents[d.i].id));
        showDotPop(a, d.i, mx, my);
      } catch (err) { pop.hidden = true; }
    } else {
      pop.hidden = true;
    }
  });
  cv.addEventListener("mousemove", (e) => {
    const [mx, my] = pos(e);
    cv.style.cursor = journeyDotAt(mx, my) ? "pointer" : "default";
  });
}
async function refreshAll() {
  await refreshStatus();
  await refreshAgents();
  if (state.homeAgent) loadHome();
  if ($("tab-journey").classList.contains("active")) fetchJourney();
}
(async function boot() {
  connectStream();
  initJourneyClicks();
  await refreshAll();
  setInterval(() => {
    if (!$("tab-journey").classList.contains("active")) refreshStatus();
    if (state.detailId) refreshDetail();
  }, 3000);
})();

/* ---------- collections management ---------- */
async function loadCollections() {
  const d = await api("/api/collections");
  renderCollections(d);
}
async function collAction(endpoint, id) {
  await api(endpoint, { method: "POST", body: JSON.stringify({ id }) });
  loadCollections();
}
function collCard(c, actions) {
  const status = c.status || "active";
  const cls = status === "deprecated" ? "coll-card deprecated" : "coll-card";
  return `<div class="${cls}">
    <h4>${c.fixed ? `<span class="coll-pos">#${c.fixedPos}</span>` : ""}
      ${esc(c.title)}
      <span class="coll-status ${status}">${status}</span>
      ${c.ai_approved ? `<span class="coll-badge-ai">AI approved</span>` : ""}</h4>
    <p>${esc(c.description || "")}</p>
    ${c.recommendation ? `<div class="rec"><b>AI recommendation</b>${esc(c.recommendation)}</div>` : ""}
    <div class="coll-actions">${actions}</div>
  </div>`;
}
function renderCollections(d) {
  const fixed = d.fixed || [], official = d.official || [];
  const removed = d.removed || [], ai = d.ai_candidates || [];
  // fixed rails with positions
  const pos = { featured: 1, apps: 2, continue: 3 };
  $("coll-fixed").innerHTML = fixed.map((c) =>
    collCard({ ...c, fixedPos: pos[c.id] || "?", status: "fixed" }, "")).join("");
  $("coll-official").innerHTML = official.map((c) => {
    const acts = c.status === "deprecated"
      ? `<button class="coll-btn" onclick="collAction('/api/collections/restore','${c.id}')">Restore</button>
         <button class="coll-btn danger" onclick="collAction('/api/collections/remove','${c.id}')">Remove</button>`
      : `<button class="coll-btn warn" onclick="collAction('/api/collections/deprecate','${c.id}')">Deprecate</button>
         <button class="coll-btn danger" onclick="collAction('/api/collections/remove','${c.id}')">Remove</button>`;
    return collCard(c, acts);
  }).join("");
  $("coll-official-count").textContent =
    `${official.filter((c) => c.status === "active").length} active · ${official.filter((c) => c.status === "deprecated").length} deprecated`;
  // removed
  const rw = $("coll-removed-wrap");
  if (removed.length) {
    rw.style.display = "";
    $("coll-removed").innerHTML = removed.map((id) =>
      `<div class="coll-card"><h4>${esc(id)} <span class="coll-status rejected">removed</span></h4>
       <div class="coll-actions"><button class="coll-btn" onclick="collAction('/api/collections/restore','${id}')">Restore to pool</button></div></div>`
    ).join("");
    $("coll-removed-count").textContent = removed.length;
  } else rw.style.display = "none";
  // AI candidates: 3-column table (name, recommendation, action)
  const pending = ai.filter((c) => c.status === "pending");
  const decided = ai.filter((c) => c.status !== "pending");
  $("coll-ai").innerHTML = `<table class="ai-table"><thead><tr>
    <th>Collection</th><th>Titles</th><th>AI recommendation</th><th>Action</th></tr></thead><tbody>` +
    ai.map((c) => `<tr class="${c.status !== "pending" ? "decided" : ""}">
      <td><b>${esc(c.title)}</b><br><span class="muted">${esc(c.description || "")}</span>
        ${c.status !== "pending" ? ` <span class="coll-status ${c.status}">${esc(c.status)}</span>` : ""}</td>
      <td style="text-align:right;font-variant-numeric:tabular-nums">${(c.title_count ?? 0).toLocaleString()}</td>
      <td>${esc(c.recommendation || "")}</td>
      <td>${c.status === "pending"
        ? `<button class="coll-btn go" onclick="collAction('/api/collections/approve','${c.id}')">Approve</button>`
        : `<button class="coll-btn warn" onclick="collAction('/api/collections/reset_ai','${c.id}')">Reset to pool</button>`}</td>
    </tr>`).join("") + `</tbody></table>`;
  $("coll-ai-count").textContent = `${pending.length} awaiting review · ${decided.length} decided`;
}
