"""Subprocess wrapper around the authenticated `multica` CLI.

This is the ONLY data path into the app: no direct HTTP calls to the
Multica server and no tokens/keys in code. Failures raise CliError with
enough context for the health report; they are never swallowed or
replaced with empty data.
"""

import json
import subprocess
from typing import Any, List, Mapping, Optional, Sequence


class CliError(Exception):
    """A multica CLI invocation failed or returned unparseable output."""

    def __init__(self, args: Sequence[str], message: str):
        self.cli_args = list(args)
        super().__init__(f"multica {' '.join(args)}: {message}")


def run_cli(
    args: List[str],
    *,
    binary: str = "multica",
    timeout: int = 120,
    env: Optional[Mapping[str, str]] = None,
    prepend: Sequence[str] = (),
) -> Any:
    """Run `multica [prepend...] <args> --output json` and return parsed JSON.

    ``prepend`` carries pinned global flags that must precede the subcommand
    (``--profile``, ``--server-url``, ``--workspace-id``) — the per-connection
    worker uses it to bind every call to one isolated identity. ``env``, when
    given, fully replaces the child environment; the per-connection worker
    passes a scrubbed copy so an ambient ``MULTICA_TOKEN`` cannot leak in.
    Only ``args`` (never ``prepend``) is echoed in errors, so a profile name
    never lands in a health message.
    """
    cmd = [binary, *prepend, *args, "--output", "json"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env=None if env is None else dict(env),
        )
    except FileNotFoundError:
        raise CliError(args, f"binary not found: {binary}")
    except subprocess.TimeoutExpired:
        raise CliError(args, f"timed out after {timeout}s")

    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise CliError(args, f"exit code {proc.returncode}: {stderr[-500:]}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise CliError(args, f"invalid JSON output: {exc}")
