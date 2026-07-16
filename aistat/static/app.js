/* AIStat dashboard: fetches the aggregate API and renders Chart.js charts.
   Live updates: an SSE `update` event fires after every poller data batch
   (the live phase mid-cycle and the completed cycle) and triggers a full
   data refresh — no page reload. */

"use strict";

const PALETTE = [
  "#4f6df5", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6",
  "#06b6d4", "#ec4899", "#84cc16", "#64748b", "#f97316",
  "#0ea5e9", "#a855f7", "#22c55e", "#eab308", "#94a3b8",
];

const state = {
  projects: [],
  agents: [],
  models: [],
  days: "30",    // "" = all time
  from: "",      // UTC datetime-local value
  to: "",        // UTC datetime-local value
  group: "model",
  lastDate: null, // max date present in daily_usage (from /api/meta)
  charts: {},
  csrf: null,
  lastSyncMarker: null,
  syncPollTimer: null,
  focusSyncTimer: null,
};

const $ = (id) => document.getElementById(id);

// ---------- formatting ----------

function fmtTokens(n) {
  if (n == null) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toLocaleString("ru-RU", { maximumFractionDigits: 2 }) + " млрд";
  if (abs >= 1e6) return (n / 1e6).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " млн";
  if (abs >= 1e3) return (n / 1e3).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " тыс";
  return n.toLocaleString("ru-RU");
}

function fmtUSD(n) {
  if (n == null) return "—";
  return "$" + n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtCredits(n) {
  if (n == null) return "—";
  return n.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}

// Cost efficiency values (USD per SP, USD per hour per SP) can be well below a
// cent, so keep up to 4 fraction digits while still reading as money.
function fmtUSDFine(n) {
  if (n == null) return "—";
  return "$" + n.toLocaleString("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
}

function fmtNum(n) {
  if (n == null) return "—";
  return n.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
}

function fmtDateTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// ---------- data access ----------

async function fetchJSON(url) {
  const resp = await fetch(url, { credentials: "same-origin" });
  if (resp.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.assign(`/login?next=${next}`);
    throw new Error("authentication required");
  }
  if (!resp.ok) throw new Error(`${url} → HTTP ${resp.status}`);
  return resp.json();
}

function periodRange() {
  if (state.from || state.to) {
    return { from: utcDateTime(state.from), to: utcDateTime(state.to) };
  }
  // The period is anchored to the newest data date, not the browser clock.
  if (!state.days || state.days === "custom" || !state.lastDate) return { from: null, to: null };
  const last = new Date(state.lastDate + "T00:00:00Z");
  const from = new Date(last.getTime() - (Number(state.days) - 1) * 86400000);
  return { from: from.toISOString().slice(0, 10), to: state.lastDate };
}

function utcDateTime(value) {
  if (!value) return null;
  return value.length === 16 ? value + ":00Z" : value + "Z";
}

function query(params) {
  const { from, to } = periodRange();
  const q = new URLSearchParams();
  if (from) q.set("from", from);
  if (to) q.set("to", to);
  for (const project of state.projects) q.append("project", project);
  for (const agent of state.agents) q.append("agent", agent);
  for (const model of state.models) q.append("model", model);
  for (const [k, v] of Object.entries(params || {})) q.set(k, v);
  const s = q.toString();
  return s ? "?" + s : "";
}

// ---------- charts ----------

function colorFor(index) {
  return PALETTE[index % PALETTE.length];
}

function upsertChart(id, config) {
  const existing = state.charts[id];
  if (existing) {
    existing.data = config.data;
    existing.options = config.options;
    existing.update();
    return existing;
  }
  const chart = new Chart($(id).getContext("2d"), config);
  state.charts[id] = chart;
  return chart;
}

function stackedDailyConfig(rows, valueOf, valueFmt) {
  const dates = [...new Set(rows.map((r) => r.date))].sort();
  const keyTotals = new Map();
  for (const r of rows) keyTotals.set(r.key, (keyTotals.get(r.key) || 0) + (valueOf(r) || 0));
  const keys = [...keyTotals.keys()].sort((a, b) => keyTotals.get(b) - keyTotals.get(a));
  const byDateKey = new Map(rows.map((r) => [r.date + "\u0000" + r.key, r]));
  const datasets = keys.map((key, i) => ({
    label: key,
    data: dates.map((d) => {
      const r = byDateKey.get(d + "\u0000" + key);
      return r ? valueOf(r) || 0 : 0;
    }),
    backgroundColor: colorFor(i),
    borderWidth: 0,
    maxBarThickness: 64,
  }));
  return {
    type: "bar",
    data: { labels: dates, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index" },
      plugins: {
        legend: { position: "bottom", labels: { boxWidth: 12, boxHeight: 12 } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${valueFmt(ctx.parsed.y)}`,
            footer: (items) => "Итого: " + valueFmt(items.reduce((s, it) => s + it.parsed.y, 0)),
          },
        },
      },
      scales: {
        x: { stacked: true, grid: { display: false } },
        y: { stacked: true, ticks: { callback: (v) => valueFmt(v) } },
      },
    },
  };
}

function renderDaily(daily) {
  const rows = daily.rows;
  $("daily-est").hidden = !daily.estimated;
  $("cost-est").hidden = !daily.estimated;
  upsertChart("chart-daily-tokens", stackedDailyConfig(rows, (r) => r.total_tokens, fmtTokens));
  upsertChart("chart-daily-cost", stackedDailyConfig(rows, (r) => r.cost_usd, fmtUSD));
}

function renderAgentsChart(agents) {
  const labels = agents.map((a) => (a.estimated ? "≈ " : "") + a.name);
  upsertChart("chart-agents", {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data: agents.map((a) => a.total_tokens),
        backgroundColor: agents.map((_, i) => colorFor(i)),
        borderWidth: 2,
        borderColor: "#ffffff",
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: "right", labels: { boxWidth: 12, boxHeight: 12 } },
        tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${fmtTokens(ctx.parsed)}` } },
      },
    },
  });
}

