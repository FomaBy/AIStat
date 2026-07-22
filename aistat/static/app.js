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

// Colors are assigned by typed entity identity (entity_type + stable id), never
// by an element's position in the current array, so one entity keeps its color
// across every chart, metric, legend, series and tooltip — and across reorder,
// live refresh and model/agent/project switches (FAN-1237). model, agent and
// project are separate identity spaces: the same label in two dimensions is not
// the same entity unless explicitly anchored.
//
// A missing/unknown identity (unattributed agent, model-less run) gets one
// explicit sentinel color instead of borrowing a palette slot.
const UNATTRIBUTED_COLOR = "#cbd5e1";

// Fixed, human-assigned colors that must never drift with the data. Fable is
// red in every model view regardless of ordering (FAN-1237 acceptance).
const ENTITY_ANCHORS = {
  model: { "claude-fable-5": "#ef4444" },
};

// A single-series chart (efficiency over time) plots one metric, not an entity,
// so it uses a stable brand color rather than the identity registry.
const SINGLE_SERIES_COLOR = "#4f6df5";

// typed key ("type\0id") -> color, assigned once and cached for the session.
// A canonical batch is registered before charts render. That lets colliding
// identities claim distinct free slots without making the mapping depend on
// the order in which a chart happens to encounter them. Existing assignments
// are deliberately retained when meta refreshes add new identities.
const colorRegistry = { byKey: new Map() };

