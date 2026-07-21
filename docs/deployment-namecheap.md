# Развёртывание AIStat на Namecheap Shared Hosting

## Архитектура

Локальный FastAPI остаётся средой разработки. Публичный пакет содержит два
совместимых входа:

- `passenger_wsgi.py` → `aistat.legacy_wsgi` для серверов с рабочим Passenger;
- `aistat.cgi` → `aistat.legacy_wsgi` для Namecheap Shared Hosting с LiteSpeed,
  где системный WSGI launcher может отсутствовать.

Оба входа используют один dependency-free Python 3.6+ WSGI-контур. CGI-вариант
запускается отдельным процессом на запрос, что медленнее Passenger, но
предсказуемо работает на shared-тарифе без установки старых web-зависимостей.

Multica CLI и его токен **не устанавливаются на публичный сервер**. Локальный
доверенный Mac продолжает собирать данные через `multica`, создаёт согласованный
SQLite snapshot и отправляет его на сайт по HTTPS. Запрос подписан отдельным
HMAC-секретом; сервер проверяет подпись, срок запроса, защиту от повтора,
целостность SQLite и версию схемы, затем атомарно заменяет только файл
конкретного пользователя `tenants/<user_id>.db`. Tenant ID входит в HMAC
подпись `v2`; replay watermark и сведения о последнем snapshot хранятся
отдельно для каждого пользователя в `security.db`.

Полезные официальные инструкции Namecheap:

