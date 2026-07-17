"""Focused regression for the cPanel deploy log's `previous:` field (FAN-1208).

`deploy/cpanel_deploy.sh` prints a `PUBLISHED ... (previous: ...)` line on every
run. The `previous` value must be unambiguous and reproducible: exactly `none`
when there is no prior release, or exactly one release name otherwise — never a
basename glued to a full path (the old `${PREV:+...}${PREV:-none}` expansion
concatenated both when PREV was set).

The script exposes a pure `prev_label` helper; sourcing it with
AISTAT_DEPLOY_LIB_ONLY=1 loads the helpers without running a real deploy.
"""

import subprocess
from pathlib import Path

DEPLOY_SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "cpanel_deploy.sh"


def _prev_label(prev: str) -> str:
    """Return `prev_label "$prev"` as produced by the real deploy script."""
    result = subprocess.run(
        ["bash", "-c",
         'AISTAT_DEPLOY_LIB_ONLY=1 source "$1"; prev_label "$2"',
         "bash", str(DEPLOY_SCRIPT), prev],
        capture_output=True, text=True, check=True,
    )
    assert result.stderr == "", result.stderr
    return result.stdout.strip()


def test_previous_is_none_for_first_deploy():
    # First deploy: ~/aistat_app is not yet a symlink, so readlink yields "".
    assert _prev_label("") == "none"


def test_previous_is_single_release_name_for_repeat_deploy():
    # Repeat deploy: readlink returns the prior release's full path; the log
    # must show just that release's name, with no path/basename gluing.
    prev = "/home/user/aistat_releases/20260716-043245-b7150d6"
    label = _prev_label(prev)
    assert label == "20260716-043245-b7150d6"
    assert "/" not in label
    assert label != "none"
