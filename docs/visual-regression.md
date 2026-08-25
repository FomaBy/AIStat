## Visual regression tests

The visual suite uses the existing stdlib CDP harness and a fresh temporary
database. It does not connect to Multica, use production credentials, or load
tenant data. The browser viewport is fixed at 1440×1000 with device scale 1.

The committed baselines cover three stable states:

- `metrics.png` — the seeded dashboard with metrics and charts;
- `login.png` — the public sign-in page in English;
- `i18n-switch.png` — the same seeded dashboard after switching to Russian.

### Run locally

Install the development dependencies, then run:

```sh
python3 -m pytest tests/visual/test_screenshots.py -q
```

The test needs a Chrome or Chromium binary. Set `AISTAT_CHROME` to its
absolute path when it is not found automatically. A browser mismatch or a
missing binary is not a reason to accept a changed baseline.

### Investigate a mismatch

On failure, changed pixels are highlighted red in a PNG diff. Keep the diff
outside the repository and inspect it before changing any baseline:

```sh
AISTAT_VISUAL_DIFF_DIR="/tmp/aistat-visual-diffs" \
  python3 -m pytest tests/visual/test_screenshots.py -q
```

The PR-only GitHub Actions job stores this directory as the
`aistat-visual-diffs` artifact when the job fails.

### Update a baseline deliberately

Only update after confirming that the UI change is intentional and the
captured page contains no private or secret data. On the exact candidate SHA,
run the suite once with the explicit update switch, inspect all three images,
then run it again without the switch:

```sh
AISTAT_UPDATE_VISUAL_BASELINES=1 \
  python3 -m pytest tests/visual/test_screenshots.py -q
python3 -m pytest tests/visual/test_screenshots.py -q
```

Commit only the reviewed `tests/visual/baselines/*.png` files. Never commit a
`*.diff.png` artifact or use the update switch in CI.
