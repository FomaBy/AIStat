/* Product copy and locale state shared by the public and private surfaces. */
"use strict";

const I18N = (() => {
  const STORAGE_KEY = "aistat.locale";
  const messages = {
    appTitle: ["AIStat — статистика токенов Multica", "AIStat — Multica token statistics"],
    loginTitle: ["Вход — AIStat", "Sign in — AIStat"],
    closedTitle: ["Регистрация закрыта — AIStat", "Registration closed — AIStat"],
    language: ["Язык интерфейса", "Interface language"],
    ru: ["Русский", "Russian"], en: ["Английский", "English"],
    subtitle: ["токены · стоимость · эффективность агентов Multica", "tokens · cost · Multica agent efficiency"],
    loginSubtitle: ["Закрытая статистика использования Multica", "Private Multica usage statistics"],
    liveUpdates: ["Live-обновления", "Live updates"], connecting: ["подключение…", "connecting…"],
    sync: ["синхронизация", "sync"], logout: ["Выйти", "Sign out"],
    filters: ["Фильтры", "Filters"],
    projects: ["Проекты", "Projects"], agents: ["Агенты", "Agents"], models: ["Модели", "Models"],
    allProjects: ["Все проекты", "All projects"], allAgents: ["Все агенты", "All agents"], allModels: ["Все модели", "All models"],
    period: ["Период", "Period"], days: ["{count} дней", "{count} days"], allTime: ["Всё время", "All time"], customRange: ["Свой диапазон", "Custom range"],
    fromUtc: ["С (UTC)", "From (UTC)"], toUtc: ["По (UTC)", "To (UTC)"], grouping: ["Группировка графиков", "Chart grouping"],
    byModels: ["По моделям", "By models"], byAgents: ["По агентам", "By agents"], byProjects: ["По проектам", "By projects"],
    resetFilters: ["Сбросить фильтры", "Reset filters"], resetFiltersTitle: ["Вернуть все фильтры к значениям по умолчанию", "Restore all filters to their defaults"],
    estimateNote: ["≈ значения по агентам/моделям/проектам и неполным дням распределены по фактическим интервалам запусков задач.", "≈ values for agents, models, projects, and partial days are allocated by actual task-run intervals."],
    connectMultica: ["Подключить свой Multica", "Connect your Multica"],
    openConnection: ["Подключить Multica", "Connect Multica"], close: ["Закрыть", "Close"],
    openConnectionLabel: ["Открыть подключение Multica", "Open Multica connection"],
    connectionIntro: ["AIStat получает статистику из вашего Multica через защищённый worker-канал. PAT нужен только для подключения и не показывается после отправки.", "AIStat collects statistics from your Multica through a secure worker channel. The PAT is only used to connect and is not shown after submission."],
    notConnected: ["не подключён", "not connected"], multicaNotConnected: ["Multica не подключён", "Multica is not connected"],
    connectPrompt: ["Подключите Multica, чтобы загрузить вашу статистику.", "Connect Multica to load your statistics."], multicaHost: ["Хост Multica", "Multica host"], workspace: ["Workspace", "Workspace"], lastSync: ["Последняя синхронизация", "Last sync"],
    emptyConnection: ["У вас пока нет подключения. Нажмите «Подключить» и вставьте PAT из Multica.", "You do not have a connection yet. Click Connect and paste a Multica PAT."],
    workspaceLabel: ["Метка workspace", "Workspace label"], optional: ["необязательно", "optional"], officialHost: ["Официальный хост:", "Official host:"], connect: ["Подключить", "Connect"], replacePat: ["Заменить PAT", "Replace PAT"], disconnect: ["Отключить", "Disconnect"],
    disconnectQuestion: ["Отключить Multica?", "Disconnect Multica?"], disconnectWarning: ["AIStat удалит доступ к этому подключению. После подтверждения отзовите PAT в Multica.", "AIStat will remove access to this connection. Revoke the PAT in Multica after confirming."],
    confirmDisconnect: ["Да, отключить", "Yes, disconnect"], cancel: ["Отмена", "Cancel"], revokeAdvice: ["После отключения отзовите этот PAT в настройках Multica.", "Revoke this PAT in Multica settings after disconnecting."],
    totalTokens: ["Всего токенов", "Total tokens"], cost: ["Стоимость", "Cost"], credits: ["Кредиты", "Credits"], storyPoints: ["Story Points", "Story Points"], tokenEfficiency: ["Эффективность токенов", "Token efficiency"], costEfficiency: ["Эффективность стоимости", "Cost efficiency"], weightedEfficiency: ["Взвешенная эффективность", "Weighted efficiency"],
    agentsWorked: ["Агентов", "Agents"], agentWorkTime: ["Время работы агентов", "Agent work time"], lessIsBetterTokens: ["токенов на 1 SP · меньше — лучше", "tokens per 1 SP · lower is better"], lessIsBetterCost: ["USD на 1 SP · меньше — лучше", "USD per 1 SP · lower is better"], lessIsBetterWeighted: ["USD/час на 1 SP · меньше — лучше", "USD/hour per 1 SP · lower is better"], workedInPeriod: ["выполняли работу за период", "worked during the period"], totalForAgents: ["суммарно по всем агентам", "total across all agents"],
    tokensByDay: ["Токены по дням", "Tokens by day"], costByDay: ["Стоимость по дням, USD", "Cost by day, USD"], tokensByAgents: ["Токены по агентам", "Tokens by agents"], forPeriod: ["за период", "for the period"],
    agent: ["Агент", "Agent"], model: ["Модель", "Model"], tokens: ["Токены", "Tokens"], runs: ["Запуски", "Runs"], workTime: ["Время работы", "Work time"],
    tokensByProjects: ["Токены по проектам", "Tokens by projects"], project: ["Проект", "Project"], issues: ["Задачи", "Issues"],
    efficiencyByAgents: ["Эффективность по агентам", "Efficiency by agents"], efficiencyByModels: ["Эффективность по моделям", "Efficiency by models"], efficiencyOverTime: ["Эффективность во времени", "Efficiency over time"], efficiencyByIssues: ["Эффективность по задачам", "Efficiency by issues"],
    dataTable: ["Данные таблицей", "Table data"], noData: ["Нет данных за выбранный период и фильтры.", "No data for the selected period and filters."],
    chartAgents: ["График: эффективность по агентам, токены на 1 story point", "Chart: efficiency by agents, tokens per story point"], chartModels: ["График: эффективность по моделям, токены на 1 story point", "Chart: efficiency by models, tokens per story point"], chartTime: ["График: эффективность во времени, токены на 1 story point", "Chart: efficiency over time, tokens per story point"], chartFallback: ["Значения графика — в таблице «Данные таблицей» ниже.", "Chart values are in the Table data section below."],
    configurableChart: ["Настраиваемый график", "Configurable chart"], chartDimension: ["Ось X", "X axis"], chartMeasure: ["Ось Y", "Y axis"], chartUnavailableHelp: ["Недоступные метрики отключены: для выбранного измерения нет надёжных данных.", "Unavailable metrics are disabled because this dimension has no reliable data."], chartLoading: ["Загрузка графика…", "Loading chart…"], chartLoadError: ["Не удалось загрузить график.", "Could not load the chart."], chartConfigCanvas: ["График: {dimension}, {measure}", "Chart: {dimension}, {measure}"], chartConfigTable: ["Данные графика: {dimension}, {measure}", "Chart data: {dimension}, {measure}"],
    chartDimensionTime: ["Время", "Time"], chartDimensionProject: ["Проект", "Project"], chartDimensionAgent: ["Агент", "Agent"], chartDimensionModel: ["Модель", "Model"], chartDimensionIssue: ["Задача", "Issue"],
    chartMeasureInputTokens: ["Входные токены", "Input tokens"], chartMeasureOutputTokens: ["Выходные токены", "Output tokens"], chartMeasureCacheReadTokens: ["Токены чтения кеша", "Cache-read tokens"], chartMeasureCacheWriteTokens: ["Токены записи кеша", "Cache-write tokens"], chartMeasureTotalTokens: ["Всего токенов", "Total tokens"], chartMeasureCostUsd: ["Стоимость, USD", "Cost, USD"], chartMeasureCostCredits: ["Кредиты", "Credits"], chartMeasureStoryPoints: ["Story Points", "Story Points"], chartMeasureTaskCount: ["Количество задач", "Task count"], chartMeasureRunCount: ["Количество запусков", "Run count"], chartMeasureAgentWorkSeconds: ["Время работы агентов", "Agent work time"], chartMeasureTokensPerSp: ["Токены / SP", "Tokens / SP"], chartMeasureCostPerSp: ["Стоимость / SP", "Cost / SP"], chartMeasureWeightedEfficiency: ["Взвешенная эффективность", "Weighted efficiency"],
    dayUtc: ["День (UTC)", "Day (UTC)"], hourUtc: ["Час (UTC)", "Hour (UTC)"], issue: ["Задача", "Issue"], name: ["Название", "Name"], status: ["Статус", "Status"],
    username: ["Имя пользователя", "Username"], password: ["Пароль", "Password"], signIn: ["Войти", "Sign in"], or: ["или", "or"], googleSignIn: ["Войти / зарегистрироваться через Google", "Sign in / register with Google"], yandexSignIn: ["Войти / зарегистрироваться через Яндекс", "Sign in / register with Yandex"], securityNote: ["Соединение защищено HTTPS. Пароль хранится только как стойкий хеш.", "The connection is protected by HTTPS. The password is stored only as a strong hash."],
    registrationClosed: ["Регистрация сейчас закрыта. Чтобы получить доступ, обратитесь к администратору.", "Registration is currently closed. Contact an administrator to get access."], backToLogin: ["Вернуться ко входу", "Back to sign in"],
    disabled: ["недоступно", "unavailable"], pending: ["ожидает синхронизации", "waiting for sync"], replacementPending: ["замена ожидает синхронизации", "replacement waiting for sync"], active: ["подключено", "connected"], syncError: ["ошибка синхронизации", "sync error"], revoking: ["отключение выполняется", "disconnecting"], revoked: ["отозвано", "revoked"],
    inputOutputCache: ["ввод {input} · вывод {output} · кеш {cache}", "input {input} · output {output} · cache {cache}"], officialRates: ["по официальным тарифам", "at official rates"], unpricedModels: ["есть неоценённые модели!", "some models are unpriced!"], issuesWithSp: ["задач: {issues} · с SP: {withSp}", "issues: {issues} · with SP: {withSp}"],
    syncing: ["синхронизация: {value}", "sync: {value}"], notYet: ["ещё не было", "not yet"], noDataYet: ["данных пока нет", "no data yet"], dataRange: ["данные: {first} — {last}", "data: {first} — {last}"], creditRate: ["курс кредитов: {rate} за $1", "credit rate: {rate} per $1"],
    polling: ["проверка каждые 30 с", "checking every 30 s"], loadingError: ["ошибка загрузки: {message}", "loading error: {message}"], total: ["Итого", "Total"], tokensPerSp: ["токенов / SP", "tokens / SP"], timeBy: ["≈ по {granularity} · токены / SP · меньше — лучше", "≈ by {granularity} · tokens / SP · lower is better"], byDaysUtc: ["дням UTC", "UTC days"], byHoursUtc: ["часам UTC", "UTC hours"],
    invalidFilters: ["Некорректные параметры фильтров в ссылке сброшены: {items}. Показаны данные по оставшимся фильтрам.", "Invalid filter parameters in the link were reset: {items}. Data for the remaining filters is shown."], invalidRange: ["«С (UTC)» должно быть раньше «По (UTC)»; диапазон не применён.", "From (UTC) must be earlier than To (UTC); the range was not applied."], invalidRangeUrl: ["диапазон from/to («С» должно быть раньше «По»)", "from/to range (From must be earlier than To)"],
    connectionError: ["Не удалось изменить подключение. Попробуйте позже.", "Could not change the connection. Try again later."], enterPat: ["Введите PAT для подключения.", "Enter a PAT to connect."], statusUnavailable: ["Статус подключения временно недоступен.", "Connection status is temporarily unavailable."], syncNotFinished: ["Синхронизация ещё не завершена. Обновите статус позже.", "Synchronization has not finished yet. Refresh the status later."],
    sessionExpired: ["Сессия истекла. Войдите снова.", "Your session expired. Sign in again."], csrfError: ["Сессия устарела или запрос не прошёл проверку. Обновите страницу.", "The session is stale or the request failed validation. Refresh the page."], tooManyAttempts: ["Слишком много попыток. Повторите позже.", "Too many attempts. Try again later."], invalidLogin: ["Неверное имя пользователя или пароль.", "Incorrect username or password."], oauthError: ["Не удалось выполнить вход через провайдера. Попробуйте снова.", "Could not sign in through the provider. Try again."],
    loginFormError: ["Не удалось проверить форму. Обновите страницу.", "Could not validate the form. Refresh the page."], loginThrottled: ["Слишком много попыток. Повторите вход позже.", "Too many attempts. Try signing in again later."],
    billion: ["млрд", "bn"], million: ["млн", "M"], thousand: ["тыс", "K"], dayShort: ["дн", "d"], hourShort: ["ч", "h"], minuteShort: ["мин", "m"], secondShort: ["с", "s"],
    noModelEfficiency: ["Нет задач со story points и загруженной статистикой для разреза по моделям.", "No issues with story points and loaded statistics for the model breakdown."],
    globalEfficiencyByModels: ["Эффективность по моделям — все пользователи", "Efficiency by models — all users"],
    chartGlobalModels: ["График: эффективность по моделям на данных всех пользователей, стоимость на 1 story point", "Chart: efficiency by models across all users, cost per story point"],
    noGlobalModels: ["Пока нет данных, которыми можно поделиться анонимно.", "No data that can be shared anonymously yet."],
    globalModelsNote: ["Анонимизированные суммарные данные всех пользователей AIStat: только модель, токены, story points и стоимость — без задач, проектов, агентов и имён. Модель показывается, только если её использовали не менее 5 разных пользователей; более редкие модели скрыты целиком. Фильтры дашборда не применяются; значения за всё время.", "Anonymized totals across all AIStat users: only model, tokens, story points and cost — no issues, projects, agents or names. A model is shown only if at least 5 different users used it; rarer models are hidden entirely. Dashboard filters do not apply; values cover all time."],
    day7: ["7 дней", "7 days"], day14: ["14 дней", "14 days"], day30: ["30 дней", "30 days"], day90: ["90 дней", "90 days"], unattributed: ["(не атрибутировано)", "(unattributed)"], unknown: ["неизвестно", "unknown"],
    estimatedTokenEfficiency: ["≈ токены / SP · меньше — лучше", "≈ tokens / SP · lower is better"], estimatedCostEfficiency: ["≈ стоимость на 1 SP · меньше — лучше", "≈ cost per 1 SP · lower is better"], issueEfficiencyHint: ["токены ÷ SP · меньше — лучше", "tokens ÷ SP · lower is better"],
    estimatedCost: ["Стоимость ≈", "Cost ≈"], estimatedCredits: ["Кредиты ≈", "Credits ≈"], weightedColumn: ["Взвеш. ≈ ($/ч·SP)", "Weighted ≈ ($/h·SP)"], tokensPerSpHeader: ["Токены / SP", "Tokens / SP"], costPerSpHeader: ["Стоимость / SP ≈", "Cost / SP ≈"],
    agentEstimateNote: ["≈ — оценка: агенты Codex Dev Sol и QA Codex Sol делят одну модель и runtime, их доли разнесены по длительности запусков за день.", "≈ estimate: Codex Dev Sol and QA Codex Sol share one model and runtime; their portions are allocated by task-run duration each day."],
    projectNote: ["Токены проектов — точные суммы по задачам (multica issue usage); стоимость ≈ оценена по моделям агентов, выполнявших задачи. Эффективность считается только по задачам со story points и загруженной статистикой.", "Project tokens are exact issue totals (multica issue usage); cost ≈ is estimated from the models of agents that ran the issues. Efficiency is calculated only for issues with story points and loaded statistics."],
    timeNote: ["Токены и SP каждой задачи распределяются по длительности подтверждённых запусков. В явно заданном интервале до 48 часов график строится по часам UTC; иначе — по дням UTC. Задачи без датированных запусков не получают выдуманную атрибуцию и не попадают в эти графики. Интервалы без данных остаются разрывами: «—» в таблице, пропуск на графике.", "Each issue's tokens and SP are allocated by confirmed task-run duration. In an explicit range of up to 48 hours the chart uses UTC hours; otherwise it uses UTC days. Issues without dated runs receive no invented attribution and are excluded. Intervals without data remain gaps: — in the table and a break in the chart."],
    modelEfficiencyNote: ["Разрез по моделям среди задач со story points и загруженной статистикой; SP и стоимость делятся между моделями по длительности их запусков. Сортировка — по стоимости на 1 SP, дешевле сверху. «Взвешенная» — стоимость в час на 1 SP; формула и обоснование: docs/metrics-efficiency.md.", "Model breakdown for issues with story points and loaded statistics; SP and cost are split across models by task-run duration. Sorted by cost per 1 SP, lowest first. Weighted means hourly cost per 1 SP; formula and rationale: docs/metrics-efficiency.md."],
    issueEfficiencyNote: ["Задачи без story points исключены из метрики (не считаются как 0). Показаны топ-15 по расходу на 1 SP с учётом фильтра проекта.", "Issues without story points are excluded from the metric (they are not counted as 0). The top 15 by spend per 1 SP are shown with the project filter applied."],
    connectionUnavailable: ["Подключение Multica недоступно", "Multica connection is unavailable"], waitingSync: ["Подключение ожидает синхронизации", "Connection is waiting for sync"], replacementWaiting: ["Замена PAT ожидает синхронизации", "PAT replacement is waiting for sync"], multicaConnected: ["Multica подключён", "Multica is connected"], couldNotSync: ["Не удалось синхронизировать Multica", "Could not synchronize Multica"], disconnectingMultica: ["Отключение Multica выполняется", "Multica is being disconnected"], connectionRevoked: ["Подключение Multica отозвано", "Multica connection was revoked"],
    workerFailed: ["Сбор статистики завершился с ошибкой. Попробуйте позже или переподключите PAT.", "Statistics collection failed. Try again later or reconnect the PAT."], patUnauthorized: ["PAT не прошёл авторизацию в Multica. Проверьте токен и переподключите его.", "The PAT was not authorized by Multica. Check the token and reconnect it."], patLoginFailed: ["Не удалось войти в Multica с этим PAT. Проверьте токен и переподключите его.", "Could not sign in to Multica with this PAT. Check the token and reconnect it."], workspaceListFailed: ["Не удалось получить список рабочих пространств для этого PAT. Попробуйте переподключить его.", "Could not get the workspace list for this PAT. Try reconnecting it."], noWorkspace: ["У этого PAT нет доступных рабочих пространств. Проверьте права токена в Multica.", "This PAT has no accessible workspaces. Check its permissions in Multica."], workspaceRequired: ["У PAT несколько рабочих пространств — укажите нужное в поле «Рабочее пространство» и переподключите PAT.", "This PAT has multiple workspaces — select one in Workspace and reconnect the PAT."], workspaceNotSelected: ["Рабочее пространство для подключения не выбрано. Переподключите PAT, указав название рабочего пространства.", "No workspace is selected for the connection. Reconnect the PAT with a workspace name."], workspaceReadFailed: ["Не удалось прочитать данные рабочего пространства. Попробуйте позже или переподключите PAT.", "Could not read workspace data. Try again later or reconnect the PAT."], dataLoadFailed: ["Не удалось загрузить данные из Multica. Попробуйте позже.", "Could not load data from Multica. Try again later."], publishFailed: ["Не удалось сохранить полученную статистику. Попробуйте позже.", "Could not save the received statistics. Try again later."], sourceFailed: ["Источник данных Multica ответил ошибкой. Попробуйте позже.", "The Multica data source returned an error. Try again later."], issueSyncFailed: ["Не удалось синхронизировать детали задач. Попробуйте позже.", "Could not synchronize issue details. Try again later."], syncFallback: ["Синхронизация Multica завершилась с ошибкой. Попробуйте подключить PAT ещё раз.", "Multica synchronization failed. Try connecting the PAT again."], namedWorkspaceMissing: ["Рабочее пространство {workspace} не найдено у этого PAT. Проверьте точное название рабочего пространства в Multica и переподключите PAT.", "Workspace {workspace} was not found for this PAT. Check the exact workspace name in Multica and reconnect the PAT."], namedWorkspaceAmbiguous: ["Название рабочего пространства {workspace} подходит сразу нескольким. Уточните точное название и переподключите PAT.", "The workspace name {workspace} matches more than one workspace. Specify the exact name and reconnect the PAT."], specified: ["указанное", "specified"],
    connectionMissing: ["Подключение не найдено. Обновите страницу.", "Connection not found. Refresh the page."], checkPat: ["Проверьте PAT и метку workspace.", "Check the PAT and workspace label."], connectionTemporarilyUnavailable: ["Подключение сейчас недоступно. Попробуйте позже.", "The connection is unavailable right now. Try again later."], manualDisabled: ["Администратор временно отключил ручное подключение.", "An administrator has temporarily disabled manual connections."], patAccepted: ["PAT принят. Ожидаем подтверждение защищённого worker-канала.", "PAT accepted. Waiting for secure worker-channel confirmation."], newPatAccepted: ["Новая PAT принята. Ожидаем подтверждение замены.", "New PAT accepted. Waiting for replacement confirmation."], autoUpdates: ["Статистика будет обновляться автоматически.", "Statistics will update automatically."], disconnectRequested: ["Запрос на отключение принят. Ожидаем удаления доступа worker-каналом.", "Disconnect request accepted. Waiting for the worker channel to remove access."], accessRemoved: ["Доступ удалён. Отзовите PAT в настройках Multica.", "Access was removed. Revoke the PAT in Multica settings."], revokedPrompt: ["Подключение отозвано. Подключите новый PAT, если хотите продолжить синхронизацию.", "The connection was revoked. Connect a new PAT to continue synchronization."], unpriced: ["⚠ без тарифа: {models}", "⚠ unpriced: {models}"], allPriced: ["все модели с официальным тарифом", "all models have official rates"],
  };
  const byText = Object.fromEntries(Object.entries(messages).flatMap(([key, pair]) => pair.map((value) => [value, key])));
  let locale;

  function preferredLocale() {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved === "ru" || saved === "en") return saved;
    } catch (_) {}
    return String(navigator.language || "").toLowerCase().startsWith("ru-") || navigator.language === "ru" ? "ru" : "en";
  }
  function interpolate(value, params) {
    return value.replace(/\{(\w+)\}/g, (_, name) => params && params[name] != null ? params[name] : "");
  }
  function t(key, params) {
    const pair = messages[key];
    return pair ? interpolate(pair[locale === "ru" ? 0 : 1], params) : key;
  }
  function translateText(value) {
    const key = byText[String(value).trim()];
    return key ? t(key) : value;
  }
  function applyNode(node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const trimmed = node.nodeValue.trim();
      const key = byText[trimmed];
      if (key) node.nodeValue = node.nodeValue.replace(trimmed, t(key));
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE || node.closest("script, style")) return;
    if (node.dataset.i18n) {
      node.textContent = t(node.dataset.i18n);
      return;
    }
    ["title", "aria-label", "placeholder"].forEach((name) => {
      if (node.hasAttribute(name)) {
        const key = byText[node.getAttribute(name)];
        if (key) node.setAttribute(name, t(key));
      }
    });
    for (const child of node.childNodes) applyNode(child);
  }
  function addSwitcher() {
    if (document.getElementById("locale-switcher")) return;
    const button = document.createElement("button");
    button.id = "locale-switcher";
    button.className = "locale-switcher";
    button.type = "button";
    button.addEventListener("click", () => setLocale(locale === "ru" ? "en" : "ru"));
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        button.click();
      }
    });
    const target = document.querySelector(".topbar .status") || document.querySelector(".login-card");
    if (target) target.prepend(button);
  }
  function render() {
    document.documentElement.lang = locale;
    const page = document.body && document.body.dataset.page;
    document.title = t(page === "login" ? "loginTitle" : page === "closed" ? "closedTitle" : "appTitle");
    addSwitcher();
    applyNode(document.body);
    const button = document.getElementById("locale-switcher");
    if (button) {
      button.textContent = locale === "ru" ? "RU / EN" : "EN / RU";
      button.setAttribute("aria-label", t("language") + ": " + t(locale));
      button.setAttribute("aria-pressed", String(locale === "ru"));
    }
  }
  function setLocale(next) {
    locale = next === "ru" ? "ru" : "en";
    try { localStorage.setItem(STORAGE_KEY, locale); } catch (_) {}
    render();
    document.dispatchEvent(new CustomEvent("aistat:localechange", { detail: { locale } }));
  }
  function init() {
    locale = preferredLocale();
    render();
    new MutationObserver((records) => records.forEach((record) => record.addedNodes.forEach(applyNode))).observe(document.body, { childList: true, subtree: true });
  }
  return { init, setLocale, t, translateText, get locale() { return locale; }, get tag() { return locale === "ru" ? "ru-RU" : "en-US"; } };
})();

if (["login", "closed"].includes(document.body && document.body.dataset.page)) I18N.init();
