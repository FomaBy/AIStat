# Подключение своего Мультика: безопасный handoff токена (FAN-1220 / FAN-1337)

Пользователь регистрируется/входит через Google, а свой Multica API-токен (PAT)
вводит вручную в кабинете на публичном хосте. PAT **никогда** не используется как
пароль AIStat. Публичный shared-хост не должен владеть токеном постоянно: он
хранит его только до тех пор, пока доверенный локальный worker не заберёт его по
аутентифицированному pull-каналу. После подтверждения worker'ом хостовая копия
физически уничтожается; на хосте остаётся только маркер `active`.

Вся функция ship'ится **выключенной** (`AISTAT_MULTICA_CONNECT_ENABLED` не задан):
приём токена и worker-эндпоинты недоступны, пока владелец явно её не включит.

## ⚠️ Что такое Multica PAT и почему это важно

- PAT представляет **самого пользователя**: он видит **все** доступные этому
  пользователю workspaces, а не один.
- PAT обычно **не истекает** сам по себе.
- Поэтому рекомендуется завести **отдельный PAT** специально для AIStat, по
  возможности с ручным сроком истечения, и **обязательно отозвать его в Multica**
  после отключения (revoke) в AIStat. Revoke в AIStat стирает хостовую и
  worker-копию, но сам токен в Multica остаётся валидным, пока его не отзовут там.
- В коде, тестах, логах, снапшотах и документации используются только
  синтетические значения — реальные токены/ключи не фигурируют нигде.

## Компоненты

| Сторона | Код | Роль |
|---|---|---|
| Публичный хост | `aistat/wsgi.py`, `aistat/legacy_wsgi.py` | приём токена, pull-канал worker'а |
| Общая логика | `aistat/handoff.py` | подпись канала, валидация, state machine (Python 3.6, только stdlib — грузится в оба контура) |
| Worker (локальная машина) | `aistat/worker_sync.py`, `aistat/worker_store.py` | pull, шифрованное хранение, ack |

Оба публичных контура (Flask и dependency-free legacy для cPanel) используют
одни и те же функции `aistat.handoff`, поэтому переходы состояний и проверки
подписи совпадают байт-в-байт.

## Схема данных: `connections` в `security.db`

Одна строка на пользователя (`user_id` — PRIMARY KEY): `server_url` (всегда
пиннится к официальному хосту, см. ниже), метка воркспейса, `token` (только до
подтверждённого handoff), `token_epoch` (монотонный счётчик замен), `status`,
timestamps, поля lease (`lease_id`, `lease_expires_at`), `revoke_acked_at`,
`last_sync_error`, `last_synced_at`. Таблица `worker_status` (единственная
строка `id=1`, `last_seen_at`) хранит время последнего аутентифицированного pull
worker'а — сигнал его готовности. Все соединения с `security.db` работают с
`PRAGMA secure_delete = ON`, поэтому стёртые значения зануляются и в
освободившихся страницах файла.

## State machine

```
 (нет строки)
      │ POST /api/connection (первый раз)        замена: POST /api/connection
      ▼                                          (epoch+1)
   pending ─┐                                    ▼
            ├──── lease/pull/ack (stored) ──▶ active ──sync_error──▶ error
 replacement_pending ─┘                        │  ▲                    │
      │                                        │  └──── sync_ok ───────┘
      │ TTL истёк / revoke / cleanup           │ revoke / cleanup
      ▼                                        ▼
   revocation_pending ◀───────────────────────┘
      │ worker удалил свою копию и подтвердил (ack "revoked")
      ▼
   revoked   (реконнект снова возможен: POST → replacement_pending, epoch+1)
```

- **pending** — первый токен лежит в `security.db` и ждёт worker.
- **replacement_pending** — то же, но заменяет уже существующее подключение
  (кабинет отличает замену от первого подключения); worker перезапишет старую
  зашифрованную копию при сохранении нового токена.
- **active** — worker подтвердил приём; токена на хосте физически нет.
- **error** — worker сообщил ошибку сбора (`last_sync_error` виден в кабинете).
- **revocation_pending** — отключение / истёкший TTL / cleanup аккаунта: хостовый
  токен стёрт немедленно, но worker ещё должен удалить свою копию и подтвердить
  это. Это видимое fail-closed состояние: подключение **не** считается активным.
- **revoked** — терминально, появляется **только** после ack удаления от worker'а.
  Значит: рабочей worker-копии токена гарантированно не осталось.

Устаревший lease/epoch не может завершить новый lifecycle: ack по старому epoch
отвергается (`stale-epoch` / `stale-lease`), поэтому «догоняющее» подтверждение
не активирует и не отзовёт уже заменённое подключение.

Все переходы атомарны (`BEGIN IMMEDIATE`) и tenant-scoped: каждый затрагивает
ровно одну строку `user_id`.

