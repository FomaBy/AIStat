# Локальные развёртки `dev` и `main` (macOS)

Две постоянные локальные развёртки AIStat, работающие бок о бок на разных
портах, каждая — своя ветка git:

| Развёртка | Ветка | Порт | URL | Обновление |
|---|---|---|---|---|
| `dev`  | `origin/dev`  | `8788` | http://127.0.0.1:8788 | автоматически подхватывает свежие пуши в `dev` |
| `main` | `origin/main` | `8789` | http://127.0.0.1:8789 | только по запросу — командой `release` |

Порт `8787` остаётся свободным для ручного `./run.sh` из рабочей копии.

Всё делает один скрипт — [`deploy/local_deploy.sh`](../deploy/local_deploy.sh).

## Как это устроено

- Каждая развёртка — **отдельный клон** репозитория под
  `~/Library/Application Support/AIStat/local/{dev,main}`. Рабочая копия
  оператора в `~/Documents/AIStat` не трогается: агенты коммитят там, а
  развёртки живут отдельно. Клоны намеренно лежат **вне** защищённого macOS
  каталога `~/Documents`, иначе launchd не смог бы их читать (та же причина, что
  в `scripts/install_launchd_sync.sh`).
- Каждая развёртка запускает штатный `./run.sh` (поллер Multica + API +
  дашборд) со своим `AISTAT_PORT` и своей базой SQLite (`data/aistat.db` внутри
  своего клона) — ветки не делят состояние.
- Управляется тремя агентами launchd (домен `gui/<uid>`):
  - `com.aistat.local.dev` — сервер ветки `dev` (`KeepAlive` — всегда поднят,
    переживает перезагрузку и падения);
  - `com.aistat.local.main` — сервер ветки `main` (тоже `KeepAlive`);
  - `com.aistat.local.dev-update` — таймер (`StartInterval`, по умолчанию 120 с),
    делает `git fetch` + `reset --hard origin/dev` и, **только если HEAD
    сдвинулся**, перезапускает `dev`-сервер. Лишних перезапусков нет.

Уже установленный агент `com.aistat.sync` (публикация на хостинг) не
затрагивается — префикс лейблов разный (`com.aistat.local.*`).

## Установка

Из рабочей копии репозитория:

```bash
deploy/local_deploy.sh install
```

Скрипт клонирует обе ветки, создаёт venv и ставит зависимости (заранее, в
foreground — первый старт launchd мгновенный), генерирует и загружает три
launchd-агента. Команда идемпотентна — повторный `install` просто обновляет
клоны и перезагружает агенты.

Требования: авторизованный CLI `multica` в `PATH` (нужен поллеру каждой
развёртки), `python3`, `git`. Ветка `dev` должна существовать на `origin`.

## Обновление `dev`

Ничего делать не нужно — таймер `com.aistat.local.dev-update` каждые
`AISTAT_DEV_UPDATE_INTERVAL_SECONDS` (по умолчанию 120 с) подтягивает
`origin/dev` и перезапускает дашборд при изменениях. Подтянуть немедленно:

```bash
deploy/local_deploy.sh sync dev            # fetch + reset origin/dev, рестарт при изменениях
deploy/local_deploy.sh sync dev --force    # рестарт даже без изменений
deploy/local_deploy.sh sync dev --ref <sha> # зафиксировать конкретный коммит
```

## Релиз в `main` (по запросу, со сборкой)

Одна команда: продвигает `dev` → `origin/main`, **собирает и проверяет** пакет
и только потом перезапускает `main`-дашборд на собранную версию. Если сборка или
проверка синтаксиса падают — `main`-развёртка остаётся на предыдущем релизе
(та же логика безопасности, что в `deploy/cpanel_deploy.sh`).

```bash
deploy/local_deploy.sh release                 # origin/dev → origin/main + build + рестарт main
deploy/local_deploy.sh release --from <ref>    # выпустить конкретную ветку/коммит вместо dev
deploy/local_deploy.sh release --no-promote    # без пуша: только пересобрать и перевыкатить текущий origin/main
deploy/local_deploy.sh release --force         # разрешить не-fast-forward пуш (--force-with-lease)
```

По умолчанию релиз запрещает не-fast-forward: если `origin/main` не является
предком `--from`, команда останавливается (защита от случайной перезаписи
истории). Сборка использует `scripts/build_cpanel_package.sh` и складывает
пакет в `dist/aistat-cpanel` внутри клона `main` (в git не попадает).

## Статус, логи, управление

```bash
deploy/local_deploy.sh status                  # launchd-состояние, HEAD sha и health по портам
deploy/local_deploy.sh restart dev|main        # перезапустить сервер
deploy/local_deploy.sh stop dev|main           # остановить (агент остаётся установлен)
deploy/local_deploy.sh start dev|main          # запустить снова
```

Логи:

- `~/Library/Application Support/AIStat/local/logs/{dev,main}.out.log` / `.err.log`
- `.../logs/dev-update.out.log` / `.err.log`
- поллер каждой развёртки — `.../local/{dev,main}/data/poller.log`

## Удаление

```bash
deploy/local_deploy.sh uninstall           # выгрузить агенты, клоны и данные оставить
deploy/local_deploy.sh uninstall --purge   # выгрузить агенты и удалить весь local-каталог
```

## Настройка (env)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `AISTAT_LOCAL_ROOT` | `~/Library/Application Support/AIStat/local` | корень клонов + логов |
| `AISTAT_REPO_URL` | origin рабочей копии | откуда клонировать развёртки |
| `AISTAT_DEV_PORT` | `8788` | порт развёртки `dev` |
| `AISTAT_MAIN_PORT` | `8789` | порт развёртки `main` |
| `AISTAT_LOCAL_POLL_INTERVAL_SECONDS` | `180` | интервал поллера внутри каждой развёртки |
| `AISTAT_DEV_UPDATE_INTERVAL_SECONDS` | `120` | как часто таймер проверяет `origin/dev` |
| `AISTAT_CLI_BIN` | найденный `multica` | бинарь CLI для поллеров |

Значения задаются **при `install`** (запекаются в plist'ы). Чтобы поменять порт
или интервал — измените env и выполните `install` заново.

## Про нагрузку

Каждая развёртка держит свой поллер, который каждые
`AISTAT_LOCAL_POLL_INTERVAL_SECONDS` (по умолчанию 180 с) обращается к CLI
`multica`. То есть после установки на машине работают до трёх поллеров: ручной
`./run.sh` (если запущен) плюс `dev` и `main`. Токены LLM они не тратят (только
data-эндпоинты CLI), но если нужно снизить нагрузку — увеличьте интервал и
переустановите, либо временно остановите ненужную развёртку (`stop`).
