# Операционный runbook AIStat (backup, восстановление, мониторинг, секреты)

Минимальный набор production-операций для AIStat: резервные копии данных,
проверяемое восстановление, наблюдаемость и гигиена секретов. Всё рассчитано на
доверенную локальную машину (owner-рантайм под `com.aistat.runtime`) и на
публичный cPanel-хост, где **нет SSH** — операции запускаются либо через launchd,
либо как cron one-shot.

## Владелец и периодичность

| Операция | Команда | Периодичность | Владелец |
|---|---|---|---|
| Резервная копия данных | `python -m aistat.backup create` | ежедневно | Сергей Фомин |
| Тест восстановления | `python -m aistat.backup self-test` | еженедельно + перед каждым релизом | Сергей Фомин |
| Проверка логов/артефактов на секреты | `scripts/scan_secrets.sh` | после каждого деплоя | Сергей Фомин |
| Очистка orphan-сайдкаров snapshot | `python -m aistat.backup clean --apply` | по мере необходимости | Сергей Фомин |
| Проверка здоровья и ошибок синхронизации | `GET /health` (см. ниже) | постоянно / при инциденте | Сергей Фомин |

Ретенция копий задаётся `AISTAT_BACKUP_RETENTION` (по умолчанию 14 поколений);
старые поколения удаляются автоматически при каждом `create`.

## Резервное копирование

`python -m aistat.backup create` делает **одно** согласованное, сжатое и
проверенное на целостность поколение всех долговременных SQLite-баз, которые
есть на машине:

- `data/aistat.db` — статистика владельца (основной пользовательский данные);
- `data/security.db` — учётные записи и watermark'и (когда включён «Connect your
  Multica»);
- `data/worker_connections.db` — **зашифрованный** store токенов (ключ живёт вне
  `data/`, в `~/.config/aistat/worker.key`, и в копию не попадает — без ключа
  копия бесполезна);
- каждый `*.db` из `data/tenants/`.

Каждая база копируется через SQLite backup API (коэрентно даже при активном WAL),
сжимается gzip, проверяется полным `PRAGMA integrity_check`, а `aistat.db`
дополнительно — на схему и обязательные таблицы. Поколение публикуется в каталог
атомарным `rename` только после того, как все проверки прошли: незавершённая
копия читателю не видна. Результат — каталог вида
`data/backups/aistat-<UTC>/` с файлами `*.db.gz` и `manifest.json` (sha256, размер,
версия схемы, счётчики строк по каждой таблице).

Каталог `data/backups/` лежит внутри `data/`, поэтому он **gitignored** и никогда
не попадает в репозиторий или в cPanel-пакет. Права — owner-only (`0700`/`0600`).

Пути и ретенция переопределяются переменными окружения `AISTAT_BACKUP_DIR`,
`AISTAT_BACKUP_RETENTION`, `AISTAT_DB_PATH`, `AISTAT_SECURITY_DB_PATH`,
`AISTAT_WORKER_STORE_PATH`, `AISTAT_TENANTS_DIR`.

### Расписание

Локальная машина (launchd, ежедневно в 03:15):

```xml
<!-- ~/Library/LaunchAgents/com.aistat.backup.plist -->
<key>ProgramArguments</key>
<array>
  <string>/bin/sh</string><string>-c</string>
  <string>cd "$HOME/Library/Application Support/AIStat/code" &&
          .venv/bin/python -m aistat.backup create</string>
</array>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>3</integer>
  <key>Minute</key><integer>15</integer></dict>
```

cPanel (Cron Jobs, ежедневно; без SSH — одноразовый запуск):

```
15 3 * * * cd $HOME/aistat && python -m aistat.backup create >> $HOME/aistat/data/backup.log 2>&1
```

## Восстановление и тест восстановления

Просмотр и проверка поколений:

```
python -m aistat.backup list            # поколения, новейшее сверху
python -m aistat.backup verify latest   # разжать и перепроверить каждую базу
```

Тест восстановления (**не трогает живые данные**): создаёт свежую копию,
разворачивает её во временный каталог, заново открывает восстановленные базы и
сверяет их с манифестом (sha256, целостность, схема, счётчики строк). Печатает
`PASS`/`FAIL` и возвращает код выхода 0/1 — годится для CI и cron-гейта:

```
python -m aistat.backup self-test
```

Боевое восстановление (перезаписывает живые базы, поэтому требует `--yes`;
`--dry-run` показывает план, ничего не меняя). Перед подменой каждый член
поколения разжимается, проверяется на целостность и сверяется по sha256; текущая
живая база **сохраняется рядом как `<имя>.pre-restore`**, и лишь затем атомарно
подменяется. Ошибка на любом шаге оставляет живые данные нетронутыми.

```
python -m aistat.backup restore latest --dry-run        # предпросмотр
python -m aistat.backup restore latest --yes            # восстановить всё
python -m aistat.backup restore latest --only aistat.db --yes  # одну базу
```

Restore никогда не доверяет путям внутри манифеста: цель выводится только из
текущей конфигурации, поэтому подделанный manifest не может записать данные вне
`data/`.