- [Python App / WSGI](https://www.namecheap.com/support/knowledgebase/article.aspx/10048/2182/how-to-work-with-python-app/)
- [включение SSH](https://www.namecheap.com/support/knowledgebase/article.aspx/10040/2210/how-to-enable-ssh-shell-in-cpanel/)
- [доступ по SSH](https://www.namecheap.com/support/knowledgebase/article.aspx/1016/89/how-to-access-a-hosting-account-via-ssh/)
- [Namecheap SSL](https://www.namecheap.com/support/knowledgebase/article.aspx/9387/2218/what-is-namecheap-ssl-and-how-do-i-use-it/)

## 1. Подготовить секреты

На локальном Mac:

```bash
cd /Users/sergeyfomin/Documents/AIStat
.venv/bin/python -m aistat.security hash-password
.venv/bin/python -m aistat.security generate-secret  # session secret
.venv/bin/python -m aistat.security generate-secret  # ingest secret
```

Нужны два **разных** секрета. Пароль на сервер не передаётся: только стойкий
Werkzeug PBKDF2 hash.

## 2. Установить HTTPS

В cPanel открыть `SSL/TLS` или `Namecheap SSL` и установить сертификат для
`aistat.app` и `www.aistat.app`, затем включить HTTPS Redirect. До продолжения:

```bash
curl -I https://aistat.app/
```

не должен выдавать ошибку сертификата. HSTS включается приложением только на
HTTPS-запросах.

## 3. Развернуть приложение

Собрать архив:

```bash
./scripts/build_cpanel_package.sh
```

Загрузить содержимое `dist/aistat-cpanel.zip` в `$HOME/aistat_app`.
Дополнительные Python-пакеты не требуются.

### Вариант A: LiteSpeed CGI на Namecheap Shared Hosting

Скопировать:

- `aistat.cgi` в `$HOME/public_html/cgi-bin/aistat.cgi` и задать права `0755`;
- `.htaccess.example` в `$HOME/public_html/.htaccess`.

Шлюз читает секретные переменные из
`$HOME/aistat-private/aistat.env`; этот каталог должен иметь права `0700`.

### Вариант B: рабочий Passenger

В `cPanel → Setup Python App`:

- Python: системная Python 3.6 или новее;
- Application root: `aistat_app`;
- Application URL: домен `aistat.app`, путь `/`;
- Startup file: `passenger_wsgi.py`;
- Entry point: `application`.

Если запросы завершаются ошибкой `lswsgi3: No such file or directory`, отключить
Passenger-привязку и использовать CGI-вариант выше.

## 4. Хранить данные вне web root

Через cPanel Terminal/SSH:

```bash
mkdir -p "$HOME/aistat-private"
mkdir -p "$HOME/aistat-private/tenants"
chmod 700 "$HOME/aistat-private"
chmod 700 "$HOME/aistat-private/tenants"
```

Для Passenger задать Environment Variables приложения. Для CGI сохранить те же
строки в `$HOME/aistat-private/aistat.env`:

```text
AISTAT_DB_PATH=/home/CPANEL_USER/aistat-private/aistat.db
AISTAT_SECURITY_DB_PATH=/home/CPANEL_USER/aistat-private/security.db
AISTAT_TENANTS_DIR=/home/CPANEL_USER/aistat-private/tenants
AISTAT_ALLOWED_HOSTS=aistat.app,www.aistat.app
AISTAT_FORCE_HTTPS=1
AISTAT_SESSION_COOKIE_SECURE=1
AISTAT_ADMIN_USERNAME=<логин>
AISTAT_ADMIN_EMAIL=<email владельца, рекомендуется>
AISTAT_PASSWORD_HASH=<результат hash-password>
AISTAT_SESSION_SECRET=<первый секрет>
AISTAT_INGEST_SECRET=<второй секрет>
```

### Одноразово перенести текущие данные владельца

После загрузки нового кода, но до запуска publisher выполнить на хостинге:

```bash
cd "$HOME/aistat_app"
set -a
. "$HOME/aistat-private/aistat.env"
set +a
python3 -m aistat.migrate
```

Команда совместима с Python 3.6, повторный запуск безопасен. Она:

- находит единственного `users.is_admin=1`, либо назначает пользователя с
  `AISTAT_ADMIN_EMAIL`, либо создаёт владельца из `AISTAT_ADMIN_USERNAME`;
- создаёт согласованную копию старой `AISTAT_DB_PATH` в
  `AISTAT_TENANTS_DIR/<owner_user_id>.db`;
- сохраняет rollback-копию `<AISTAT_DB_PATH>.migrated`;
- переносит старый replay watermark в запись владельца и печатает JSON с
  `owner_user_id`.

Если команда сообщает неоднозначность владельца или отличающийся существующий
tenant-файл, она ничего не перезаписывает: сначала нужно исправить данные
`security.db`, а не выбирать пользователя по первой строке.

После миграции перезапустить Python App. `https://aistat.app/healthz` должен
вернуть только минимальный статус; дашборд должен перенаправить на `/login`, а
после входа владельца показать прежнюю статистику.

## 5. Вход через Google (OAuth) — опционально

Вход через Google встаёт поверх общего authorization-code ядра: провайдер — это
чистая конфигурация через переменные окружения, поэтому парольный вход
администратора продолжает работать без изменений, а Google подключается
опционально. Если блок ниже не задавать, приложение остаётся на входе по
логину/паролю. Все значения Google задаются там же, где остальные секреты
(Passenger Environment Variables или `$HOME/aistat-private/aistat.env`), и
**никогда не коммитятся**: реальные `client_id` / `client_secret` живут только
на сервере. Готовые плейсхолдеры собраны в `.env.example`.

### Шаг 1. Создать OAuth-клиент в Google Cloud Console

1. Открыть [Google Cloud Console](https://console.cloud.google.com/) и создать
   (или выбрать) проект.
2. `APIs & Services → OAuth consent screen`:
   - User type — **External**;
   - заполнить название приложения, support email и контакт разработчика;
   - Authorized domain — `aistat.app`;
   - scopes — `openid`, `email`, `profile` (неконфиденциальные);
   - пока экран в статусе **Testing**, добавить нужные аккаунты в **Test
     users**; для общедоступного входа — **Publish app**.
3. `APIs & Services → Credentials → Create credentials → OAuth client ID`:
   - Application type — **Web application**;
   - **Authorized redirect URIs** — добавить точь-в-точь
     `https://aistat.app/auth/google/callback` (тот же адрес пойдёт в
     `AISTAT_OAUTH_GOOGLE_REDIRECT_URI`; если вход бывает и на `www`, добавить
     также `https://www.aistat.app/auth/google/callback`).
4. Сохранить и скопировать выданные **Client ID** и **Client secret**.

Redirect URI сверяется побайтово: схема (`https`), хост и путь
`/auth/google/callback` должны совпадать с тем, что настроено в приложении,
иначе Google отклонит вход.

### Шаг 2. Задать переменные окружения

Добавить к остальным переменным (Passenger Environment Variables или
`aistat.env`):

```text
AISTAT_OAUTH_PROVIDERS=google
AISTAT_OAUTH_GOOGLE_AUTHORIZE_URL=https://accounts.google.com/o/oauth2/v2/auth
AISTAT_OAUTH_GOOGLE_TOKEN_URL=https://oauth2.googleapis.com/token
AISTAT_OAUTH_GOOGLE_USERINFO_URL=https://openidconnect.googleapis.com/v1/userinfo
AISTAT_OAUTH_GOOGLE_SCOPES=openid email profile
AISTAT_OAUTH_GOOGLE_CLIENT_ID=<Client ID из Google>
AISTAT_OAUTH_GOOGLE_CLIENT_SECRET=<Client secret из Google>
AISTAT_OAUTH_GOOGLE_REDIRECT_URI=https://aistat.app/auth/google/callback
# опционально: список email, которым разрешена ПЕРВАЯ регистрация
# AISTAT_OAUTH_ALLOWED_EMAILS=you@example.com
```

Провайдер включается, только когда заданы **все** его поля (три URL, scopes,
client id/secret, redirect uri). Если хоть одно пустое, Google молча не
подключится и сайт останется на парольном входе — это защита от
полунастроенного провайдера. Имя провайдера в `AISTAT_OAUTH_PROVIDERS` и префикс
`AISTAT_OAUTH_GOOGLE_` должны совпадать; так же (данными, без правки кода) позже
добавляется Yandex.

### Шаг 3. Модель доступа

- **Кнопка входа.** Когда провайдер `google` настроен, на странице `/login`
  появляется кнопка «Войти / зарегистрироваться через Google» (в обоих
  контурах — Flask и dependency-free). Маршруты — `/auth/google/start` и
  `/auth/google/callback`.
- **Открытая регистрация (по умолчанию).** Если `AISTAT_OAUTH_ALLOWED_EMAILS`
  пуст или не задан — любой Google-пользователь с подтверждённым email при
  первом входе получает собственный обычный аккаунт (`is_admin=0`) и пустой
  tenant.
- **Регистрация по списку.** Если задать `AISTAT_OAUTH_ALLOWED_EMAILS` (через
  запятую, регистр не важен) — новый пользователь должен совпасть со списком,
  иначе видит страницу «Регистрация закрыта» (403) без раскрытия деталей. Уже
  зарегистрированные аккаунты этот список не блокирует.
- **Владелец.** Google-вход с подтверждённым email, равным `AISTAT_ADMIN_EMAIL`
  (из раздела 4), привязывается к существующему аккаунту владельца: владелец
  остаётся единственным админом и сохраняет парольный вход и свой tenant.
- **Требование к email.** Вход принимается только с провайдер-подтверждённым
  (`email_verified`) и корректным email; иначе он безопасно отклоняется, не
  создавая ни пользователя, ни сессии. Один и тот же Google-аккаунт (`sub`)
  всегда попадает в один и тот же аккаунт AIStat, даже если позже сменит email.

### Шаг 4. Применить и проверить

Для Passenger — **Restart** в `Setup Python App`; CGI-вход перечитывает
конфигурацию на каждом запросе и рестарта не требует. Затем открыть
`https://aistat.app/login`: под формой должна появиться кнопка Google, а вход
через неё создаёт или находит аккаунт и возвращает на дашборд.

## 6. Включить безопасную синхронизацию Multica

На локальном Mac сохранить ingest secret в login Keychain:

```bash
security add-generic-password \
  -U \
  -a "$USER" \
  -s "aistat.app ingest" \
  -w '<тот же ingest secret>'
```

Синхронизацию и публикацию на Mac держит автономный локальный рантайм —
supervisor `com.aistat.runtime`
(см. [runtime-supervisor.md](runtime-supervisor.md)). Его конфигурация и
секреты живут в персистентном owner-only env-файле
`~/.config/aistat/production.env` (обычный файл, права `0600`; Keychain выше
нужен только для ручной проверки ниже):

```text
AISTAT_TENANT_ID=<owner_user_id из aistat.migrate>
AISTAT_PUBLISH_URL=https://aistat.app/api/ingest/snapshot
AISTAT_SESSION_SECRET=<≥32 байта, тот же, что на хосте>
AISTAT_INGEST_SECRET=<тот же ingest secret, что на хосте>
AISTAT_WORKER_SYNC_URL=https://aistat.app
AISTAT_WORKER_SECRET=<≥32 байта, тот же, что на хосте>
```

Первичная ручная проверка:

```bash
AISTAT_INGEST_SECRET="$(security find-generic-password \
  -a "$USER" -s 'aistat.app ingest' -w)" \
  AISTAT_PUBLISH_URL=https://aistat.app/api/ingest/snapshot \
  AISTAT_TENANT_ID=<owner_user_id> \
  .venv/bin/python -m aistat.publish
```

Для автозапуска установить рантайм вне защищённого macOS каталога
`Documents` (все пути выводятся из `$HOME`):

```bash
deploy/aistat_runtime.sh preflight
deploy/aistat_runtime.sh install
```

`install` собирает runtime-копию из текущего Git-коммита в
`~/Library/Application Support/AIStat` (случайные незакоммиченные изменения
не попадают в работающий сервис), сохраняет базу там же и регистрирует
launchd-задание `com.aistat.runtime`. Если на машине ещё работает
legacy-задание `com.aistat.sync` (его ставил retired-скрипт
`scripts/install_launchd_sync.sh`), install автоматически снимает его **до**
запуска нового supervisor'а и продолжает работать на тех же данных —
дублирующихся poller/publisher не остаётся. Путь обновления и отката описан
в [runtime-supervisor.md](runtime-supervisor.md).

## 7. Автообновление сайта через cPanel Git + cron (ежедневно в 05:00)

Прод обновляется раз в сутки без передачи каких-либо доступов: хостинг сам
забирает код из **публичного** репозитория `FomaBy/AIStat`, а cron в 05:00
собирает пакет и атомарно публикует его. Ни одного секрета на стороне
CI / Mac / агента не появляется. Данные (`~/aistat-private`) и 5-минутный цикл
публикации (runtime `com.aistat.runtime` на Mac) этот механизм не трогает.

### Шаг 1. Клонировать репозиторий (cPanel → Git Version Control)

`cPanel → Files → Git™ Version Control → Create`:

- **Clone URL**: `https://github.com/FomaBy/AIStat.git`
- **Repository Path**: `repositories/AIStat`
  (полный путь получится `/home/CPANEL_USER/repositories/AIStat`)
- **Repository Name**: `AIStat`

Нажать **Create** — cPanel сделает начальный clone ветки `main`.

### Шаг 2. Добавить cron-задачу (cPanel → Cron Jobs)

`cPanel → Advanced → Cron Jobs → Add New Cron Job`. Часовой пояс сервера
Namecheap может отличаться от часового пояса владельца. Чтобы обновление всегда
проходило в **05:00 Europe/Vilnius**, включая переходы на летнее и зимнее время,
cron запускает лёгкую проверку каждый час:

- Minute `0`;
- Hour `*`;
- Day `*`;
- Month `*`;
- Weekday `*`.

Command (одной строкой, `$HOME` cPanel подставит сам):

```bash
LC_ALL=C TZ=Europe/Vilnius /bin/date | /bin/grep -q ' 05:' && /bin/bash "$HOME/repositories/AIStat/deploy/cpanel_deploy.sh" >> "$HOME/aistat-private/deploy.log" 2>&1
```

Готово: каждый день в 05:00 по Вильнюсу сайт обновляется до последнего `main`,
независимо от часового пояса cPanel-сервера.

### Что делает `deploy/cpanel_deploy.sh`

1. `git fetch` + `git reset --hard origin/main` в клоне;
2. собирает dependency-free пакет (`scripts/build_cpanel_package.sh`, без `zip`);
3. проверяет пакет через `python3 -m compileall` — при ошибке деплой **не
   выполняется**;
4. кладёт новую версию в `~/aistat_releases/<дата>-<sha>` и атомарно
   переключает симлинк `~/aistat_app` на неё (`ln -sfn`);
5. хранит последние релизы (по умолчанию 5) для отката.

Любой сбой на шагах 1–3 прекращает деплой **до** переключения симлинка — прод
продолжает работать на прежнем релизе. CGI-вход перечитывает код на каждом
запросе, поэтому перезапуск не нужен (для Passenger — Restart в Setup Python App).

### Как убедиться, что деплой прошёл

```bash
tail -n 20 ~/aistat-private/deploy.log   # строки "PUBLISHED ... -> <релиз>" и "deploy complete: <sha>"
ls -l ~/aistat_app                        # симлинк на ~/aistat_releases/<дата>-<sha>
ls -1t ~/aistat_releases                  # список релизов, новейший сверху
curl -I https://aistat.app/               # сайт отвечает (редирект на /login)
```

### Откат на предыдущую версию

```bash
ls -1t ~/aistat_releases                  # выбрать предыдущий релиз
ln -sfn ~/aistat_releases/<предыдущий> ~/aistat_app
```

При первом запуске существующий каталог `~/aistat_app` из ручной установки
сохраняется как релиз `manual-<дата>` — на него тоже можно откатиться.

## Защита данных

- все страницы, API и статические dashboard-assets требуют входа;
- cookie `aistat_session`: непрозрачный случайный токен (≥256 бит), без
  подписанного/сериализованного envelope и без клиентских claim'ов (email,
  провайдер, user/tenant ID, роль, CSRF, срок), `HttpOnly`, `Secure`,
  `SameSite=Lax`, `Path=/`, срок по умолчанию 12 ч;
- вся авторитетная сессия (user_id, срок, CSRF, отзыв) хранится server-side в
  security.db; в базе лежит только SHA-256 хеш токена, а не сам токен; каждый
  защищённый запрос резолвит cookie в server-side запись; logout/отзыв удаляют
  запись, поэтому скопированный до выхода cookie и любой старый
  подписанный/структурированный cookie сразу получают `401`, а истёкшие записи
  чистятся при новых входах;
- повторный вход в том же браузере ротирует токен и инвалидирует предыдущий;
- login и logout защищены CSRF (CSRF-токен приходит через `/api/session`, а не
  в cookie; форма входа защищена отдельным одноразовым `aistat_login_csrf`);
- после пяти неудачных входов IP-hash блокируется на 15 минут;
- CSP, HSTS, `frame-ancestors 'none'`, `nosniff`, no-referrer и no-store;
- разрешены только заданные Host headers;
- tenant SQLite и security DB имеют права `600`, каталог tenants — `700`;
- Multica credential остаётся только на локальном Mac;
- ingest secret отделён от session secret; tenant ID входит в HMAC, подпись
  сравнивается constant-time, replay-защита ведётся per tenant;
- snapshot проверяется на размер, gzip bomb, SQLite integrity, обязательные
  таблицы и совместимую версию схемы;
- предыдущий snapshot сохраняется как `<user_id>.db.previous` для отката.