## Приём токена (оба публичных контура)

- `GET /api/connection` — статус для кабинета. Токен не возвращается никогда.
  `server_url` также не публикуется; при выключенной функции возвращается
  `{"status":"disabled"}`.
- `POST /api/connection` — принять/заменить токен. Требуются по порядку:
  функция включена; задан `AISTAT_WORKER_SECRET`; активная сессия; CSRF
  (`X-CSRF-Token` или поле `csrf`); не превышен лимит (10 попыток за 15 минут на
  пользователя, по образцу `login_throttle`; попытка учитывается до валидации);
  валидный `token`, необязательный `workspace_label` и совместимое legacy-поле
  `server_url` (только отсутствующее/пустое или exact `https://multica.ai`);
  **и свежий heartbeat worker'а** (иначе токен не сохраняется).
  Сообщения об ошибках валидации никогда не содержат присланных значений.
- `POST /api/connection/revoke` — отключить (сессия + CSRF); переводит в
  `revocation_pending`, `revoked` — после ack worker'а.

### Fail-closed контроли

- **Feature flag.** Без `AISTAT_MULTICA_CONNECT_ENABLED=1` приём и revoke отвечают
  `503`, worker-эндпоинты — `404`, `GET` — `disabled`. Функция дремлет.
- **Только официальный хост.** Endpoint зафиксирован byte-exact константой
  `https://multica.ai` и не редактируется/не публикуется в UI/API. Отсутствующее
  или пустое legacy-поле `server_url` нормализуется к константе; exact значение
  допускается только для обратной совместимости. Любое отличие — другой host,
  case, whitespace, slash, path, query, fragment, IP, port, credentials или
  scheme — отвергается (`422`) до сохранения PAT. Legacy
  `AISTAT_MULTICA_OFFICIAL_URL` является только assertion: несовпадение блокирует
  запуск/collector, но не может заменить host. Pending-строка с отравленным
  persisted URL стирает PAT и переводится в revoke до handoff worker'у.
- **Короткий TTL pending-токена.** Открытый токен живёт на хосте не дольше
  `AISTAT_CONNECTION_PENDING_TTL_SECONDS` (по умолчанию 600 c, жёсткий потолок
  900 c). По истечении он физически стирается и переходит в `revocation_pending`
  (worker удаляет любую копию и подтверждает). Чистка срабатывает при pull, при
  чтении статуса и в периодическом worker-цикле.
- **Готовность worker'а.** Перед записью токена хост требует, чтобы последний
  аутентифицированный pull worker'а был не старше
  `AISTAT_WORKER_READINESS_TTL_SECONDS` (по умолчанию 900 c). Если worker не
  готов — `503`, токен не сохраняется. Так токен не попадёт на хост, где его
  некому вовремя забрать.
- **Cleanup аккаунта.** Удаление аккаунта/credential использует тот же
  revoke-примитив: хостовый токен стирается, а `revoked` наступает лишь после
  ack удаления worker'ом. Пока ack не пришёл — видимое `revocation_pending`,
  активной worker-копии не остаётся.

## Pull-канал worker'а

Эндпоинты (вне сессионной аутентификации):

- `POST /api/worker/connection/pull` — записать heartbeat, сделать TTL-чистку,
  выдать pending/replacement-подключения (с токенами и свежими lease) и
  неподтверждённые revoke (для удаления копии).
- `POST /api/worker/connection/ack` — пакет подтверждений
  `{"acks": [{user_id, token_epoch, lease_id?, result, error?}]}`,
  `result ∈ {stored, revoked, sync_ok, sync_error}`.

Каждый запрос подписан **отдельным** секретом `AISTAT_WORKER_SECRET`
(≥ 32 байт; обязан отличаться и от session-, и от ingest-секрета):

```
canonical = "aistat-worker-v1\n{path}\n{timestamp}\n{nonce}\n{sha256(body)}"
X-AIStat-Signature: v1=HMAC-SHA256(secret, canonical)
X-AIStat-Timestamp: unix-время (окно ±AISTAT_INGEST_MAX_AGE_SECONDS, 300 c)
X-AIStat-Nonce: 16–128 символов [A-Za-z0-9_-]
```

Replay-защита двухслойная: timestamp ограничивает окно, nonce одноразов
(таблица `worker_nonces`, повтор → `409`; хранится втрое дольше окна, чтобы
очистка не «оживила» ещё валидный по времени запрос). Подпись привязана к
конкретному пути, поэтому запрос pull нельзя проиграть в ack и наоборот.

### Lease/ack

