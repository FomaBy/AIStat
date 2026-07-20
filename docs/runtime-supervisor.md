# Автономный локальный рантайм AIStat (supervisor)

Доверенная локальная машина держит постоянно поднятыми ровно **четыре**
долгоживущих контура:

| Контур | Команда | Что делает |
|---|---|---|
| owner poller | `python -m aistat.poller` | синхронизирует Multica владельца в локальную БД |
| owner publisher | `python -m aistat.publish --watch` | подписанный snapshot владельца → публичный хост |
| PAT worker | `python -m aistat.worker_sync --watch` | тянет пользовательские токены в зашифрованный store |
| per-user collector | `python -m aistat.collector` | собирает статистику по подключениям и публикует per-tenant |

Всеми четырьмя управляет один **fail-fast supervisor**
(`aistat/supervisor.py`), которого поднимает один launchd-агент
`com.aistat.runtime`. Supervisor:

- держит по одному экземпляру каждого контура (одиночность гарантирует
  `flock` — второй supervisor не запустится);
- перезапускает упавший контур с экспоненциальным backoff;
- при crash-loop (слишком частые перезапуски) завершается с ненулевым кодом,
  launchd поднимает рантайм заново, а проблема остаётся видимой, а не «тихо
  умирает»;
- по SIGTERM/SIGINT (и при `launchctl bootout` во время reinstall/uninstall)
  гасит каждый контур **вместе с его группой процессов**, не оставляя
  осиротевших дочерних процессов (`multica` CLI, per-connection профиль).

## Раскладка рантайма

Рантайм-рут по умолчанию — `$HOME/Library/Application Support/AIStat`
(переопределяется `AISTAT_RUNTIME_ROOT`). Пути в plist и логах генерируются из
реального `$HOME`; захардкоженного имени пользователя нигде нет.

```
<runtime-root>/
  code/        активная копия кода (пакет aistat + манифесты)
  code.prev/   предыдущая копия — для rollback
  data/        постоянное состояние: БД, зашифрованный store, tenants, логи
  .venv/       виртуальное окружение рантайма
```

`data/` лежит **вне** `code/`, поэтому обновление кода никогда не трогает БД
владельца, зашифрованный store, tenant-снимки и логи. Ключ шифрования по
умолчанию `~/.config/aistat/worker.key` — вообще вне рантайм-рута.

## Контракт приватной активации (без значений секретов)

Секреты **никогда** не попадают в plist, argv, stdout/stderr, Git или
тестовые артефакты. Единственный источник секретов — приватный env-файл
`AISTAT_ENV_FILE` (по умолчанию `~/.config/aistat/production.env`), права
`0600`, каталог `0700`. Supervisor загружает его на старте и проверяет права;
небезопасный файл — отказ старта.

**Персистентный env-файл обязателен для активации.** Plist launchd намеренно
не содержит значений секретов — установленный runtime может перечитать
конфигурацию только из этого файла. Поэтому `install`, `preflight`, `restart`
и `rollback` требуют существующий **обычный** (не symlink) файл с правами
`0600`; отсутствующий, симлинкованный, доступный группе/миру или малформный
(не `KEY=VALUE`) файл — отказ **до** любого `launchctl bootout`/`bootstrap` и
до остановки предыдущего runtime. Секреты, экспортированные только в
вызывающем шелле, гейт не проходят: они не переживут перезапуск через
launchd. `uninstall` и `status` остаются доступны без файла как fail-safe.

Файл всегда **парсится** (`aistat.preflight.load_env_file`), а не исполняется
шеллом: малформная строка даёт понятную ошибку с номером строки (без её
содержимого) вместо выполнения кода.

Переопределение конфигурации переменными окружения процесса (без env-файла)
работает только при ручном запуске `python -m aistat.supervisor` и в
изолированных тестах — production-активация через installer всегда требует
персистентный файл.

Минимальный набор для рантайма (значения задаёт владелец, здесь только имена):

