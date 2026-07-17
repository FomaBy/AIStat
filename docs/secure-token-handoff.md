# Подключение своего Мультика: безопасный handoff токена (FAN-1220)

Пользователь вводит API-токен своего Multica в кабинете на публичном хосте.
Публичный shared-хост не должен владеть этим токеном постоянно: он хранит его
только до тех пор, пока доверенный локальный worker не заберёт его по
аутентифицированному pull-каналу. После подтверждения worker'ом хостовая копия
физически уничтожается; на хосте остаётся только маркер `active`.

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

Одна строка на пользователя (`user_id` — PRIMARY KEY): `server_url`
(по умолчанию — сервер владельца из `AISTAT_DEFAULT_SERVER_URL`), метка
воркспейса, `token` (только до подтверждённого handoff), `token_epoch`
(монотонный счётчик замен), `status`, timestamps, поля lease
(`lease_id`, `lease_expires_at`), `revoke_acked_at`, `last_sync_error`,
`last_synced_at`. Все соединения с `security.db` работают с
`PRAGMA secure_delete = ON`, поэтому стёртые значения зануляются и в
освободившихся страницах файла.

## State machine

```
 (нет строки)
      │ POST /api/connection (токен принят)
      ▼
   pending ──────────── lease/pull/ack ────────────▶ active
      │  ▲                                            │  │
      │  │ POST /api/connection (замена: epoch+1)     │  │ sync_error
      │  └────────────────────────────────────────────┘  ▼
      │                                                error ── sync_ok ─▶ active
      │ POST /api/connection/revoke                      │
      ▼                                                  │ (revoke)
   revoked ◀─────────────────────────────────────────────┘
      │ worker ack "revoked" → revoke_acked_at
      ▼
   (замена снова возможна: новый POST → pending, epoch+1)
```

- **pending** — токен лежит в `security.db` и ждёт worker.
- **active** — worker подтвердил приём; токена на хосте физически нет.
- **error** — worker сообщил ошибку сбора (`last_sync_error` виден в кабинете).
- **revoked** — пользователь отключил подключение; хостовый токен стёрт сразу,
  worker при следующем pull удаляет свой локальный токен и подтверждает это.

Все переходы атомарны (`BEGIN IMMEDIATE`) и tenant-scoped: каждый затрагивает
ровно одну строку `user_id`.

## Приём токена (оба публичных контура)

- `GET /api/connection` — статус для кабинета. Токен не возвращается никогда.
- `POST /api/connection` — принять/заменить токен. Требуются: активная
  сессия, CSRF (`X-CSRF-Token` или поле `csrf`), не превышен лимит
  (10 попыток за 15 минут на пользователя, по образцу `login_throttle`;
  попытка учитывается до валидации). Поля: `token`, `server_url`
  (опционально при заданном `AISTAT_DEFAULT_SERVER_URL`; только HTTPS,
  http — исключительно для localhost), `workspace_label` (опционально).
  Сообщения об ошибках валидации никогда не содержат присланных значений.
- `POST /api/connection/revoke` — отключить (сессия + CSRF).

Если `AISTAT_WORKER_SECRET` не задан, приём отвечает `503`, а worker-эндпоинты
`404`: хост не принимает токен, который некому забрать (fail-closed).

## Pull-канал worker'а

Эндпоинты (вне сессионной аутентификации):

- `POST /api/worker/connection/pull` — выдать pending-подключения (с токенами
  и свежими lease) и неподтверждённые revoke.
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

`pull` в одной транзакции выдаёт каждому pending-подключению новый `lease_id`
(TTL 10 минут), инвалидируя предыдущий. `ack result=stored` срабатывает только
при точном совпадении (`status=pending`, `lease_id`, `token_epoch`, lease не
истёк) — тогда токен на хосте стирается и статус становится `active`. Потерян
ack — worker просто делает новый pull и ack (идемпотентная повторная попытка).
Замена или revoke между pull и ack меняют `token_epoch`/статус, и устаревший
ack отвергается (`stale-epoch` / `stale-lease`) — новый токен не может быть
ошибочно удалён или активирован старым подтверждением.

## Worker-side хранилище

`aistat/worker_store.py`: SQLite-файл, в котором токены лежат **только** в виде
Fernet-шифртекста (AEAD из пакета `cryptography`). Ключ генерируется
автоматически, хранится отдельным файлом `0600` (каталог `0700`) и обязан
находиться **не** в каталоге хранилища — иначе стор откажется стартовать.
Файл хранилища — `0600`, тоже с `secure_delete`. Замена перезаписывает
шифртекст, revoke удаляет строку.

Клиент `python -m aistat.worker_sync --once|--watch` выполняет цикл
pull → store/delete → ack и никогда не пишет значения токенов в логи и вывод.
`report_sync(...)` отправляет `sync_ok`/`sync_error` для кабинета (сам сбор
per-user данных — отдельный этап, FAN-1221).

## Деплой

Хост (cPanel, «Setup Python App» → environment):
`AISTAT_WORKER_SECRET` (третий независимый секрет),
опционально `AISTAT_DEFAULT_SERVER_URL`.

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

Между отправкой формы и подтверждённым handoff (обычно ≤ интервала pull)
токен существует на shared-хосте: в памяти процесса при запросе и в
`security.db` (файл `0600`, `secure_delete`). Компрометация хоста в этом окне
раскрывает pending-токены; уже переданные (`active`) недоступны — на хосте их
физически нет. Митигируется коротким окном, правами файлов, отсутствием
токена в логах/ответах/снапшотах и немедленным стиранием при revoke. Осознанно
принятый остаток риска для shared-hosting архитектуры; снижение — вводить
токен, когда worker онлайн, и отзывать его при любых сомнениях (после revoke
worker прекращает сбор и удаляет локальную копию при следующем pull).