// FNV-1a over the typed key: a stable, well-spread starting index into the
// palette that depends only on the entity's identity.
function hashKey(key) {
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function normalizeEntityId(id) {
  if (id == null) return "";
  return String(id).trim();
}

function entityKey(type, id) {
  return String(type || "").trim() + "\u0000" + normalizeEntityId(id);
}

function fallbackColorForKey(key, reserved, used) {
  const start = hashKey(key + "\u0000palette") % PALETTE.length;

  // Prefer an unused color, skipping anchors. The first loop is what gives a
  // finite batch exact uniqueness up to its effective capacity.
  for (let i = 0; i < PALETTE.length; i++) {
    const candidate = PALETTE[(start + i) % PALETTE.length];
    if (!reserved.has(candidate) && !used.has(candidate)) return candidate;
  }

  // The palette is finite. After exhaustion, keep the same deterministic
  // probe, but allow a repeat while still protecting anchor colors.
  for (let i = 0; i < PALETTE.length; i++) {
    const candidate = PALETTE[(start + i) % PALETTE.length];
    if (!reserved.has(candidate)) return candidate;
  }
  return UNATTRIBUTED_COLOR;
}

function registerEntityColors(type, ids) {
  const normalizedType = String(type || "").trim();
  if (!normalizedType) return;

  const anchors = ENTITY_ANCHORS[normalizedType] || {};
  const reserved = new Set(Object.values(anchors));
  for (const [id, color] of Object.entries(anchors)) {
    colorRegistry.byKey.set(entityKey(normalizedType, id), color);
  }

  const canonicalIds = [...new Set((Array.isArray(ids) ? ids : [])
    .map(normalizeEntityId).filter(Boolean))].sort();
  const used = new Set();
  for (const [key, color] of colorRegistry.byKey) {
    if (key.startsWith(normalizedType + "\u0000") && !reserved.has(color)) {
      used.add(color);
    }
  }

  for (const id of canonicalIds) {
    const key = entityKey(normalizedType, id);
    if (colorRegistry.byKey.has(key)) continue;
    const color = Object.prototype.hasOwnProperty.call(anchors, id)
      ? anchors[id] : fallbackColorForKey(key, reserved, used);
    colorRegistry.byKey.set(key, color);
    if (!reserved.has(color)) used.add(color);
  }
}

function entityColor(type, id) {
  const normalizedType = String(type || "").trim();
  const normalizedId = normalizeEntityId(id);
  if (!normalizedId) return UNATTRIBUTED_COLOR;
  const key = entityKey(normalizedType, normalizedId);
  const cached = colorRegistry.byKey.get(key);
  if (cached) return cached;
  // Production paths register their complete meta batch first. This small
  // compatibility fallback also keeps direct probes safe without assigning a
  // sentinel or bypassing anchor handling.
  registerEntityColors(normalizedType, [normalizedId]);
  return colorRegistry.byKey.get(key) || UNATTRIBUTED_COLOR;
}

// Every value the period/group selects can hold; URL parameters outside these
// sets are user input errors and must be dropped, not applied (FAN-1255).
const PERIOD_VALUES = new Set(["7", "14", "30", "90", "", "custom"]);
const GROUP_VALUES = new Set(["model", "agent", "project"]);

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
  connection: null,
  connectionSupported: true,
  connectionBusy: false,
  connectionReplacing: false,
  connectionPollTimer: null,
  connectionPollGeneration: 0,
  connectionPollDeadline: 0,
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

// Agent work-time comes from the API as whole seconds (the canonical value);
// show it as a readable duration with the two most significant units.
function fmtDuration(seconds) {
  if (seconds == null) return "—";
  let s = Math.round(seconds);
  if (s <= 0) return "0 с";
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d > 0) return h > 0 ? `${d} дн ${h} ч` : `${d} дн`;
  if (h > 0) return m > 0 ? `${h} ч ${m} мин` : `${h} ч`;
  if (m > 0) return s > 0 ? `${m} мин ${s} с` : `${m} мин`;
  return `${s} с`;
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

// ---------- personal Multica connection cabinet ----------

const CONNECTION_POLL_INTERVAL_MS = 1500;
const CONNECTION_POLL_TIMEOUT_MS = 60000;
const CONNECTION_PENDING_STATES = new Set([
  "pending", "replacement_pending", "revocation_pending",
]);
const CONNECTION_STATES = new Set([
  "none", "disabled", "pending", "replacement_pending", "active",
  "error", "revocation_pending", "revoked",
]);
const CONNECTION_STATE_LABELS = {
  none: "не подключён",
  disabled: "недоступно",
  pending: "ожидает синхронизации",
  replacement_pending: "замена ожидает синхронизации",
  active: "подключено",
  error: "ошибка синхронизации",
  revocation_pending: "отключение выполняется",
  revoked: "отозвано",
};
const CONNECTION_STATE_TITLES = {
  none: "Multica не подключён",
  disabled: "Подключение Multica недоступно",
  pending: "Подключение ожидает синхронизации",
  replacement_pending: "Замена PAT ожидает синхронизации",
  active: "Multica подключён",
  error: "Не удалось синхронизировать Multica",
  revocation_pending: "Отключение Multica выполняется",
  revoked: "Подключение Multica отозвано",
};
// Every worker-reported status is an internal English allowlist code, never a
// user-facing message. The cabinet renders a clear, actionable Russian message
// for each known code and falls back to a generic one for anything else, so an
// unexpected value can never be echoed verbatim. Workspace-label failures name
// the label the owner typed — that label is host-side cabinet data
// (workspace_label), not anything crossing the worker boundary — turning the
// opaque "подключил, но не работает" into a diagnosable "рабочее пространство
// «X» не найдено".
const CONNECTION_ERROR_MESSAGES = {
  "worker collection failed":
    "Сбор статистики завершился с ошибкой. Попробуйте позже или переподключите PAT.",
  "authentication with the connection's token failed":
    "PAT не прошёл авторизацию в Multica. Проверьте токен и переподключите его.",
  "official CLI login failed for the connection":
    "Не удалось войти в Multica с этим PAT. Проверьте токен и переподключите его.",
  "could not list the connection's workspaces":
    "Не удалось получить список рабочих пространств для этого PAT. Попробуйте переподключить его.",
  "the connection's token has no accessible workspace":
    "У этого PAT нет доступных рабочих пространств. Проверьте права токена в Multica.",
  "the connection's token has multiple workspaces but none was selected":
    "У PAT несколько рабочих пространств — укажите нужное в поле «Рабочее пространство» и переподключите PAT.",
  "workspace was not selected before polling":
    "Рабочее пространство для подключения не выбрано. Переподключите PAT, указав название рабочего пространства.",
  "could not read the connection's workspace data":
    "Не удалось прочитать данные рабочего пространства. Попробуйте позже или переподключите PAT.",
  "connection collection failed":
    "Сбор статистики завершился с ошибкой. Попробуйте позже или переподключите PAT.",
  "polling the connection's data failed":
    "Не удалось загрузить данные из Multica. Попробуйте позже.",
  "publishing the connection's snapshot failed":
    "Не удалось сохранить полученную статистику. Попробуйте позже.",
  "connection was revoked":
    "Подключение было отозвано. Подключите новый PAT, если хотите продолжить синхронизацию.",
  "poll source failed":
    "Источник данных Multica ответил ошибкой. Попробуйте позже.",
  "issue details synchronization failed":
    "Не удалось синхронизировать детали задач. Попробуйте позже.",
};

// Codes whose message names the workspace label the owner typed.
const CONNECTION_WORKSPACE_ERRORS = {
  "the connection's workspace could not be resolved": (ws) =>
    "Рабочее пространство " + ws + " не найдено у этого PAT. Проверьте точное "
    + "название рабочего пространства в Multica и переподключите PAT.",
  "the connection's workspace label is ambiguous": (ws) =>
    "Название рабочего пространства " + ws + " подходит сразу нескольким. "
    + "Уточните точное название и переподключите PAT.",
};

const CONNECTION_ERROR_FALLBACK =
  "Синхронизация Multica завершилась с ошибкой. Попробуйте подключить PAT ещё раз.";

function connectionSupportedSurface() {
  const cabinet = $("connection-cabinet");
  return cabinet && !cabinet.hidden && state.connectionSupported;
}

function clearConnectionToken() {
  const input = $("connection-token");
  if (!input) return;
  // Clear before waiting for the response. The value is never put in a URL,
  // history entry, storage area, log message or rendered error.
  input.value = "";
  input.defaultValue = "";
  input.removeAttribute("value");
}

function safeConnectionError(value, label) {
  const code = typeof value === "string" ? value.trim() : "";
  const workspace = typeof label === "string" ? label.trim() : "";
  const named = workspace ? "«" + workspace + "»" : "указанное";
  const withLabel = CONNECTION_WORKSPACE_ERRORS[code];
  if (withLabel) return withLabel(named);
  return CONNECTION_ERROR_MESSAGES[code] || CONNECTION_ERROR_FALLBACK;
}

function connectionRequestError(status) {
  const error = new Error("connection request failed");
  error.status = status;
  return error;
}

async function readConnectionResponse(response) {
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try { payload = JSON.parse(raw); } catch (_) { payload = {}; }
  }
  if (response.status === 401) {
    const next = encodeURIComponent(location.pathname + location.search);
    location.assign(`/login?next=${next}`);
    throw connectionRequestError(401);
  }
  if (!response.ok) throw connectionRequestError(response.status);
  return payload;
}