```
AISTAT_TENANT_ID=            # внутренний users.id владельца (из aistat.migrate)
AISTAT_PUBLISH_URL=          # https://… — куда publisher шлёт snapshot
AISTAT_SESSION_SECRET=       # ≥32 байта, HMAC сессий; тот же, что на public host
AISTAT_INGEST_SECRET=        # ≥32 байта, независимый HMAC для snapshot
AISTAT_WORKER_SYNC_URL=      # https://… — откуда worker тянет токены
AISTAT_WORKER_SECRET=        # ≥32 байта, независимый HMAC для worker-канала
```

Требования, которые проверяет preflight (`python -m aistat.preflight`):

- задан `AISTAT_TENANT_ID`;
- `AISTAT_PUBLISH_URL` и `AISTAT_WORKER_SYNC_URL` — HTTPS;
- `AISTAT_SESSION_SECRET`, `AISTAT_INGEST_SECRET` и `AISTAT_WORKER_SECRET`
  обязательны, не короче 32 байт и **попарно различаются**;
- интервалы publish/worker-pull/worker-collect ≥ 60 секунд;
- ключ шифрования лежит **не** в каталоге store; ключ/store — owner-only
  (`0600`), каталоги — `0700`;
- все контуры и зависимость `cryptography` импортируются.

Каждый host-side secret генерируется отдельно командой
`python -m aistat.security generate-secret`; в private runtime env копируются
те же три значения, чтобы preflight проверял реальную попарную независимость.

> **Fail-closed intake.** Хост не сохраняет новый PAT, пока не увидит свежий
> подписанный heartbeat worker'а: до первого успешного `worker_sync` intake
> отвечает `503`. Инсталлятор **не** включает `AISTAT_MULTICA_CONNECT_ENABLED`
> — это отдельный хостовый флаг и отдельный этап активации.

## Команды

Всё через один скрипт `deploy/aistat_runtime.sh` (рантайм-рут и пути берутся
из `$HOME`):

```bash
deploy/aistat_runtime.sh preflight    # проверить конфиг до установки
deploy/aistat_runtime.sh install      # поставить/обновить рантайм (транзакционно)
deploy/aistat_runtime.sh status       # статус launchd-задания и контуров
deploy/aistat_runtime.sh restart      # перезапустить рантайм
deploy/aistat_runtime.sh rollback     # откатиться на предыдущую копию кода
deploy/aistat_runtime.sh uninstall            # снять рантайм, данные сохранить
deploy/aistat_runtime.sh uninstall --purge    # снять рантайм и удалить данные
```

### Установка / обновление (транзакционно)

`install` сначала проверяет персистентный env-файл (существование, обычный
файл, `0600`), затем стейджит свежую копию кода, поднимает/обновляет venv,
линтит plist (`plutil -lint`) и прогоняет **полный preflight** (который сам
парсит и загружает env-файл) — и только после этого останавливает старый
рантайм. Порядок:

1. guard персистентного env-файла (без него install не стартует);
2. staging кода → `code.incoming/`, обновление `.venv`;
3. рендер + `plutil -lint` манифеста, полный preflight по новой копии;
4. атомарный swap: `code/ → code.prev/`, `code.incoming/ → code/`;
5. `launchctl bootout` старого + `bootstrap` нового задания, postflight.

Если что-то падает после swap — предыдущая копия автоматически
восстанавливается из `code.prev/` и снова bootstrap'ится. Неудачная установка
никогда не оставляет машину без рабочего рантайма и не удаляет `data/`.
Повторный `install` идемпотентен.

### Откат

```bash
deploy/aistat_runtime.sh rollback     # code.prev/ → code/, повторный bootstrap
```

### Удаление

`uninstall` гасит **оба поколения** рантайма: и supervisor
`com.aistat.runtime`, и legacy-задание `com.aistat.sync`, если оно ещё
установлено. Сначала `bootout` обоих заданий с проверкой, что они
действительно выгружены (задание, пережившее bootout, прерывает uninstall с
ошибкой — файлы не удаляются, чтобы повторная попытка была возможна), затем
удаляются оба plist, копии кода и legacy-артефакты. `data/` и ключ
**сохраняются**; `--purge` дополнительно удаляет `data/`. Ключ
`~/.config/aistat/worker.key` удаляется только вручную.

