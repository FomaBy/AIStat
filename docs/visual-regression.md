## Visual regression tests

The visual suite uses the existing stdlib CDP harness and fresh temporary
databases, servers and browser profiles. It does not connect to Multica, use
production credentials, or load tenant data. The viewport is fixed at
1440×1000 with device scale 1.

### Why the harness freezes state

The rejected candidate started comparison after the first summary card was
filled. The dashboard's live EventSource could still change the status dot,
and the login page's `autofocus` field could still show a focus ring or caret.
Chart.js could also be drawing its initial animation. Those are transient test
states, not product changes.

Before capture, the harness waits for the page data condition, a loaded font
set, and two consecutive identical layout frames. It then freezes only test
rendering: CSS transitions and animations, the caret, focus, Chart.js
animations, and the browser color scheme used for native controls. Product
templates, CSS, screenshot tolerances, and masks are not changed.

### Browser pin

CI uses Chrome for Testing `151.0.7922.170`, not a floating `stable` channel;
the committed baselines are captured with that exact revision.
The fixture calls `Browser.getVersion` and fails with `Chrome version mismatch`
if the launched binary does not report the pinned build.

Local runs use the same expected version. Set `AISTAT_CHROME` to the absolute
Chrome/Chromium path when automatic discovery does not find it. To test a
different deliberately selected local build, set `AISTAT_CHROME_VERSION` to the
same version reported by that binary; CI remains pinned in the workflow.

### Run locally

Install the development dependencies, then run the unit harness checks and the
browser suite:

```sh
python3 -m pytest tests/visual/test_visual_regression.py -q
python3 -m pytest tests/visual/test_screenshots.py -q
```

For the required fresh process/profile evidence, run the browser command three
times. Each module fixture starts and removes a new Chrome profile:

```sh
python3 -m pytest tests/visual/test_screenshots.py -q
python3 -m pytest tests/visual/test_screenshots.py -q
python3 -m pytest tests/visual/test_screenshots.py -q
```

To record the decoded-pixel SHA-256 for each capture, add
`AISTAT_VISUAL_HASHES=1` and `-s`; the three runs must print the same hash for
each named page:

```sh
AISTAT_VISUAL_HASHES=1 \
  python3 -m pytest tests/visual/test_screenshots.py -q -s
```

On failure, changed pixels are highlighted red in a PNG diff. Keep the diff
outside the repository and inspect it before changing any baseline:

```sh
AISTAT_VISUAL_DIFF_DIR="/tmp/aistat-visual-diffs" \
  python3 -m pytest tests/visual/test_screenshots.py -q
```

The PR-only GitHub Actions job uploads this directory as the
`aistat-visual-diffs` artifact when the job fails.

### Negative CSS probe

The one-shot probe applies a 1 px shift inside the test browser only. It must
fail the exact comparison and create a readable diff; it must not be used for
normal runs:

```sh
AISTAT_VISUAL_CSS_PROBE=1 \
AISTAT_VISUAL_DIFF_DIR="/tmp/aistat-visual-negative" \
  python3 -m pytest tests/visual/test_screenshots.py -q
```

### Update a baseline deliberately

Only update after confirming that the rendered state is intentional and
contains no private or secret data. On the exact candidate SHA, run the suite
once with the explicit update switch, inspect all three images, then run it
again without the switch. The update switch is never set in CI:

```sh
AISTAT_UPDATE_VISUAL_BASELINES=1 \
  python3 -m pytest tests/visual/test_screenshots.py -q
python3 -m pytest tests/visual/test_screenshots.py -q
```

Commit only the reviewed `tests/visual/baselines/*.png` files. Never commit a
`*.diff.png` artifact or accept a changed baseline to hide a browser mismatch.

The ordinary `pytest` job remains available for pushes to `dev`; screenshot
coverage runs only in the pull-request job.