async function connectionRequest(path, options) {
  const response = await fetch(path, Object.assign({
    credentials: "same-origin",
    headers: { "Accept": "application/json" },
  }, options || {}));
  return readConnectionResponse(response);
}

function showConnectionOperationError(error) {
  const note = $("connection-form-error");
  if (!note) return;
  const messages = {
    400: "Сессия устарела или запрос не прошёл проверку. Обновите страницу.",
    401: "Сессия истекла. Войдите снова.",
    404: "Подключение не найдено. Обновите страницу.",
    422: "Проверьте PAT и метку workspace.",
    429: "Слишком много попыток. Повторите позже.",
    503: "Подключение сейчас недоступно. Попробуйте позже.",
  };
  note.textContent = messages[error && error.status]
    || "Не удалось изменить подключение. Попробуйте позже.";
  note.hidden = false;
}

function clearConnectionOperationError() {
  const note = $("connection-form-error");
  if (!note) return;
  note.textContent = "";
  note.hidden = true;
}

function setConnectionBusy(busy) {
  state.connectionBusy = busy;
  const form = $("connection-form");
  const submit = $("connection-submit");
  const token = $("connection-token");
  const label = $("connection-workspace-label");
  if (form) form.setAttribute("aria-busy", busy ? "true" : "false");
  if (submit) {
    submit.disabled = busy || (state.connection && state.connection.status === "disabled");
    submit.setAttribute("aria-busy", busy ? "true" : "false");
  }
  if (token) token.disabled = busy;
  if (label) label.disabled = busy;
  for (const id of ["connection-replace", "connection-disconnect", "connection-confirm-yes", "connection-confirm-no"]) {
    const button = $(id);
    if (button) button.disabled = busy;
  }
}

function connectionStatusMessage(status, data) {
  if (status === "error") {
    return safeConnectionError(
      data && data.last_sync_error, data && data.workspace_label
    );
  }
  const messages = {
    none: "Подключите Multica, чтобы загрузить вашу статистику.",
    disabled: "Администратор временно отключил ручное подключение.",
    pending: "PAT принят. Ожидаем подтверждение защищённого worker-канала.",
    replacement_pending: "Новая PAT принята. Ожидаем подтверждение замены.",
    active: "Статистика будет обновляться автоматически.",
    revocation_pending: "Запрос на отключение принят. Ожидаем удаления доступа worker-каналом.",
    revoked: "Доступ удалён. Отзовите PAT в настройках Multica.",
  };
  return messages[status] || messages.none;
}

