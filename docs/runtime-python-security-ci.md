# Runtime 3.11/3.12, proxy trust и security CI — план миграции и отката (FAN-3458)

## Что меняется

1. **Поддерживаемые runtime — Python 3.11 и 3.12.** Full pytest-suite
   проверяется CI-матрицей ровно на этих версиях (`Tests / pytest`).
   Python 3.6.8 больше не заявлен как целевой production-интерпретатор ни в
   config, ни в docs. Код stdlib-only контура (`legacy_wsgi`, `aistat.cgi`,
   `passenger_wsgi.py`) намеренно остаётся синтаксически совместимым со
   старыми интерпретаторами — это свойство кода, а не заявление о
   поддерживаемом runtime.
2. **Явный proxy trust.** Параметр `AISTAT_PROXY_TRUST_HOPS` (default `0`)
   одинаково действует в Flask и в stdlib-only контуре, который запускают
   `passenger_wsgi.py` и `aistat.cgi`. При `0` приложение не доверяет ни
   одному прокси: любые `X-Forwarded-*` заголовки игнорируются. За ровно N
   терминирующими прокси оператор ставит `AISTAT_PROXY_TRUST_HOPS=N`:
   Werkzeug `ProxyFix`, а legacy-контур для `X-Forwarded-Proto`, берут ровно
   N-е значение справа. Всё, что клиент дописал слева, отбрасывается; если
   значений меньше N, заголовок не доверяется. Негативные и позитивные
   проверки: `tests/test_proxy_trust.py`, `tests/test_legacy_wsgi.py` и
   `tests/test_cpanel_package.py`.
3. **Security CI** (`.github/workflows/security.yml`): pip-audit (SCA),
   CodeQL + bandit (static analysis), CycloneDX SBOM, полный
   full-history secret-scan gitleaks с опубликованием только
   санитизированного отчёта. `scripts/test_gitleaks_history.sh` в
   изолированной Git-истории проверяет чистую историю, документированный
   `.env.example` и current/deleted synthetic findings без вывода их значений.
   Все четыре артефакта (`pip-audit`, `bandit`, `sbom-cyclonedx`,
   `gitleaks-redacted`) привязаны к одному и тому же выражению —
   `${{ github.event.pull_request.head.sha || github.sha }}` — так что на PR
   имя несёт source head SHA кандидата, а не merge SHA; на push — `github.sha`.
   `artifact-sha-contract` job и `scripts/test_security_artifact_sha.sh`
   статически проверяют это выражение на каждом запуске и падают, если
   привязка к SHA отсутствует или расходится. Job падает при подтверждённой
   finding.
4. **Required browser lane** (`Tests / browser`): pinned Chrome
   131.0.6778.85 + `AISTAT_REQUIRE_BROWSER=1`; отсутствие/неработоспособность
   браузера — FAIL, а не skip (`tests/test_dashboard_browser.py`,
   `tests/test_auth_browser.py` содержат gate-тесты).

## План миграции (dev → следующий релиз)

1. Мерж настоящего candidate в `dev`; CI на `dev` прогоняет матрицу
   3.11/3.12, browser lane и все security-джобы.
2. На shared-хосте (при следующем deploy-окне):
   - выбрать в cPanel Python App интерпретатор 3.11/3.12 (или оставить
     текущий — контур остаётся importable, но это больше не
     поддерживаемая конфигурация);
   - добавить в Passenger Environment Variables (или в
     `aistat-private/aistat.env` для CGI) строку
     `AISTAT_PROXY_TRUST_HOPS=1` (ровно один терминирующий прокси
     LiteSpeed/Passenger впереди приложения). Это одинаково относится к
     Passenger и CGI: без этой строки они не используют forwarded headers, а
     цепочка короче одного доверенного hop также не считается HTTPS.
3. Migration на host: `python3 -m aistat.migrate` без изменений
   (schema/данные не меняются этой картой).

## План отката

- **Код:** откат — деплой предыдущего release-каталога штатным
  rollback-механизмом (манифест+sha256, описан в
  `docs/deployment-namecheap.md`); изменений схемы БД нет, откат не
  требует миграции данных.
- **Proxy trust:** вернуть/убрать `AISTAT_PROXY_TRUST_HOPS` в env —
  параметр читается на старте процесса, откат = перезапуск приложения.
- **CI:** workflows живут в репозитории; откат — revert коммита. Branch
  protection независимо настроен и не меняется этой картой.

## Evidence

- Полный suite локально и в CI на 3.11/3.12 (матрица `pytest`).
- `tests/test_proxy_trust.py`: негативные (spoofed forwarded headers при
  hops=0 не проходят host-gate, не удовлетворяют force_https, не выбирают
  throttle-ключ) и позитивные (hops=1 доверяет крайнему правому значению)
  проверки.
- Security artifacts: `pip-audit-<sha>`, `bandit-<sha>`, `sbom-cyclonedx-<sha>`,
  `gitleaks-redacted-<sha>` на каждом запуске; `<sha>` для PR — source SHA
  кандидата, для push — `github.sha`.
