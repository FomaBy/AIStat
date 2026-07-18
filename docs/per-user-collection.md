# Per-user сбор данных и per-tenant публикация (FAN-1221)

После того как доверенный worker забрал пользовательский Multica API-токен (PAT)
по каналу handoff ([secure-token-handoff.md](secure-token-handoff.md)), он сам
собирает статистику **по каждому подключению отдельно** и публикует подписанный
snapshot **только этого tenant**. Регистрация/вход в AIStat остаются через Google;
PAT — это только вручную введённый ключ интеграции статистики, он никогда не
выступает паролем AIStat.

Всё выполняется только на доверенной локальной машине. Публичный cPanel-хост не
получает ни токен, ни CLI-конфигурацию — только подписанные snapshot'ы. В коде,
тестах, логах и документации используются исключительно синтетические значения.

## Компоненты

| Код | Роль |
|---|---|
| `aistat/worker_store.py` | зашифрованное хранилище активных подключений; `list_connections()` (без токена) и `get_token(user_id)` |
| `aistat/cli_profile.py` | `ConnectionCliProfile` — task-owned lifecycle официального CLI для одного подключения |
| `aistat/poller.py` | существующий `Poller`; получает per-connection `runner` и пишет в tenant-БД подключения |
| `aistat/publish.py` | `publish_snapshot(config, db_path, tenant_id)` — подпись и загрузка snapshot одного tenant |
| `aistat/collector.py` | `Collector` — цикл по активным подключениям, изоляция сбоев, backpressure, отчёт в кабинет |

## Цикл одного подключения

Для каждой записи из `WorkerTokenStore.list_connections()`:

1. взять per-tenant advisory-lock (`data/cli_profiles/conn-<id>.lock`); если он занят
   — подключение пропускается (backpressure), чтобы не запустить competing poll того
   же tenant;
2. расшифровать PAT (`get_token`); если его уже нет (revoke между list и get) —
   пропустить как `revoked`;
3. залогинить официальный CLI в изолированный профиль (см. ниже) **stdin-контрактом**;
4. явно выбрать workspace подключения;
5. прогнать один полный цикл `Poller` в worker-локальную БД
   `data/worker_tenants/<internal_user_id>.db`;
6. опубликовать per-tenant snapshot этой БД;
7. `auth logout` + удаление residue профиля;
8. отчитаться о результате (`worker_sync.report_sync`) для кабинета пользователя.

Каждое подключение обрабатывается независимо: сбой auth/CLI/poll/publish одного
подключения не мешает остальным.

## Изоляция профиля официального CLI

`ConnectionCliProfile` не даёт per-user вызову «свалиться» на identity владельца:

- **Scrubbed environment.** Из окружения дочернего процесса вырезаются **все**
  `MULTICA_*` переменные (в т.ч. `MULTICA_TOKEN`, `MULTICA_SERVER_URL`,
  `MULTICA_WORKSPACE_ID`, которые runtime владельца инжектит в ambient env).
  Без этого неаутентифицированный профиль молча использовал бы токен владельца;
  со scrub — он честно fail-closed («No server configured»).
- **Task-owned HOME.** Официальный CLI хранит конфиг в `$HOME/.multica/...`, поэтому
  `HOME` указывается на `AISTAT_CLI_PROFILES_DIR` (не на реальный `~/.multica`
  владельца).
- **Детерминированный профиль.** `--profile aistat-conn-<internal_user_id>` строится
  только из доверенного числового id (валидируется `canonical_tenant_id`), поэтому
  path traversal / collision через пользовательский ввод невозможны.
- **Пиннинг официального хоста.** На каждом вызове передаётся
  `--server-url <AISTAT_MULTICA_OFFICIAL_URL>` (по умолчанию `https://multica.ai`);
  сохранённый/пользовательский `server_url` не используется никогда.
- **Явный workspace.** `workspace list` → `resolve_workspace` выбирает ровно один
  workspace (по id/slug/name или ≥4-символьному префиксу id, либо единственный при
  отсутствии метки). Неоднозначность/отсутствие совпадения — ошибка, а не тихий
  fallback на workspace владельца. Выбор передаётся как `--workspace-id` на каждом
  data-вызове.

## Контракт токена

PAT попадает в официальный CLI **только через stdin-приглашение**
`multica login --token` (CLI читает токен из pipe, TTY не требуется). Токен никогда
не появляется в argv, environment, URL, stdout/stderr, логах, исключениях, snapshot
или статусе. При сбое одного подключения в статус пишется фиксированная безопасная
строка (например «authentication with the connection's token failed») — без PAT,
пути профиля и сырых деталей CLI.

## Per-tenant snapshot

`publish_snapshot` подписывает snapshot HMAC-подписью, в которую входят
`tenant_id`, timestamp и sha256 тела (`aistat-snapshot-v2`), и шлёт заголовок
`X-AIStat-Tenant`. Один ingest-секрет подписывает любого tenant, потому что tenant
находится **внутри** подписанного материала. Хост устанавливает snapshot только в
БД этого tenant и отклоняет wrong-tenant, replay и устаревший timestamp; установка
атомарна (`os.replace`, symlink/traversal-guard). Подтверждение хоста сверяется с
tenant_id/sha256/size отправленного.

## Restart-idempotency и cleanup

- Все записи poller'а — идемпотентные upsert'ы; установка snapshot атомарна и
  защищена per-tenant replay-watermark, поэтому crash/restart не дублирует данные.
- Перед каждым логином residue профиля удаляется, а после цикла выполняется
  `auth logout` + удаление каталога профиля, поэтому revoked/replaced токен не
  «воскресает» и не остаётся на диске.
- Replace во время цикла: токен читается из хранилища в начале цикла, а logout в
  конце стирает копию; следующий цикл берёт уже новый токен (по bumped epoch).

## Запуск

Worker-процесс (только доверенная машина), рядом с `worker_sync`:

```
python -m aistat.collector --watch      # цикл по всем подключениям
python -m aistat.collector --once       # один цикл, JSON-сводка (без токенов/путей)
```

Требует настроенных `AISTAT_PUBLISH_URL` + `AISTAT_INGEST_SECRET` (для публикации) и
`AISTAT_WORKER_STORE_PATH` + `AISTAT_WORKER_KEY_PATH` (хранилище токенов). `HOME`
per-user CLI берётся из `AISTAT_CLI_PROFILES_DIR`, staging tenant-БД — из
`AISTAT_WORKER_TENANTS_DIR`.

## Residual risk

- Поведение `multica login --token` с **валидным** PAT (немедленный возврат против
  фонового «watch») на реальном хосте в этой задаче не прогонялось: по требованиям
  запрещены реальные credentials и обращение к production. Логин ограничен
  `AISTAT_CLI_TIMEOUT_SECONDS`, а сбой/таймаут одного подключения изолирован и
  помечается безопасным статусом. Пустой/фейковый токен на официальном хосте в
  проверке lifecycle возвращался за <1s, читая токен из stdin и не оставляя residue.
- `collector` использует `fcntl` (advisory-lock) и предназначен только для
  Unix-worker'а; на публичный cPanel-контур он не импортируется.
