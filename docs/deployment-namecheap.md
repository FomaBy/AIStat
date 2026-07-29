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

## 3. Развернуть приложение (только первичная установка)

Пакет всегда собирается из **заранее одобренной** пары full commit SHA +
root tree SHA — той же, что проходит approval gate раздела 7 (шаг 2). Не
вычислять значения из `origin/main` в момент сборки: вершина ветки к этому
моменту могла уйти вперёд одобренного кандидата, и раздел 7 прямо запрещает
подставлять текущий mutable `origin/main` вместо ожидаемой пары.

Собрать архив из точного commit tree (рабочий каталог и untracked/ignored
файлы источником не являются):

```bash
EXPECTED_SHA=<40-символьный approved commit SHA>
EXPECTED_TREE=<40-символьный approved root tree SHA>
git fetch origin main
./scripts/build_cpanel_package.sh "$EXPECTED_SHA" "$EXPECTED_TREE"
```

Ручная загрузка содержимого `dist/aistat-cpanel.zip` в `$HOME/aistat_app`
допустима **только до первого выката по разделу 7**, пока этот путь — обычный
каталог. После раздела 7 `$HOME/aistat_app` — **живой symlink** на каталог
текущего release: запись «в него» проходит сквозь symlink внутрь живого
release, загрязняет его и блокирует следующий deploy на проверке
live-манифеста. Все обновления работающего сайта выполняются только
процедурой раздела 7 (пиннинг approved-кандидата + `deploy/cpanel_deploy.sh`).

Дополнительные Python-пакеты не требуются. В пакет входит
`PACKAGE-MANIFEST.json`: полный commit/tree кандидата и отсортированные SHA-256,
размеры и режимы всех payload-файлов.

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

## 7. Пиннинг и публикация approved-кандидата через cPanel Git + cron

Прод принимает только заранее одобренную пару **full commit SHA + root tree
SHA** из `main`. Скрипт никогда не подставляет текущий mutable `origin/main`
вместо ожидаемого кандидата: собирается и публикуется ровно запиненный commit.
«Одобренность» — это принадлежность истории `main`, а не совпадение с её
вершиной, поэтому продвижение `main` вперёд не делает уже одобренного кандидата
невыкатываемым. Cron раз в сутки проверяет и при необходимости публикует именно
запиненную пару; уже работающий exact-кандидат даёт безопасный `ALREADY LIVE`
с кодом возврата 0 и без строки `ERROR` в `deploy.log`.

Fail-closed сохраняется для всего, что не является одобренным кандидатом. У
gate шесть отказных ветвей; в `deploy.log` они различимы по сообщению (перед
каждым стоит фаза — `pre-build` или `pre-publish`):

- `fetch failed — keeping current release` — сам `git fetch origin main`
  завершился ошибкой (сеть, хостинг, права); о кандидате ещё ничего не
  известно;
- `candidate commit missing after fetch: <sha>` — commit-объект отсутствует в
  клоне даже после fetch (например, shallow-клон без истории до пина);
- `candidate is not approved: <sha> is not contained in origin/main` —
  настоящий отзыв одобрения: commit существует, но не содержится в
  `origin/main` (force-push переписал ветку, либо кандидат в неё не попал);
- `could not test candidate against origin/main (git exit N)` — проверку
  принадлежности выполнить не удалось: это сломанный репозиторий (например,
  `git exit 128` при отсутствующем tracking ref), а **не** отзыв одобрения —
  чинить нужно клон, а не искать плохой кандидат;
- `could not resolve candidate tree: <sha>` — у commit не резолвится root tree;
- `tree drift: expected <tree>, candidate <tree>` — commit одобрен, но его
  root tree не совпадает с ожидаемым из пары.

Проверка выполняется дважды. На `pre-build` отказ происходит **до** reset
checkout, сборки и staging. На `pre-publish` release уже собран, поэтому отказ
происходит до commit-point: live-symlink остаётся прежним, а неопубликованный
stage удаляется. Ни в одном случае действующая live-цель не меняется.

Четыре следствия новой семантики, которые важно знать оператору:

