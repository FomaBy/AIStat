# AIStat

Статистика использования токенов агентами Multica: сколько токенов, какими
моделями, по каким задачам и проектам, и с какой эффективностью
(токены ÷ story points).

Данные поступают **только** через аутентифицированный CLI `multica`
(subprocess, `--output json`) — без токенов/ключей в коде и без прямых
HTTP-вызовов к серверу. Хранилище — один файл SQLite.

Статус: **этап 1 из 4** — каркас, ингест-поллер, SQLite-схема, health.
Дальше: тарифы и стоимость (этап 2), API агрегатов + дашборд (этап 3),
сквозное QA (этап 4).

## Требования

- macOS / Linux, Python ≥ 3.9 (проверено на системном 3.9.6)
- Авторизованный CLI `multica` в `PATH`

## Установка

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Запуск

Поллер (фоновая синхронизация, цикл каждые 45 с по умолчанию):

```bash
.venv/bin/python -m aistat.poller            # бесконечный цикл
.venv/bin/python -m aistat.poller --once     # один цикл и выход (exit 1 при ошибках)
.venv/bin/python -m aistat.poller --once --detail-budget 200   # ускоренный бэкфилл деталей
```

Health-снапшот (счётчики таблиц, последний цикл, ошибки источников):

```bash
.venv/bin/python -m aistat.health
```

HTTP-сервер (пока только `/health`; дашборд появится на этапе 3):

```bash
.venv/bin/uvicorn aistat.server:app --port 8787
# → http://127.0.0.1:8787/health
```

Тесты:

```bash
.venv/bin/python -m pytest tests/ -q
```

## Конфигурация (переменные окружения)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `AISTAT_DB_PATH` | `./data/aistat.db` | путь к файлу SQLite |
| `AISTAT_POLL_INTERVAL_SECONDS` | `45` | пауза между циклами поллера |
| `AISTAT_USAGE_DAYS` | `90` | окно `runtime usage --days N` (макс. 365) |
| `AISTAT_DETAIL_BUDGET` | `40` | максимум issue, чьи детали (usage+runs) обновляются за цикл |
| `AISTAT_ISSUE_PAGE_LIMIT` | `100` | размер страницы `issue list` |
| `AISTAT_CLI_BIN` | `multica` | бинарь CLI |
| `AISTAT_CLI_TIMEOUT_SECONDS` | `120` | таймаут одного вызова CLI |

## Как работает ингест

Один цикл поллера опрашивает: `runtime list`, `agent list`, `project list`,
`runtime usage`/`runtime activity` по каждому рантайму, `issue list` по каждому
проекту (с пагинацией), `agent tasks` по каждому агенту, и — с бюджетом
`AISTAT_DETAIL_BUDGET` за цикл — `issue usage` + `issue runs` по задачам,
у которых изменился `updated_at` (свежие первыми). Так активные проекты
обновляются в каждом цикле, а большой легаси-архив (Jira Archive, 1024 задачи)
докачивается постепенно, не раздувая цикл.

Все записи — идемпотентные upsert'ы по натуральным ключам: повторные циклы
не создают дублей. Ошибка любого источника логируется, сохраняется в
`sync_state` и видна в health (`status: degraded`, `failing_sources`);
остальные источники цикла продолжают работать, нулевые значения вместо
ошибок не подставляются.

## Схема данных (SQLite)

- **Измерения**: `runtimes`, `agents` (модель, runtime), `projects`,
  `issues` (+ `story_points` из metadata с фолбэком на лейбл `SP:N`,
  `estimation_model`).
- **Факты**: `daily_usage` — токены по (runtime, model, date) — 4 вида
  токенов: input / output / cache read / cache write; `issue_usage` —
  суммарные токены и число задач по issue; `runs` — задачи Multica
  (атрибуция issue↔агент↔runtime, статусы, времена); `runtime_activity` —
  почасовой снапшот активности.
- **Служебные**: `sync_state` (здоровье источников), `poll_cycles` (журнал циклов).

Потокенной статистики по отдельным задачам у Multica нет (проверено);
атрибуция токенов агентам делается через (runtime_id, model, date) +
карту агент→(runtime_id, model) — это работа этапа 3. Агенты Codex Dev Sol
и QA Codex Sol делят одну модель и один runtime, поэтому их раздельные
значения будут помечаться как оценочные.

## Известные ограничения

- Дневная история usage начинается 2026-07-12 (workspace создан 2026-07-11) —
  более старых данных не существует на стороне Multica.
- `runtime activity` отдаёт срез «час → число задач» без даты; хранится как
  заменяемый снапшот.
- Удаление/архивация сущностей на сервере не отслеживается (только upsert).