`pull` в одной транзакции выдаёт каждому pending/replacement-подключению новый
`lease_id` (TTL 10 минут), инвалидируя предыдущий. `ack result=stored` срабатывает
только при точном совпадении (`status ∈ {pending, replacement_pending}`,
`lease_id`, `token_epoch`, lease не истёк) — тогда токен на хосте стирается и
статус становится `active`. Потерян ack — worker просто делает новый pull и ack
(идемпотентная повторная попытка). Замена или revoke между pull и ack меняют
`token_epoch`/статус, и устаревший ack отвергается (`stale-epoch` / `stale-lease`).
`ack result=revoked` переводит `revocation_pending → revoked` ровно один раз
(повтор идемпотентен) и только при совпадении epoch.

## Worker-side хранилище

`aistat/worker_store.py`: SQLite-файл, в котором токены лежат **только** в виде
Fernet-шифртекста (AEAD из пакета `cryptography`). PAT, routing metadata и
`token_epoch` читаются одной атомарной credential version. Ключ генерируется
автоматически, хранится отдельным файлом `0600` (каталог `0700`) и обязан
находиться **не** в каталоге хранилища — иначе стор откажется стартовать.
Файл хранилища — `0600`, тоже с `secure_delete`. Замена перезаписывает
шифртекст, revoke удаляет строку с PAT, но сохраняет durable epoch/tombstone.
Store/revoke применяются только монотонно: delayed старый worker pull не может
перезаписать новый PAT, воскресить revoked credential или удалить reconnect.
Точный повтор одного stored/revoked epoch идемпотентен; конфликтующие данные в
том же epoch fail-closed.

Worker sync и collector используют общий process/thread-safe per-tenant file
fence. Collector берёт его коротко для атомарного чтения, а затем повторно прямо
перед publish: completed replace/revoke отменяет stale snapshot и report;
изменение, начавшееся после final check, ждёт завершения publish/cleanup/report.
Другие tenant имеют отдельные fences и продолжают работу независимо; exception
или crash освобождает advisory lock.

Клиент `python -m aistat.worker_sync --once|--watch` выполняет цикл
pull → store/delete → ack и никогда не пишет значения токенов в логи и вывод.
Регулярный pull worker'а одновременно служит heartbeat'ом готовности для хоста.
`report_sync(...)` отправляет `sync_ok`/`sync_error` для кабинета (сам сбор
per-user данных — отдельный этап, FAN-1221).

## Деплой

Хост (cPanel, «Setup Python App» → environment):
`AISTAT_MULTICA_CONNECT_ENABLED=1` (включить функцию),
`AISTAT_WORKER_SECRET` (третий независимый секрет),
`AISTAT_CONNECTION_PENDING_TTL_SECONDS` (≤ 900),
`AISTAT_WORKER_READINESS_TTL_SECONDS`.

`AISTAT_MULTICA_OFFICIAL_URL` не настраивается. Если переменная осталась от
старого деплоя, она должна быть byte-exact `https://multica.ai`, иначе процесс
fail-closed не запускает connection lifecycle.

Локальная машина (рядом с publisher'ом в `~/.config/aistat/production.env`):
`AISTAT_WORKER_SYNC_URL=https://aistat.app`, тот же `AISTAT_WORKER_SECRET`,
опционально `AISTAT_WORKER_KEY_PATH` (по умолчанию
`~/.config/aistat/worker.key`), `AISTAT_WORKER_STORE_PATH` (по умолчанию
`./data/worker_connections.db`), `AISTAT_WORKER_PULL_INTERVAL_SECONDS`
(по умолчанию 300, минимум 60). Запуск — `python -m aistat.worker_sync
--watch` (например, отдельным launchd-агентом по образцу
`deploy/com.aistat.sync.plist.example`).

Граница пакета: `scripts/build_cpanel_package.sh` не включает
`worker_store.py`/`worker_sync.py`, а `requirements-cpanel.txt` остаётся
dependency-free — `cryptography`, ключи и хранилище существуют только на
доверенной локальной машине (проверяется тестом `tests/test_cpanel_package.py`).

## Residual risk

Между отправкой формы и подтверждённым handoff (обычно ≤ интервала pull, но не
дольше pending-TTL) токен существует на shared-хосте: в памяти процесса при
запросе и в `security.db` (файл `0600`, `secure_delete`). Компрометация хоста в
этом окне раскрывает pending-токены; уже переданные (`active`) недоступны — на
хосте их физически нет. Митигируется коротким TTL, обязательной готовностью
worker'а до записи, пиннингом к официальному хосту, правами файлов, отсутствием
токена в логах/ответах/снапшотах и немедленным стиранием при revoke/истечении.
Осознанно принятый остаток риска для shared-hosting архитектуры; снижение —
вводить токен, когда worker онлайн, использовать отдельный PAT с ручным сроком и
отзывать его в Multica при любых сомнениях (после revoke worker прекращает сбор
и удаляет локальную копию при следующем pull).
