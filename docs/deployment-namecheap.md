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
chmod 700 "$HOME/aistat-private"
```

Для Passenger задать Environment Variables приложения. Для CGI сохранить те же
строки в `$HOME/aistat-private/aistat.env`:

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

На локальном Mac сохранить ingest secret в login Keychain:

```bash
security add-generic-password \
  -U \
  -a "$USER" \
  -s "aistat.app ingest" \
  -w '<тот же ingest secret>'
```

`sync_to_host.sh` по умолчанию берёт этот секрет из Keychain, публикует на
`https://aistat.app/api/ingest/snapshot` и использует интервал 300 секунд:

```bash
cd /Users/sergeyfomin/Documents/AIStat
./sync_to_host.sh
```

Для нестандартного URL/интервала можно создать
`~/.config/aistat/production.env` с правами `600`; хранить там ingest secret
не требуется:

```text
AISTAT_PUBLISH_URL=https://aistat.app/api/ingest/snapshot
AISTAT_PUBLISH_INTERVAL_SECONDS=300
```

Первичная ручная проверка:

```bash
AISTAT_INGEST_SECRET="$(security find-generic-password \
  -a "$USER" -s 'aistat.app ingest' -w)" \
  AISTAT_PUBLISH_URL=https://aistat.app/api/ingest/snapshot \
  .venv/bin/python -m aistat.publish
```

Для автозапуска установить отдельную runtime-копию вне защищённого macOS
каталога `Documents`:

```bash
./scripts/install_launchd_sync.sh
```

Скрипт копирует только исполняемый код в
`~/Library/Application Support/AIStat`, сохраняет базу там же и регистрирует
`com.aistat.sync`. Runtime собирается из текущего Git-коммита, поэтому случайные
незакоммиченные изменения не попадают в работающий сервис. Секрет остаётся в
login Keychain.

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