- **Revert на `main` не отзывает пин.** Отменённый commit остаётся достижимым из
  `origin/main`, поэтому продолжает считаться одобренным. Чтобы прекратить
  выкат плохого кандидата, нужно заменить пару SHA/tree в cron (при
  необходимости — сделать rollback); одного revert недостаточно.
- **Пин старого предка оставляет сам инструмент на старой версии.** Deploy
  делает `git reset --hard` в `~/repositories/AIStat`, то есть следующий запуск
  cron исполнит `cpanel_deploy.sh` и `build_cpanel_package.sh` из запиненного
  commit. При откате пина далеко назад это возвращает и старое поведение
  деплой-скриптов.
- **Клон должен содержать историю до пина.** Fetch тянет только `main`; в
  shallow-клоне старый предок будет отсутствовать, и gate откажет с
  `candidate commit missing after fetch`.
- **Клон `~/repositories/AIStat` управляется скриптом и не предназначен для
  ручного редактирования.** Ежедневный `git reset --hard` на запиненный
  кандидат выполняется **до** короткого замыкания `ALREADY LIVE`, то есть
  срабатывает и в «тихие» дни без публикации. Любые локальные правки в этом
  клоне стираются следующим запуском cron.

Репозиторий публичный, поэтому fetch не требует production credentials. Секреты
приложения, данные (`~/aistat-private`) и 5-минутный цикл публикации
(`com.aistat.runtime` на Mac) механизм не читает и не меняет.

### Шаг 1. Клонировать репозиторий (cPanel → Git Version Control)

`cPanel → Files → Git™ Version Control → Create`:

- **Clone URL**: `https://github.com/FomaBy/AIStat.git`
- **Repository Path**: `repositories/AIStat`
  (полный путь получится `/home/CPANEL_USER/repositories/AIStat`)
- **Repository Name**: `AIStat`

Нажать **Create** — cPanel сделает начальный clone ветки `main`.

### Шаг 2. Зафиксировать approved SHA/tree

После independent QA взять из её evidence **полные** значения кандидата и
проверить их в cPanel-клоне:

```bash
EXPECTED_SHA=<40-символьный approved commit SHA>
EXPECTED_TREE=<40-символьный approved root tree SHA>
git -C "$HOME/repositories/AIStat" fetch origin main \
  && git -C "$HOME/repositories/AIStat" merge-base --is-ancestor "$EXPECTED_SHA" origin/main \
  && test "$(git -C "$HOME/repositories/AIStat" rev-parse "${EXPECTED_SHA}^{tree}")" = "$EXPECTED_TREE" \
  && echo "APPROVED $EXPECTED_SHA"
```

Обе проверки молчаливы при успехе, поэтому они соединены `&&` с финальным
`echo`: единственный признак прохождения gate — строка `APPROVED`. Её
отсутствие означает, что кандидат не одобрен, и продолжать нельзя.

Проверяется принадлежность кандидата истории `main` и точное совпадение tree, а
не равенство вершине: к моменту пиннинга `main` уже может уйти вперёд. Short
SHA, имя ветки или автоматически вычисленное после fetch значение не заменяет
этот approval gate.

### Шаг 3. Добавить cron-задачу (cPanel → Cron Jobs)

`cPanel → Advanced → Cron Jobs → Add New Cron Job`. Часовой пояс сервера
Namecheap может отличаться от часового пояса владельца. Чтобы обновление всегда
проходило в **05:00 Europe/Vilnius**, включая переходы на летнее и зимнее время,
cron запускает лёгкую проверку времени каждый час, но сам deploy выполняется
**один раз в сутки** — в единственный час, прошедший фильтр:

- Minute `0`;
- Hour `*`;
- Day `*`;
- Month `*`;
- Weekday `*`.

Command (одной строкой; заменить оба placeholder точными approved-значениями,
`$HOME` cPanel подставит сам):

```bash
LC_ALL=C TZ=Europe/Vilnius /bin/date | /bin/grep -q ' 05:' && /bin/bash "$HOME/repositories/AIStat/deploy/cpanel_deploy.sh" deploy <FULL_COMMIT_SHA> <FULL_TREE_SHA> >> "$HOME/aistat-private/deploy.log" 2>&1
```