function stopConnectionPolling() {
  if (state.connectionPollTimer) {
    clearTimeout(state.connectionPollTimer);
    state.connectionPollTimer = null;
  }
  state.connectionPollGeneration += 1;
  state.connectionPollDeadline = 0;
}

function renderConnection(data) {
  const rawStatus = data && typeof data.status === "string" ? data.status : "none";
  const status = CONNECTION_STATES.has(rawStatus) ? rawStatus : "none";
  const normalized = Object.assign({}, data || {}, { status });
  state.connection = normalized;

  const badge = $("connection-status-badge");
  const title = $("connection-status-title");
  const message = $("connection-status-message");
  if (badge) {
    badge.dataset.state = status;
    badge.textContent = CONNECTION_STATE_LABELS[status];
  }
  if (title) title.textContent = CONNECTION_STATE_TITLES[status];
  if (message) message.textContent = connectionStatusMessage(status, normalized);

  const details = $("connection-details");
  if (details) details.hidden = false;
  const workspaceDetail = $("connection-workspace-detail");
  const workspace = $("connection-workspace");
  const workspaceLabel = typeof normalized.workspace_label === "string"
    ? normalized.workspace_label : "";
  if (workspace) workspace.textContent = workspaceLabel;
  if (workspaceDetail) workspaceDetail.hidden = !workspaceLabel;
  const syncedDetail = $("connection-synced-detail");
  const synced = $("connection-synced-at");
  if (synced) synced.textContent = normalized.last_synced_at
    ? fmtDateTime(new Date(Number(normalized.last_synced_at) * 1000).toISOString()) : "";
  if (syncedDetail) syncedDetail.hidden = !normalized.last_synced_at;

  const empty = $("connection-empty");
  if (empty) {
    empty.textContent = status === "revoked"
      ? "Подключение отозвано. Подключите новый PAT, если хотите продолжить синхронизацию."
      : "У вас пока нет подключения. Нажмите «Подключить» и вставьте PAT из Multica.";
    empty.hidden = !["none", "revoked"].includes(status);
  }
  const error = $("connection-error");
  if (error) {
    error.textContent = status === "error"
      ? safeConnectionError(normalized.last_sync_error, normalized.workspace_label) : "";
    error.hidden = status !== "error";
  }
  const advice = $("connection-advice");
  if (advice) advice.hidden = !["revoked", "revocation_pending"].includes(status);

  const form = $("connection-form");
  const actions = $("connection-actions");
  const showForm = status === "none" || status === "disabled" || status === "revoked"
    || ((status === "active" || status === "error") && state.connectionReplacing);
  if (form) form.hidden = !showForm;
  if (actions) actions.hidden = !((status === "active" || status === "error") && !state.connectionReplacing);
  const submit = $("connection-submit");
  if (submit) {
    submit.textContent = (status === "active" || status === "error" || state.connectionReplacing)
      ? "Заменить PAT" : "Подключить";
    submit.disabled = state.connectionBusy || status === "disabled";
  }
  const confirm = $("connection-confirm");
  if (confirm && status !== "active" && status !== "error") confirm.hidden = true;
  if (!CONNECTION_PENDING_STATES.has(status)) stopConnectionPolling();
}

async function refreshConnection() {
  if (!connectionSupportedSurface()) return null;
  try {
    const data = await connectionRequest("/api/connection");
    renderConnection(data);
    return state.connection;
  } catch (error) {
    if (error && error.status === 404) {
      state.connectionSupported = false;
      const cabinet = $("connection-cabinet");
      if (cabinet) cabinet.hidden = true;
      stopConnectionPolling();
      return null;
    }
    const message = $("connection-status-message");
    if (message) message.textContent = "Статус подключения временно недоступен.";
    return null;
  }
}

function startConnectionPolling(status) {
  stopConnectionPolling();
  if (!CONNECTION_PENDING_STATES.has(status)) return;
  const generation = state.connectionPollGeneration;
  state.connectionPollDeadline = Date.now() + CONNECTION_POLL_TIMEOUT_MS;
  const tick = async () => {
    if (generation !== state.connectionPollGeneration) return;
    if (Date.now() >= state.connectionPollDeadline) {
      const error = $("connection-error");
      if (error) {
        error.textContent = "Синхронизация ещё не завершена. Обновите статус позже.";
        error.hidden = false;
      }
      state.connectionPollTimer = null;
      return;
    }
    const current = await refreshConnection();
    if (generation !== state.connectionPollGeneration || !current
        || !CONNECTION_PENDING_STATES.has(current.status)) return;
    state.connectionPollTimer = setTimeout(tick, CONNECTION_POLL_INTERVAL_MS);
  };
  state.connectionPollTimer = setTimeout(tick, 0);
}