## Наблюдаемость: здоровье и ошибки синхронизации

Ошибки приложения и синхронизации диагностируются **без ручного чтения базы** —
через health-эндпоинт (`aistat/health.py`, `GET /health` и `GET /api/health`).
Он возвращает JSON:

- `status` — `ok` | `degraded` | `empty` (`degraded`, если хоть один источник
  синхронизации упал);
- `failing_sources[]` — упавшие источники с безопасным `last_error`,
  `last_error_at`, `last_success_at`;
- `last_cycle` — последний цикл поллинга: `sources_ok`, `sources_failed`,
  `notes` (сжатый список ошибок цикла);
- `last_beat` — «сердцебиение» синхронизации (последняя активность);
- `row_counts`, `daily_usage_span`, `issues_pending_details` — объём данных и
  фоновой очереди деталей;
- `pricing` — загруженность тарифов и модели без цены.

Быстрая диагностика:

```
curl -s http://127.0.0.1:8000/health | python -m json.tool | less
# смотреть status, failing_sources[].last_error, last_cycle.sources_failed
```

Все сообщения об ошибках проходят через фиксированный безопасный словарь
`aistat.handoff.safe_sync_error`, поэтому в `last_error` не попадают токены,
пути или произвольный текст исключений. Сырые логи контуров лежат в
`data/<контур>.log` (`poller.log`, `publisher.log`, `worker_sync.log`,
`collector.log`) — это дополнение к health, а не замена.

## Гигиена секретов и логов

Приложение спроектировано так, чтобы **никогда** не писать токен, пароль или
заголовок `Authorization` в лог: ошибки синхронизации/поллинга проходят через
конечный безопасный словарь `safe_sync_error`, а пользовательский PAT попадает в
официальный `multica` CLI только через stdin — не через argv, окружение, лог-строку
или текст исключения (`aistat/cli_profile.py`). Проверка, что так и осталось:

```
scripts/scan_secrets.sh            # логи + dist/ + все tracked-файлы
```

Скрипт ищет конкретные сигнатуры значений секретов (приватные ключи, Bearer/PAT,
ключи облаков, присваивания секрет-подобным ключам) в лог-файлах, в собранном
cPanel-пакете `dist/` и во всех отслеживаемых git-файлах. При находке печатает
`file:line` (без самого значения) и завершается с кодом 1; иначе — `OK`.

### Инвентарь чувствительных локальных артефактов (утверждённое хранилище)

Эти файлы **не** в git и покрыты `.gitignore` — они существуют только на машине
владельца (owner-only) и никогда не публикуются:

| Артефакт | Что это | Статус |
|---|---|---|
| `adminpanel.txt` | URL/креды cPanel | локально, gitignored; ротация — сменить пароль в cPanel и переписать файл |
| `aistat_app/` (`*.crt`, `*.ca-bundle`, `*.p7b`) | публичная TLS-цепочка хоста | локально, gitignored; ротация вместе с сертификатом хоста |
| `aistat_app.zip` | дубликат TLS-цепочки одним архивом | регенерируемый; можно удалить (`rm aistat_app.zip`), источник — `aistat_app/` |
| `dist/` | собранный cPanel-пакет | регенерируемый через `scripts/build_cpanel_package.sh`; gitignored |
| `data/` (`*.db`, `*.log`, `backups/`) | БД, логи, копии | локально, gitignored; секреты в БД токенов — только в зашифрованном виде |
| `~/.config/aistat/production.env` | боевые секреты рантайма | owner-only `0600`, вне репозитория и вне plist |
| `~/.config/aistat/worker.key` | ключ шифрования token-store | owner-only, вне `data/`, в backup не попадает |

Проверка, что ни один секрет не отслеживается git:

```
git ls-files | grep -iE 'adminpanel|\.crt$|\.ca-bundle$|\.p7b$|\.key$|\.pem$|\.env$|\.zip$'   # ожидается пусто
```

Временные snapshot-сайдкары (`data/.aistat-snapshot-*.db-{wal,shm}`), оставшиеся
после прерванного ingest, безопасно убираются:

```
python -m aistat.backup clean            # dry-run: показать
python -m aistat.backup clean --apply    # удалить (только orphan-сайдкары)
```

`clean` трогает только заведомо мусорные сайдкары, чей родительский temp-файл уже
исчез; реальные БД, `.env`, `adminpanel.txt`, TLS-бандл и ключи он не кандидатит.

## Чек-лист приёмки FAN-1185

- [x] Тест восстановления из свежего backup — `python -m aistat.backup self-test`
      (`PASS`, боевые данные не затронуты).
- [x] Логи и артефакты проверены на отсутствие секретов — `scripts/scan_secrets.sh`
      (`OK`), плюс архитектурные гарантии `safe_sync_error` / PAT-через-stdin.
- [x] Ошибки приложения и синхронизации диагностируются без ручного чтения базы —
      `GET /health` (`status`, `failing_sources[].last_error`, `last_cycle`).
- [x] Временные credential/TLS/package-файлы в утверждённом хранилище — все
      gitignored, ни один не отслеживается git; orphan-сайдкары убираются
      `aistat.backup clean`.