## Миграция с legacy `com.aistat.sync`

До появления supervisor'а локальный рантайм устанавливался скриптом
`scripts/install_launchd_sync.sh`: одно launchd-задание `com.aistat.sync`
держало owner poller и owner publisher через `sync_to_host.sh` в том же
runtime-root и на тех же данных. Этот скрипт **retired** и теперь только
сообщает об этом; единственный путь установки — `deploy/aistat_runtime.sh
install`, который выполняет миграцию автоматически:

1. После обычных гейтов (env-файл, staging, preflight) install обнаруживает
   legacy-поколение — по plist
   `~/Library/LaunchAgents/com.aistat.sync.plist` и/или загруженному заданию
   — и снимает его **до** bootstrap'а нового supervisor'а: `bootout` с
   проверкой, что задание действительно выгружено (иначе установка
   прерывается), затем удаление legacy plist. Оба поколения никогда не
   работают одновременно — дублирующихся poller/publisher не бывает.
2. `data/` общий для обоих поколений и миграцией не затрагивается: БД
   владельца, зашифрованный store, tenant-базы, логи и ключ остаются на
   месте, новый рантайм продолжает работать на тех же данных.
3. После успешного postflight удаляются legacy-артефакты кода в runtime-root
   (`aistat/`, `sync_to_host.sh`, `pricing.json`, `requirements.txt`,
   `.install-stage`). `data/` и `.venv/` (переиспользуется новым рантаймом)
   не удаляются. Повторный `install` идемпотентен: без legacy-поколения
   миграционных действий нет.
4. Если cutover падает (bootstrap/postflight) **при первой миграции** —
   предыдущего new-gen кода ещё нет — восстанавливается legacy-поколение:
   plist возвращается byte-exact и задание bootstrap'ится заново, а
   полуустановленное новое задание снимается и его plist удаляется. Если
   предыдущий new-gen код существует, восстанавливается он (как обычно), а
   legacy остаётся снятым. В любом исходе на машине остаётся ровно один
   известно-рабочий рантайм.

Обратной миграции нет: `rollback` откатывает только внутри нового поколения
(`code.prev/`) и заодно снимает случайно возродившееся legacy-задание;
`restart` при загруженном `com.aistat.sync` отказывается работать (иначе
получились бы дубликаты) и отправляет на `install`; `status` показывает
состояние обоих поколений (`legacy.loaded`, `legacy.plist_present`). Чтобы
полностью остановить рантайм, используйте `uninstall` — данные сохраняются.

## Логи и статус

- `data/runtime.stdout.log`, `data/runtime.stderr.log` — вывод supervisor;
- `data/<contour>.log` — вывод каждого контура (poller/publisher/…);
- `run/supervisor.status.json` — текущие PID/перезапуски по контурам (без
  секретов), права `0600`.

## Проверка

Поведение доказано автоматическими тестами:

- `tests/test_supervisor.py` — одиночность, перезапуск, crash-loop fail-fast,
  чистое завершение по SIGTERM без осиротевших процессов (в т.ч. реальные
  подпроцессы);
- `tests/test_preflight.py` — все проверки конфигурации и прав;
- `tests/test_runtime_install.py` — рендер plist (без захардкоженного
  пользователя), транзакционный install/rollback/uninstall, сохранность
  данных, обязательность персистентного env-файла (отсутствующий, symlink,
  группо-/миро-читаемый и малформный файл падают до launchctl — включая
  guard в `deploy/aistat_runtime.sh`), а также миграция legacy
  `com.aistat.sync`: снятие до bootstrap'а нового задания, инжектированные
  сбои bootout/bootstrap/postflight с восстановлением ровно одного
  известно-рабочего рантайма, сохранность общих данных, uninstall обоих
  поколений и инвариант «оба поколения никогда не загружены одновременно»
  на полном жизненном цикле;
- `tests/test_runtime_e2e.py` — синтетический lifecycle submit → pull →
  зашифрованный store → ack/erase → collector → per-tenant publish → sync
  report, включая replace, revoke, restart и credential epoch.