async function postConnection(path, body) {
  const headers = {
    "Accept": "application/json",
    "X-CSRF-Token": state.csrf || "",
    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
  };
  const request = connectionRequest(path, {
    method: "POST",
    headers,
    body: new URLSearchParams(body || {}),
  });
  return request;
}

async function submitConnection(event) {
  event.preventDefault();
  if (state.connectionBusy || !connectionSupportedSurface()) return;
  const tokenInput = $("connection-token");
  const labelInput = $("connection-workspace-label");
  const token = tokenInput ? tokenInput.value : "";
  const workspaceLabel = labelInput ? labelInput.value : "";
  if (!token.trim()) {
    const error = $("connection-form-error");
    if (error) {
      error.textContent = "Введите PAT для подключения.";
      error.hidden = false;
    }
    return;
  }
  clearConnectionOperationError();
  setConnectionBusy(true);
  const body = { token };
  if (workspaceLabel.trim()) body.workspace_label = workspaceLabel;
  // The input is cleared immediately after fetch receives its body, before
  // any response or error can be rendered.
  const request = postConnection("/api/connection", body);
  clearConnectionToken();
  try {
    const data = await request;
    state.connectionReplacing = false;
    renderConnection(data);
    startConnectionPolling(state.connection.status);
  } catch (error) {
    showConnectionOperationError(error);
  } finally {
    setConnectionBusy(false);
  }
}

async function revokeConnection() {
  if (state.connectionBusy || !connectionSupportedSurface()) return;
  clearConnectionOperationError();
  setConnectionBusy(true);
  const confirm = $("connection-confirm");
  if (confirm) confirm.hidden = true;
  try {
    const data = await postConnection("/api/connection/revoke", {});
    state.connectionReplacing = false;
    renderConnection(data);
    startConnectionPolling(state.connection.status);
  } catch (error) {
    showConnectionOperationError(error);
  } finally {
    setConnectionBusy(false);
  }
}

function setupConnection() {
  const cabinet = $("connection-cabinet");
  if (!cabinet) return;
  $("connection-form").addEventListener("submit", submitConnection);
  $("connection-replace").addEventListener("click", () => {
    state.connectionReplacing = true;
    clearConnectionOperationError();
    renderConnection(state.connection || { status: "none" });
    $("connection-token").focus();
  });
  $("connection-disconnect").addEventListener("click", () => {
    if (!state.connectionBusy) $("connection-confirm").hidden = false;
  });
  $("connection-confirm-no").addEventListener("click", () => {
    $("connection-confirm").hidden = true;
  });
  $("connection-confirm-yes").addEventListener("click", revokeConnection);
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

// A datetime-local value the API accepts: full date plus minutes (optionally
// seconds) that names a real calendar instant. Chrome's Date.parse falls
// back to a lenient parser that rolls impossible parts over (2026-02-30
// parses as March 2) instead of returning NaN, so validity is judged by
// re-reading each part from a UTC round-trip, never by the parser (FAN-1269).
function isValidDateTimeLocal(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value || "");
  if (!match) return false;
  const [year, month, day, hour, minute, second] =
    match.slice(1).map((part) => Number(part || 0));
  const utc = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  return utc.getUTCFullYear() === year && utc.getUTCMonth() === month - 1 &&
    utc.getUTCDate() === day && utc.getUTCHours() === hour &&
    utc.getUTCMinutes() === minute && utc.getUTCSeconds() === second;
}

