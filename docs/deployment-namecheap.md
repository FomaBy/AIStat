# Развёртывание AIStat на Namecheap Shared Hosting

## Архитектура

Namecheap Shared Hosting поддерживает WSGI, но не ASGI. Поэтому локальный
FastAPI остаётся средой разработки, а cPanel запускает
`passenger_wsgi.py` → `aistat.wsgi`.

Multica CLI и его токен **не устанавливаются на публичный сервер**. Локальный
доверенный Mac продолжает собирать данные через `multica`, создаёт согласованный
SQLite snapshot и отправляет его на сайт по HTTPS. Запрос подписан отдельным
HMAC-секретом; сервер проверяет подпись, срок запроса, защиту от повтора,
целостность SQLite и версию схемы, затем атомарно заменяет только файл данных.

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

## 3. Создать Python App

В `cPanel → Setup Python App`:

- Python: 3.11 или новее;
- Application root: `aistat_app`;
- Application URL: домен `aistat.app`, путь `/`;
- Startup file: `passenger_wsgi.py`;
- Entry point: `application`.

Собрать архив:

```bash
./scripts/build_cpanel_package.sh
```

Загрузить содержимое `dist/aistat-cpanel.zip` в application root. В созданном
virtualenv выполнить:

```bash
pip install -r requirements-cpanel.txt
```

## 4. Хранить данные вне web root

Через cPanel Terminal/SSH:

```bash
mkdir -p "$HOME/aistat-private"
chmod 700 "$HOME/aistat-private"
```

В Environment Variables приложения задать:

```text
AISTAT_DB_PATH=/home/CPANEL_USER/aistat-private/aistat.db
AISTAT_SECURITY_DB_PATH=/home/CPANEL_USER/aistat-private/security.db
AISTAT_ALLOWED_HOSTS=aistat.app,www.aistat.app
AISTAT_FORCE_HTTPS=1
AISTAT_SESSION_COOKIE_SECURE=1
AISTAT_ADMIN_USERNAME=<логин>
AISTAT_PASSWORD_HASH=<результат hash-password>
AISTAT_SESSION_SECRET=<первый секрет>
AISTAT_INGEST_SECRET=<второй секрет>
```

Перезапустить Python App. `https://aistat.app/healthz` должен вернуть только
минимальный статус; сам дашборд должен перенаправить на `/login`.

## 5. Включить безопасную синхронизацию Multica

На локальном Mac создать `~/.config/aistat/production.env` с правами `600`:

```text
AISTAT_PUBLISH_URL=https://aistat.app/api/ingest/snapshot
AISTAT_INGEST_SECRET=<тот же ingest secret>
AISTAT_PUBLISH_INTERVAL_SECONDS=300
```

```bash
chmod 600 ~/.config/aistat/production.env
cd /Users/sergeyfomin/Documents/AIStat
./sync_to_host.sh
```

Первичная ручная проверка:

```bash
set -a
. ~/.config/aistat/production.env
set +a
.venv/bin/python -m aistat.publish
```

Для автозапуска скопировать `deploy/com.aistat.sync.plist.example` в
`~/Library/LaunchAgents/com.aistat.sync.plist`, затем:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.aistat.sync.plist
```

## Защита данных

- все страницы, API и статические dashboard-assets требуют входа;
- cookie: signed, `HttpOnly`, `Secure`, `SameSite=Lax`, срок по умолчанию 12 ч;
- login и logout защищены CSRF;
- после пяти неудачных входов IP-hash блокируется на 15 минут;
- CSP, HSTS, `frame-ancestors 'none'`, `nosniff`, no-referrer и no-store;
- разрешены только заданные Host headers;
- SQLite и security DB имеют права `600` и находятся вне web root;
- Multica credential остаётся только на локальном Mac;
- ingest secret отделён от session secret, подпись сравнивается constant-time;
- snapshot проверяется на размер, gzip bomb, SQLite integrity, обязательные
  таблицы и совместимую версию схемы;
- предыдущий snapshot сохраняется как `aistat.db.previous` для отката.