При принятии следующего кандидата заменить в cron **оба** значения одной
операцией. До этого сайт остаётся на прежнем approved-кандидате независимо от
движения `main`, а ежедневный запуск на уже живом кандидате остаётся тихим
(`ALREADY LIVE`, exit 0). Строка `ERROR` в `deploy.log` означает реальную
проблему, а не то, что `main` ушёл вперёд.

### Что делает `deploy/cpanel_deploy.sh`

1. Берёт nonblocking host-local lock
   `~/aistat-private/cpanel-deploy.lock`; concurrent deploy/rollback завершается
   до fetch, staging и publish. Lock держится kernel `flock` через Python
   `fcntl` и автоматически освобождается при exit/crash; оставшийся файл
   удалять не нужно, строка `pid=...` в нём только диагностическая.
2. Делает fetch и проверяет одобренность запиненного кандидата: commit
   существует локально, содержится в `origin/main` и его root tree совпадает с
   ожидаемым. Вершина `origin/main` при этом может быть любой.
3. Сбрасывает checkout на точный commit и строит пакет только через
   `git archive <expected-sha>`; tracked-правки, untracked и ignored файлы хоста
   в release не попадают.
4. Проверяет манифест в режиме `strict`, принудительно компилирует весь пакет,
   отдельно `aistat.cgi` и `passenger_wsgi.py`, затем импортирует
   Passenger-контур с временными `AISTAT_DB_PATH`, `AISTAT_SECURITY_DB_PATH` и
   `AISTAT_TENANTS_DIR`. Production DB/tenant paths в smoke не используются.
5. Создаёт уникальный release и повторяет fetch + тот же approval gate
   непосредственно перед publish. Продвижение `main` между двумя fetch
   безопасно и публикацию не отменяет; отзыв одобрения (кандидат перестал
   содержаться в `origin/main`) или расхождение tree оставляют live link
   прежним и удаляют unpublished stage.
6. Создаёт новый symlink рядом с `~/aistat_app`, затем делает один атомарный
   `os.replace` на том же host filesystem. Перед commit-point прежняя live-цель
   повторно сверяется: тот же exact target, валидной формы, прямым дочерним
   каталогом `~/aistat_releases` и не исчезнувший во время проверки.
7. Хранит live и exact previous target. `AISTAT_KEEP_RELEASES` — это **общее**
   число оставляемых managed-release, **включая** live и previous: при default
   `5` на диске остаётся 5 каталогов release всего, а не 5 сверх двух
   защищённых. Принимается только `0` (не удалять релизы) или любое
   каноническое целое `>=2` — как однозначное (`2`, `9`), так и многозначное
   (`10`, `19`, `100`). Значение `1` запрещено именно из-за этой базы отсчёта:
   live и exact previous не удаляются никогда, поэтому общий лимит `1` при
   двух защищённых целях недостижим. Пустое значение
   (`AISTAT_KEEP_RELEASES=""`) не отклоняется, а означает «не задано» и даёт
   default `5` — до валидации формата оно не доходит. Значения `1`,
   отрицательные, с ведущим нулём, дробные и нечисловые отклоняются до
   каких-либо изменений на диске.

Любая ошибка до `os.replace` прекращает deploy без изменения live target.
После switch cleanup retention является best-effort и никогда не удаляет live
или exact previous release. CGI перечитывает код на каждом запросе; для
Passenger после публикации нужен Restart в `Setup Python App`.

Publish и rollback неделимы относительно `TERM`, `HUP` и `INT`: вокруг
commit-point эти сигналы игнорируются, поэтому сигнал не разрывает rename
пополам и не удаляет только что опубликованный release, а cleanup никогда не
удаляет каталог, на который фактически указывает `~/aistat_app`. Deploy и
rollback всегда оставляют валидную live-цель — прежнюю или новую.