function renderAgentsTable(agents) {
  const tbody = $("table-agents").querySelector("tbody");
  tbody.innerHTML = "";
  for (const a of agents) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${a.estimated ? "≈ " : ""}${esc(a.name)}</td>
      <td>${esc(a.model || "—")}</td>
      <td class="num">${fmtTokens(a.total_tokens)}</td>
      <td class="num">${fmtUSD(a.cost_usd)}${a.has_unpriced ? " *" : ""}</td>
      <td class="num">${fmtCredits(a.cost_credits)}</td>
      <td class="num">${a.runs}</td>`;
    tbody.appendChild(tr);
  }
}

function renderProjects(projects) {
  upsertChart("chart-projects", {
    type: "bar",
    data: {
      labels: projects.map((p) => p.title),
      datasets: [{
        data: projects.map((p) => p.total_tokens),
        backgroundColor: projects.map((_, i) => colorFor(i)),
        borderWidth: 0,
        maxBarThickness: 42,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => fmtTokens(ctx.parsed.x) } },
      },
      scales: { x: { ticks: { callback: (v) => fmtTokens(v) } } },
    },
  });

  const tbody = $("table-projects").querySelector("tbody");
  tbody.innerHTML = "";
  for (const p of projects) {
    const chips = Object.entries(p.statuses)
      .sort((a, b) => b[1] - a[1])
      .map(([s, n]) => `<span class="chip chip-${esc(s)}">${esc(s)}: ${n}</span>`)
      .join("");
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(p.title)}</td>
      <td class="num">${p.issues}<br><span class="status-chips">${chips}</span></td>
      <td class="num">${fmtTokens(p.total_tokens)}</td>
      <td class="num">${fmtUSD(p.cost_usd)}${p.unpriced_tokens > 0 || p.cost_unattributed_issues > 0 ? " *" : ""}</td>
      <td class="num">${fmtCredits(p.cost_credits)}</td>
      <td class="num">${fmtNum(p.story_points)}</td>
      <td class="num">${p.tokens_per_sp == null ? "—" : fmtTokens(p.tokens_per_sp)}</td>`;
    tbody.appendChild(tr);
  }
}

