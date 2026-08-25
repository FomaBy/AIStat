# Runtime 3.11/3.12, proxy trust и security CI — план миграции и отката (FAN-3458)

## Что меняется

1. **Поддерживаемые runtime — Python 3.11 и 3.12.** Full pytest-suite
   проверяется CI-матрицей ровно на этих версиях (`Tests / pytest`).
   Python 3.6.8 больше не заявлен как целевой production-интерпретатор ни в
   config, ни в docs. Код stdlib-only контура (`legacy_wsgi`, `aistat.cgi`,
   `passenger_wsgi.py`) намеренно остаётся синтаксически совместимым со
   старыми интерпретаторами — это свойство кода, а не заявление о
   поддерживаемом runtime.
2. **Явный proxy trust.** Новый параметр `AISTAT_PROXY_TRUST_HOPS`
   (default `0`). При `0` приложение не доверяет ни одному прокси: любые
   `X-Forwarded-*` заголовки игнорируются (ProxyFix не подключается). За
   ровно N терминирующими прокси оператор ставит `AISTAT_PROXY_TRUST_HOPS=N`
   — тогда Werkzeug `ProxyFix` берёт ровно N крайних правых значений из
   каждого forwarded-заголовка, а всё, что клиент дописал слева,
   отбрасывается. Негативные тесты: `tests/test_proxy_trust.py`.
3. **Security CI** (`.github/workflows/security.yml`): pip-audit (SCA),
   CodeQL + bandit (static analysis), CycloneDX SBOM, полный
   full-history secret-scan gitleaks с опубликованием только
   санитизированного отчёта. Артефакты версионированы по commit SHA и
   падают в required workflow при подтверждённой finding.
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
   - добавить в `aistat-private/aistat.env` строку
     `AISTAT_PROXY_TRUST_HOPS=1` (ровно один терминирующий прокси
     LiteSpeed/Passenger впереди приложения). **Без этой строки и за
     прокси, редирект/HTTPS-редиректы будут видеть HTTP и внутренний
     адрес** — это видимое поведение, проверяемое `curl -I https://aistat.app/`.
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
  `gitleaks-redacted-<sha>` на каждом запуске.