Проверка манифеста имеет два режима. `strict` — для свежесобранного package и
для staged release до публикации: состав файлов должен совпадать с манифестом
точно, без исключений. `runtime` — для release, который уже обслуживал трафик:
текущая live-цель при следующем deploy и target при rollback; здесь допускаются
ровно каталоги `__pycache__/` и файлы `*.pyc` внутри них, и больше ничего.

Причина исключения: `aistat.cgi` работает как отдельный CGI-процесс на запрос
под `#!/usr/bin/python3` (на хосте это CPython 3.6.8), поэтому интерпретатор
пишет `__pycache__/*.cpython-36.pyc` рядом с исходниками — то есть через
live-symlink внутрь опубликованного release. Это нормальная работа, а не
повреждение пакета.

Целостность при этом не ослаблена: sha256, размер и режим каждого файла из
манифеста проверяются всегда и в обоих режимах. Любой другой лишний или
пропавший путь по-прежнему fail-closed останавливает deploy/rollback, а ошибка
перечисляет конкретные пути:
`manifest payload set does not match package (unexpected paths: ...)`.

Отдельно: каталог `data/` внутри release — это **ошибка, а не allowlist**. Он
появляется, когда приватный env-файл `~/aistat-private/aistat.env` не задаёт
`AISTAT_DB_PATH`, `AISTAT_SECURITY_DB_PATH` и `AISTAT_TENANTS_DIR`: тогда
`aistat/legacy_wsgi.py` резолвит их по умолчанию в `<app_root>/data`, то есть
внутрь release, и следующий deploy остановится. Лечение — задать все три
переменные вне release в `~/aistat-private/aistat.env` (см. `.env.example` и
раздел 4), перенести накопленные там базы в `~/aistat-private`, затем убрать
созданный внутри release каталог `data/`.

### Переменные окружения deploy-инструментов

Все переменные ниже опциональны: в обычной cPanel-инсталляции ни одна не
задаётся и работают значения по умолчанию.

`deploy/cpanel_deploy.sh`:

- `AISTAT_APP_ROOT` — путь live-symlink приложения; default `$HOME/aistat_app`.
  Это не только деплойный, но и **runtime-knob**: он также есть в
  `.env.example` для CGI-окружения, а `aistat.cgi` читает его на каждом запросе,
  чтобы найти каталог приложения для импорта кода. Если менять его для deploy,
  ту же переменную нужно задать и CGI-окружению — иначе сайт продолжит
  обслуживаться из старого пути.
- `AISTAT_RELEASES_DIR` — каталог managed-release; default
  `$HOME/aistat_releases`. Только его прямые дочерние каталоги считаются
  release-целями, и только внутри него работает retention.
- `AISTAT_DEPLOY_LOCK_FILE` — host-local lock-файл deploy/rollback; default
  `$HOME/aistat-private/cpanel-deploy.lock`.
- `AISTAT_KEEP_RELEASES` — retention; default `5`. Семантика и база отсчёта —
  в пункте 7 выше: это общее число хранимых release, включая live и previous.
- `AISTAT_DEPLOY_LIB_ONLY` — default пусто / переменная не задана: deploy-логика
  выполняется штатно. Непустое значение заставляет `source`-загрузку скрипта
  отдать только чистые helper-функции, не выполняя deploy-логику. Используется
  focused-тестами; оператору задавать не нужно.

`scripts/build_cpanel_package.sh` (вызывается deploy-скриптами, но может
запускаться и вручную, как в разделе 3):

- `AISTAT_SOURCE_REPOSITORY` — git-репозиторий, из которого резолвится и
  архивируется кандидат; default — корень репозитория, содержащего сам скрипт.
- `AISTAT_EXPECTED_SHA`, `AISTAT_EXPECTED_TREE` — env-альтернатива двум
  позиционным аргументам (full commit SHA и root tree SHA); default нет, пара
  обязательна в том или ином виде, позиционные аргументы имеют приоритет.
- `AISTAT_SKIP_ZIP` — значение `1` пропускает сборку `dist/aistat-cpanel.zip`
  и оставляет только каталог `dist/aistat-cpanel`; default `0` (архив
  собирается). Так работает ежедневный cPanel-deploy: ему нужен только
  каталог, а `zip` на shared-хосте может отсутствовать.