function renderEfficiency(issues) {
  const tbody = $("table-efficiency").querySelector("tbody");
  tbody.innerHTML = "";
  for (const it of issues) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(it.identifier || it.issue_id)}</td>
      <td class="ellipsis" title="${esc(it.title || "")}">${esc(it.title || "")}</td>
      <td>${esc(it.project || "—")}</td>
      <td>${esc((it.agents || []).join(", ") || "—")}</td>
      <td><span class="chip chip-${esc(it.status)}">${esc(it.status)}</span></td>
      <td class="num">${fmtNum(it.story_points)}</td>
      <td class="num">${fmtTokens(it.total_tokens)}</td>
      <td class="num">${fmtTokens(it.tokens_per_sp)}</td>`;
    tbody.appendChild(tr);
  }
}

function renderModelEfficiency(data) {
  const tbody = $("table-model-efficiency").querySelector("tbody");
  tbody.innerHTML = "";
  const models = (data && data.models) || [];
  for (const m of models) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(m.model)}${m.has_unpriced ? " *" : ""}</td>
      <td class="num">${fmtNum(m.story_points)}</td>
      <td class="num">${fmtTokens(m.total_tokens)}</td>
      <td class="num">${m.tokens_per_sp == null ? "—" : fmtTokens(m.tokens_per_sp)}</td>
      <td class="num">${m.cost_usd == null ? "—" : fmtUSDFine(m.cost_usd)}</td>
      <td class="num">${m.cost_per_sp == null ? "—" : fmtUSDFine(m.cost_per_sp)}</td>
      <td class="num">${m.weighted_efficiency == null ? "—" : fmtUSDFine(m.weighted_efficiency)}</td>`;
    tbody.appendChild(tr);
  }
  if (!models.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="7" class="note">Нет задач со story points и загруженной статистикой для разреза по моделям.</td>`;
    tbody.appendChild(tr);
  }
}

function renderSummary(s) {
  const est = s.estimated ? "≈ " : "";
  $("card-tokens").textContent = est + fmtTokens(s.total_tokens);
  $("card-tokens-sub").textContent =
    `ввод ${fmtTokens(s.input_tokens)} · вывод ${fmtTokens(s.output_tokens)} · кеш ${fmtTokens(s.cache_read_tokens + s.cache_write_tokens)}`;
  $("card-cost").textContent = est + fmtUSD(s.cost_usd);
  $("card-cost-sub").textContent = s.has_unpriced ? "есть неоценённые модели!" : "по официальным тарифам";
  $("card-credits").textContent = est + fmtCredits(s.cost_credits);
  $("card-sp").textContent = fmtNum(s.story_points);
  $("card-sp-sub").textContent = `задач: ${s.issues} · с SP: ${s.issues_with_sp}`;
  $("card-eff").textContent = s.tokens_per_sp == null ? "—" : fmtTokens(s.tokens_per_sp);
  // Cost/weighted efficiency lean on model attribution + pricing, so they are
  // always estimates (≈); a trailing * marks unpriced tokens in the mix.
  const effStar = s.efficiency_has_unpriced ? " *" : "";
  $("card-cost-eff").textContent =
    s.cost_per_sp == null ? "—" : "≈ " + fmtUSDFine(s.cost_per_sp) + effStar;
  $("card-weighted-eff").textContent =
    s.weighted_efficiency == null ? "—" : "≈ " + fmtUSDFine(s.weighted_efficiency) + effStar;
  $("sync-label").textContent = "синхронизация: " +
    (s.last_cycle ? fmtDateTime(s.last_cycle.finished_at) : "ещё не было");
  const unpriced = s.unpriced_models || [];
  $("footer-unpriced").textContent = unpriced.length
    ? "⚠ без тарифа: " + unpriced.join(", ")
    : "все модели с официальным тарифом";
}

function esc(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- refresh ----------

async function refreshAll() {
  $("estimate-note").hidden = !(
    state.projects.length || state.agents.length || state.group !== "model" || state.from || state.to
  );
  const [summary, daily, agents, projects, efficiency, modelEfficiency, health] = await Promise.all([
    fetchJSON("/api/summary" + query()),
    fetchJSON("/api/daily" + query({ group: state.group })),
    fetchJSON("/api/agents" + query()),
    fetchJSON("/api/projects" + query()),
    fetchJSON("/api/efficiency" + query({ limit: "15" })),
    fetchJSON("/api/model-efficiency" + query()),
    fetchJSON("/api/health"),
  ]);
  renderSummary(summary);
  renderDaily(daily);
  renderAgentsChart(agents.agents);
  renderAgentsTable(agents.agents);
  renderProjects(projects.projects);
  renderEfficiency(efficiency.issues);
  renderModelEfficiency(modelEfficiency);

  const badge = $("health-badge");
  badge.hidden = false;
  badge.textContent = health.status;
  badge.className = "badge " + (health.status === "ok" ? "badge-ok" : "badge-warn");
  $("footer-span").textContent = health.daily_usage_span.first_date
    ? `данные: ${health.daily_usage_span.first_date} — ${health.daily_usage_span.last_date}`
    : "данных пока нет";
  $("footer-credits").textContent =
    `курс кредитов: ${health.pricing.credits_per_usd} за $1`;
}

// ---------- live updates (SSE locally, polling on WSGI hosting) ----------

function syncMarker(sync) {
  const beat = sync && sync.beat ? sync.beat.seq : null;
  const cycle = sync && sync.cycle ? sync.cycle.id : null;
  return `${beat}:${cycle}`;
}

async function pollSync() {
  const sync = await fetchJSON("/api/sync");
  const marker = syncMarker(sync);
  if (state.lastSyncMarker && marker !== state.lastSyncMarker) {
    await refreshMeta();
    await refreshAll();
  }
  state.lastSyncMarker = marker;
  if (sync.beat) {
    $("sync-label").textContent = "синхронизация: " + fmtDateTime(sync.beat.at);
  }
}

function startSyncPolling() {
  if (state.syncPollTimer) return;
  $("live-dot").className = "dot dot-on";
  $("live-label").textContent = "проверка каждые 30 с";
  pollSync().catch(console.error);
  state.syncPollTimer = setInterval(() => pollSync().catch(console.error), 30000);
}

function stopSyncPolling() {
  if (!state.syncPollTimer) return;
  clearInterval(state.syncPollTimer);
  state.syncPollTimer = null;
}

// Returning to the dashboard (window regains focus or the tab becomes
// visible) re-reads the data so displayed stats are current without a manual
// reload. `pollSync` hits the lightweight /api/sync endpoint and only fires a
// full refresh when the data actually changed, so an unchanged return costs a
// single request. focus and visibilitychange can both fire for one return; a
// short debounce coalesces them into one check.
function refreshOnReturn() {
  if (document.visibilityState === "hidden") return;
  if (state.focusSyncTimer) return;
  state.focusSyncTimer = setTimeout(() => {
    state.focusSyncTimer = null;
    pollSync().catch(console.error);
  }, 200);
}

function watchWindowFocus() {
  document.addEventListener("visibilitychange", refreshOnReturn);
  window.addEventListener("focus", refreshOnReturn);
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onopen = () => {
    stopSyncPolling();
    $("live-dot").className = "dot dot-on";
    $("live-label").textContent = "live";
  };
  source.onerror = () => {
    // Passenger/LiteSpeed may buffer WSGI streams. Polling is the supported
    // public-host fallback; EventSource keeps retrying for local FastAPI.
    startSyncPolling();
  };
  source.addEventListener("update", (event) => {
    const sync = JSON.parse(event.data);
    state.lastSyncMarker = syncMarker(sync);
    refreshMeta()
      .then(refreshAll)
      .then(() => {
        // The live phase lands between poll_cycles rows, so the beat
        // timestamp is fresher than summary.last_cycle written by refreshAll.
        if (sync.beat) {
          $("sync-label").textContent = "синхронизация: " + fmtDateTime(sync.beat.at);
        }
      })
      .catch(console.error);
  });
}

// ---------- boot ----------

async function setupSession() {
  try {
    const auth = await fetchJSON("/api/session");
    state.csrf = auth.csrf;
    const button = $("logout-button");
    button.hidden = false;
    button.addEventListener("click", async () => {
      const response = await fetch("/logout", {
        method: "POST",
        credentials: "same-origin",
        body: new URLSearchParams({ csrf: state.csrf }),
      });
      if (!response.ok) throw new Error(`logout → HTTP ${response.status}`);
      location.assign("/login");
    });
  } catch (error) {
    // The local FastAPI app intentionally has no login endpoint. A 404 here
    // means local-only mode, not a dashboard failure.
    if (!String(error.message).includes("HTTP 404")) throw error;
  }
}

async function refreshMeta() {
  const meta = await fetchJSON("/api/meta");
  state.lastDate = meta.date_span.last;
  populateMultiSelect("filter-project", meta.projects, (p) => p.id, (p) => p.title, state.projects);
  populateMultiSelect("filter-agent", meta.agents, (a) => a.id, (a) => a.name, state.agents);
  populateMultiSelect("filter-model", meta.models, (m) => m, (m) => m, state.models);
}

function populateMultiSelect(id, items, valueOf, labelOf, selected) {
  const select = $(id);
  while (select.options.length) select.remove(0);
  const all = document.createElement("option");
  all.value = "";
  all.textContent = id === "filter-project" ? "Все проекты"
    : id === "filter-agent" ? "Все агенты" : "Все модели";
  all.selected = !selected.length;
  select.appendChild(all);
  for (const item of items) {
    const option = document.createElement("option");
    option.value = valueOf(item);
    option.textContent = labelOf(item);
    option.selected = selected.includes(option.value);
    select.appendChild(option);
  }
}

function selectedValues(id) {
  return [...$(id).selectedOptions].map((option) => option.value).filter(Boolean);
}

function setSelectedValues(id, values) {
  for (const option of $(id).options) {
    option.selected = option.value ? values.includes(option.value) : !values.length;
  }
}

function syncFiltersToUrl() {
  const q = new URLSearchParams();
  for (const project of state.projects) q.append("project", project);
  for (const agent of state.agents) q.append("agent", agent);
  for (const model of state.models) q.append("model", model);
  if (state.days !== "30") q.set("days", state.days || "all");
  if (state.from) q.set("from", state.from);
  if (state.to) q.set("to", state.to);
  if (state.group !== "model") q.set("group", state.group);
  const qs = q.toString();
  history.replaceState(null, "", qs ? "?" + qs : location.pathname);
}

function readFiltersFromUrl() {
  const q = new URLSearchParams(location.search);
  state.projects = q.getAll("project");
  state.agents = q.getAll("agent");
  state.models = q.getAll("model");
  if (q.has("days")) state.days = q.get("days") === "all" ? "" : q.get("days");
  state.from = q.get("from") || "";
  state.to = q.get("to") || "";
  if (state.from || state.to) state.days = "custom";
  if (q.has("group")) state.group = q.get("group");
  setSelectedValues("filter-project", state.projects);
  setSelectedValues("filter-agent", state.agents);
  setSelectedValues("filter-model", state.models);
  $("filter-period").value = state.days;
  $("filter-group").value = state.group;
  $("filter-from").value = state.from;
  $("filter-to").value = state.to;
}

async function boot() {
  await setupSession();
  await refreshMeta();
  readFiltersFromUrl();
  $("filter-project").addEventListener("change", () => {
    state.projects = selectedValues("filter-project");
    syncFiltersToUrl();
    refreshAll().catch(console.error);
  });
  $("filter-agent").addEventListener("change", () => {
    state.agents = selectedValues("filter-agent");
    syncFiltersToUrl();
    refreshAll().catch(console.error);
  });
  $("filter-model").addEventListener("change", () => {
    state.models = selectedValues("filter-model");
    syncFiltersToUrl();
    refreshAll().catch(console.error);
  });
  $("filter-period").addEventListener("change", (e) => {
    state.days = e.target.value;
    if (state.days !== "custom") {
      state.from = "";
      state.to = "";
      $("filter-from").value = "";
      $("filter-to").value = "";
    }
    syncFiltersToUrl();
    refreshAll().catch(console.error);
  });
  for (const id of ["filter-from", "filter-to"]) {
    $(id).addEventListener("change", () => {
      state.from = $("filter-from").value;
      state.to = $("filter-to").value;
      state.days = "custom";
      $("filter-period").value = "custom";
      syncFiltersToUrl();
      refreshAll().catch(console.error);
    });
  }
  $("filter-group").addEventListener("change", (e) => {
    state.group = e.target.value;
    syncFiltersToUrl();
    refreshAll().catch(console.error);
  });
  await refreshAll();
  connectEvents();
  watchWindowFocus();
}

boot().catch((err) => {
  console.error(err);
  $("live-label").textContent = "ошибка загрузки: " + err.message;
});