// The API rejects from >= to, so an unordered pair must never become active
// state; one-sided (half-open) ranges stay allowed.
function rangeIsOrdered(from, to) {
  return !from || !to || Date.parse(utcDateTime(from)) < Date.parse(utcDateTime(to));
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

function stackedDailyConfig(rows, valueOf, valueFmt, group) {
  const dates = [...new Set(rows.map((r) => r.date))].sort();
  // Series are grouped by stable typed identity (r.id), not by display label:
  // two entities that share a label stay distinct, and each keeps its registry
  // color no matter how this metric happens to order them (FAN-1237). Sorting
  // by total still controls stack/legend order; it no longer drives color.
  const idTotals = new Map();
  const idLabel = new Map();
  for (const r of rows) {
    idTotals.set(r.id, (idTotals.get(r.id) || 0) + (valueOf(r) || 0));
    if (!idLabel.has(r.id)) idLabel.set(r.id, r.key);
  }
  const ids = [...idTotals.keys()].sort((a, b) => idTotals.get(b) - idTotals.get(a));
  const byDateId = new Map(rows.map((r) => [r.date + "\u0000" + r.id, r]));
  const datasets = ids.map((id) => ({
    label: idLabel.get(id),
    data: dates.map((d) => {
      const r = byDateId.get(d + "\u0000" + id);
      return r ? valueOf(r) || 0 : 0;
    }),
    backgroundColor: entityColor(group, id),
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
  const group = daily.group;
  upsertChart("chart-daily-tokens", stackedDailyConfig(rows, (r) => r.total_tokens, fmtTokens, group));
  upsertChart("chart-daily-cost", stackedDailyConfig(rows, (r) => r.cost_usd, fmtUSD, group));
}

function renderAgentsChart(agents) {
  const labels = agents.map((a) => (a.estimated ? "≈ " : "") + a.name);
  upsertChart("chart-agents", {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data: agents.map((a) => a.total_tokens),
        backgroundColor: agents.map((a) => entityColor("agent", a.agent_id)),
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
      <td class="num">${a.runs}</td>
      <td class="num">${fmtDuration(a.work_seconds)}</td>`;
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
        backgroundColor: projects.map((p) => entityColor("project", p.project_id)),
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
    const est = it.estimated ? "≈ " : "";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${esc(it.identifier || it.issue_id)}</td>
      <td class="ellipsis" title="${esc(it.title || "")}">${esc(it.title || "")}</td>
      <td>${esc(it.project || "—")}</td>
      <td>${esc((it.agents || []).join(", ") || "—")}</td>
      <td><span class="chip chip-${esc(it.status)}">${esc(it.status)}</span></td>
      <td class="num">${est}${fmtNum(it.story_points)}</td>
      <td class="num">${est}${fmtTokens(it.total_tokens)}</td>
      <td class="num">${est}${fmtTokens(it.tokens_per_sp)}</td>`;
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

function efficiencyBarConfig(rows, type) {
  return {
    type: "bar",
    data: {
      labels: rows.map((r) => r.label),
      datasets: [{
        data: rows.map((r) => r.tokens_per_sp),
        backgroundColor: rows.map((r) => entityColor(type, r.key)),
        borderWidth: 0,
        maxBarThickness: 36,
      }],
    },
    options: {
      indexAxis: "y",
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => fmtTokens(ctx.parsed.x) + " токенов / SP" } },
      },
      scales: { x: { ticks: { callback: (v) => fmtTokens(v) } } },
    },
  };
}

// The keyboard/screen-reader alternative for one efficiency chart: the same
// label → tokens/SP pairs the chart draws, with a gap shown as an explicit —.
function renderBreakdownTable(id, rows) {
  const tbody = $(id).querySelector("tbody");
  tbody.innerHTML = "";
  if (!rows.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="2" class="note">Нет данных за выбранный период и фильтры.</td>`;
    tbody.appendChild(tr);
    return;
  }
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="wrap">${esc(r.label)}</td>
      <td class="num">${r.tokens_per_sp == null ? "—" : fmtTokens(r.tokens_per_sp)}</td>`;
    tbody.appendChild(tr);
  }
}

function renderEfficiencyBreakdown(data) {
  const agents = (data && data.agents) || [];
  const models = (data && data.models) || [];
  const time = (data && data.time) || { granularity: "day", rows: [] };
  const rows = time.rows || [];
  const granularity = time.granularity === "hour" ? "часам UTC" : "дням UTC";
  $("efficiency-time-title").innerHTML =
    `Эффективность во времени <span class="est-mark">≈ по ${granularity} · токены / SP · меньше — лучше</span>`;
  $("efficiency-time-data-label").textContent =
    time.granularity === "hour" ? "Час (UTC)" : "День (UTC)";
  // A chart with nothing drawable is an unexplained blank canvas — cover it
  // with the no-data message instead (FAN-1242).
  const hasData = (rs) => rs.some((r) => r.tokens_per_sp != null);
  $("empty-efficiency-agents").hidden = hasData(agents);
  $("empty-efficiency-models").hidden = hasData(models);
  $("empty-efficiency-time").hidden = hasData(rows);
  renderBreakdownTable("table-efficiency-agents-data", agents);
  renderBreakdownTable("table-efficiency-models-data", models);
  renderBreakdownTable("table-efficiency-time-data", rows);
  upsertChart("chart-efficiency-agents", efficiencyBarConfig(agents, "agent"));
  upsertChart("chart-efficiency-models", efficiencyBarConfig(models, "model"));
  upsertChart("chart-efficiency-time", {
    type: "line",
    data: {
      labels: rows.map((r) => r.label),
      datasets: [{
        label: "Токены / SP",
        // A bucket without attributable SP stays null so the line breaks at
        // the gap (spanGaps: false) instead of inventing a zero.
        data: rows.map((r) => (r.tokens_per_sp == null ? null : r.tokens_per_sp)),
        borderColor: SINGLE_SERIES_COLOR,
        backgroundColor: SINGLE_SERIES_COLOR,
        pointRadius: 3,
        pointHoverRadius: 5,
        tension: 0.2,
        spanGaps: false,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (ctx) => fmtTokens(ctx.parsed.y) + " токенов / SP" } },
      },
      scales: { y: { ticks: { callback: (v) => fmtTokens(v) } } },
    },
  });
}

function renderSummary(s) {
  const est = s.estimated ? "≈ " : "";
  $("card-tokens").textContent = est + fmtTokens(s.total_tokens);
  $("card-tokens-sub").textContent =
    `ввод ${fmtTokens(s.input_tokens)} · вывод ${fmtTokens(s.output_tokens)} · кеш ${fmtTokens(s.cache_read_tokens + s.cache_write_tokens)}`;
  $("card-cost").textContent = est + fmtUSD(s.cost_usd);
  $("card-cost-sub").textContent = s.has_unpriced ? "есть неоценённые модели!" : "по официальным тарифам";
  $("card-credits").textContent = est + fmtCredits(s.cost_credits);
  // SP and token efficiency are run-share attributions under agent/model/
  // period filters; their flags are separate from the token-card `estimated`.
  const spEst = s.sp_estimated ? "≈ " : "";
  $("card-sp").textContent = spEst + fmtNum(s.story_points);
  $("card-sp-sub").textContent = `задач: ${s.issues} · с SP: ${s.issues_with_sp}`;
  const effEst = s.efficiency_estimated ? "≈ " : "";
  $("card-eff").textContent = s.tokens_per_sp == null ? "—" : effEst + fmtTokens(s.tokens_per_sp);
  // Cost/weighted efficiency lean on model attribution + pricing, so they are
  // always estimates (≈); a trailing * marks unpriced tokens in the mix.
  const effStar = s.efficiency_has_unpriced ? " *" : "";
  $("card-cost-eff").textContent =
    s.cost_per_sp == null ? "—" : "≈ " + fmtUSDFine(s.cost_per_sp) + effStar;
  $("card-weighted-eff").textContent =
    s.weighted_efficiency == null ? "—" : "≈ " + fmtUSDFine(s.weighted_efficiency) + effStar;
  // Agent participation and total agent-time — any eligible run over the
  // window, so they stand apart from the SP/usage-eligible efficiency cards.
  $("card-agent-count").textContent = fmtNum(s.agent_count == null ? 0 : s.agent_count);
  $("card-agent-time").textContent = fmtDuration(s.agent_work_seconds);
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
  const [summary, daily, agents, projects, efficiency, modelEfficiency, efficiencyBreakdown, health] = await Promise.all([
    fetchJSON("/api/summary" + query()),
    fetchJSON("/api/daily" + query({ group: state.group })),
    fetchJSON("/api/agents" + query()),
    fetchJSON("/api/projects" + query()),
    fetchJSON("/api/efficiency" + query({ limit: "15" })),
    fetchJSON("/api/model-efficiency" + query()),
    fetchJSON("/api/efficiency-breakdown" + query()),
    fetchJSON("/api/health"),
  ]);
  // The ≈-note legends the token-attribution markers. Drive it from the real
  // API flags, not merely from the presence of a filter: a unique-agent
  // whole-day slice is exact, so it must stay hidden (FAN-1253). Show it only
  // when a filter/group is active AND the token, daily or an agent value is
  // actually estimated.
  const filterActive = Boolean(
    state.projects.length || state.agents.length || state.models.length ||
    state.group !== "model" || state.from || state.to
  );
  const tokensEstimated = Boolean(
    summary.estimated || daily.estimated ||
    agents.agents.some((a) => a.estimated)
  );
  $("estimate-note").hidden = !(filterActive && tokensEstimated);
  renderSummary(summary);
  renderDaily(daily);
  renderAgentsChart(agents.agents);
  renderAgentsTable(agents.agents);
  renderProjects(projects.projects);
  renderEfficiency(efficiency.issues);
  renderModelEfficiency(modelEfficiency);
  renderEfficiencyBreakdown(efficiencyBreakdown);

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
  registerEntityColors("project", meta.projects.map((project) => project.id));
  registerEntityColors("agent", meta.agents.map((agent) => agent.id));
  registerEntityColors("model", meta.models);
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

function showFilterError(message) {
  const note = $("filter-error");
  note.textContent = message;
  note.hidden = false;
}

function clearFilterError() {
  $("filter-error").hidden = true;
  $("filter-error").textContent = "";
}

// URL state is user input: a hand-edited or truncated link must not strand
// the dashboard on a 422 with every card at "—" (FAN-1255). Runs after
// /api/meta filled the selects and before the first full load: invalid
// parts are dropped, the URL is rewritten to the surviving state and a
// visible note says what was reset.
function readFiltersFromUrl() {
  const q = new URLSearchParams(location.search);
  const dropped = [];
  const keepKnown = (param, selectId) => {
    const values = q.getAll(param);
    const known = new Set([...$(selectId).options].map((o) => o.value));
    const kept = values.filter((v) => v && known.has(v));
    if (kept.length !== values.length) dropped.push(param);
    return kept;
  };
  state.projects = keepKnown("project", "filter-project");
  state.agents = keepKnown("agent", "filter-agent");
  state.models = keepKnown("model", "filter-model");
  if (q.has("days")) {
    const days = q.get("days") === "all" ? "" : q.get("days");
    if (PERIOD_VALUES.has(days)) state.days = days;
    else dropped.push("days");
  }
  const from = q.get("from") || "";
  const to = q.get("to") || "";
  state.from = isValidDateTimeLocal(from) ? from : "";
  if (from && !state.from) dropped.push("from");
  state.to = isValidDateTimeLocal(to) ? to : "";
  if (to && !state.to) dropped.push("to");
  if (!rangeIsOrdered(state.from, state.to)) {
    state.from = "";
    state.to = "";
    dropped.push("диапазон from/to («С» должно быть раньше «По»)");
  }
  if (state.from || state.to) state.days = "custom";
  if (q.has("group")) {
    if (GROUP_VALUES.has(q.get("group"))) state.group = q.get("group");
    else dropped.push("group");
  }
  setSelectedValues("filter-project", state.projects);
  setSelectedValues("filter-agent", state.agents);
  setSelectedValues("filter-model", state.models);
  $("filter-period").value = state.days;
  $("filter-group").value = state.group;
  $("filter-from").value = state.from;
  $("filter-to").value = state.to;
  if (dropped.length) {
    syncFiltersToUrl();
    showFilterError("Некорректные параметры фильтров в ссылке сброшены: " +
      dropped.join(", ") + ". Показаны данные по оставшимся фильтрам.");
  } else {
    clearFilterError();
  }
}

// One unambiguous way back to the canonical dashboard: every filter returns
// to its default and the URL to bare "/".
function resetFilters() {
  state.projects = [];
  state.agents = [];
  state.models = [];
  state.days = "30";
  state.from = "";
  state.to = "";
  state.group = "model";
  setSelectedValues("filter-project", []);
  setSelectedValues("filter-agent", []);
  setSelectedValues("filter-model", []);
  $("filter-period").value = state.days;
  $("filter-from").value = "";
  $("filter-to").value = "";
  $("filter-group").value = state.group;
  clearFilterError();
  syncFiltersToUrl();
  refreshAll().catch(console.error);
}

async function boot() {
  await setupSession();
  setupConnection();
  const connection = await refreshConnection();
  if (connection) startConnectionPolling(connection.status);
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
      clearFilterError();
    }
    syncFiltersToUrl();
    refreshAll().catch(console.error);
  });
  for (const id of ["filter-from", "filter-to"]) {
    $(id).addEventListener("change", () => {
      const from = $("filter-from").value;
      const to = $("filter-to").value;
      if (!rangeIsOrdered(from, to)) {
        // A reverse/equal range never becomes active state: data, URL and a
        // future reload keep the last valid filters (FAN-1255).
        showFilterError("«С (UTC)» должно быть раньше «По (UTC)»; диапазон не применён.");
        return;
      }
      clearFilterError();
      state.from = from;
      state.to = to;
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
  $("filter-reset").addEventListener("click", resetFilters);
  await refreshAll();
  connectEvents();
  watchWindowFocus();
}

boot().catch((err) => {
  console.error(err);
  $("live-label").textContent = "ошибка загрузки: " + err.message;
});