### Как убедиться, что деплой прошёл

```bash
tail -n 30 ~/aistat-private/deploy.log   # full commit/tree, previous/new target, manifest_sha256
readlink ~/aistat_app                    # exact абсолютный live target
python3 -m json.tool "$(readlink ~/aistat_app)/PACKAGE-MANIFEST.json" >/dev/null
ls -1t ~/aistat_releases                # список релизов, новейший сверху
curl -I https://aistat.app/             # сайт отвечает (редирект на /login)
```

Успешная строка `PUBLISHED` содержит full commit SHA, tree SHA, exact previous
и new release paths и SHA-256 самого manifest без секретов.

### Первый переход с ручного каталога

Обычный deploy намеренно **не** перемещает существующий реальный каталог
`~/aistat_app`: заменить непустой directory на symlink одним переносимым
atomic rename нельзя. Выполнить переход отдельно в объявленное maintenance-окно
и заранее сохранить имя backup:

```bash
STAMP="$(date '+%Y%m%d-%H%M%S')"
MANUAL_BACKUP="$HOME/aistat-manual-backup-$STAMP"
mv "$HOME/aistat_app" "$MANUAL_BACKUP"
/bin/bash "$HOME/repositories/AIStat/deploy/cpanel_deploy.sh" deploy <FULL_COMMIT_SHA> <FULL_TREE_SHA>
```

Процедура не требует, чтобы approved-кандидат был вершиной `main` на момент
выполнения: подойдёт любой одобренный commit, содержащийся в `origin/main`. По
той же причине можно перевыпустить release после потери каталога, не сдвигая
пин.

Между `mv` и успешным deploy возможна краткая недоступность — поэтому это
только maintenance procedure, не cron. Если deploy завершился ошибкой до
создания `~/aistat_app`, немедленно вернуть ручную версию:

```bash
test -e "$HOME/aistat_app" || mv "$MANUAL_BACKUP" "$HOME/aistat_app"
```

После проверки нового exact release backup можно оставить до следующего
maintenance-window или удалить вручную; deploy/retention его не трогает.

### Откат на предыдущую версию

```bash
CURRENT="$(readlink "$HOME/aistat_app")"
ls -1t "$HOME/aistat_releases"           # выбрать существующий previous exact release
TARGET="$HOME/aistat_releases/<полное-имя-релиза>"
/bin/bash "$HOME/repositories/AIStat/deploy/cpanel_deploy.sh" rollback "$TARGET"
```

Rollback принимает только существующий **absolute** direct-child target с
валидным `PACKAGE-MANIFEST.json`, берёт тот же lock, повторно проверяет current
target и использует тот же атомарный switch. Git fetch/build и retention при
rollback не выполняются. Для аварийного возврата `MANUAL_BACKUP` используется
отдельное maintenance-окно, описанное выше.

### Восстановление «висящего» live-symlink

Состояние: `~/aistat_app` существует как symlink, но каталог, на который он
указывает, отсутствует (например, release удалили вручную) — сайт не работает.

Сломанная **текущая** live-цель инструмент не блокирует. Проверяется только
форма пути: absolute, без traversal, не symlink, прямой ребёнок
`~/aistat_releases`. Отсутствие самого каталога допускается и логируется
строкой `WARNING: live symlink points at a missing release: ...`. Отказ
остаётся только для невалидного аргумента, переданного оператором, — например
`rollback` на несуществующий target.

Штатное восстановление — обычный rollback на существующий release:

```bash
readlink "$HOME/aistat_app"              # видно, что цель отсутствует
ls -1t "$HOME/aistat_releases"           # выбрать существующий release
TARGET="$HOME/aistat_releases/<полное-имя-релиза>"
/bin/bash "$HOME/repositories/AIStat/deploy/cpanel_deploy.sh" rollback "$TARGET"
```

Обычный `deploy` следующего кандидата из этого состояния тоже проходит и
восстанавливает работу. Удалять сам symlink `~/aistat_app` вручную не нужно и
не помогает: без него `rollback` откажет (`rollback requires an existing live
symlink`), а deploy всё равно создаёт link заново.

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
