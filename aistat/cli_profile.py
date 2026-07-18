"""Task-owned official Multica CLI profile for one user connection.

The trusted local worker collects each user's statistics with that user's own
manually entered Multica API token (PAT). To keep every connection isolated
from the owner's own CLI identity and from every other connection, one
``ConnectionCliProfile`` drives the *official* ``multica`` CLI under:

* a task-owned ``HOME`` (``config.cli_profiles_dir``), so no per-user token
  ever touches the owner's real ``~/.multica``;
* a deterministic ``--profile aistat-conn-<internal_user_id>`` derived only
  from the trusted internal numeric id (never from user input);
* a **scrubbed environment** with every ``MULTICA_*`` variable removed, so an
  ambient ``MULTICA_TOKEN`` / ``MULTICA_WORKSPACE_ID`` (the owner identity the
  runtime injects) cannot silently authenticate the call;
* the official host re-pinned on every invocation via ``--server-url``; the
  stored/user-supplied ``server_url`` is never trusted;
* an explicit ``--workspace-id`` chosen for this connection, so the owner's
  default workspace is never inherited.

The PAT is handed to the CLI only through its supported stdin prompt
(``multica login --token``); it never appears in argv, the environment, a URL,
a log line or an exception message. On exit the profile logs out and erases its
on-disk residue, so a revoked/replaced token leaves nothing behind.
"""

import logging
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .cli import CliError, run_cli
from .config import Config
from .tenant import canonical_tenant_id

logger = logging.getLogger("aistat.cli_profile")


class CliProfileError(RuntimeError):
    """A profile lifecycle step failed. Never carries a token or a path."""


def _assert_safe_component(path: Path) -> None:
    """Fail closed if an existing storage component is a symlink or a
    non-directory.

    An absent component is fine: it is created fresh under a parent that has
    already been validated. A symlink is rejected outright so a linked/foreign
    target can never receive the token or be deleted as residue, and there is
    no fallback to another location. ``os.lstat`` never follows the link and
    the raised error mentions neither the path nor any token.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise CliProfileError(
            "connection profile storage is unsafe: a symlink is not permitted"
        )
    if not stat.S_ISDIR(st.st_mode):
        raise CliProfileError(
            "connection profile storage is unsafe: not a directory"
        )


@dataclass
class ExecResult:
    returncode: int
    stdout: str
    stderr: str


def _profile_name(user_id: int) -> str:
    """Deterministic, path-safe profile name from a trusted internal id."""
    return "aistat-conn-{}".format(canonical_tenant_id(user_id))


def scrubbed_env(home: Path) -> Dict[str, str]:
    """A child environment with every MULTICA_* key removed and HOME pinned.

    Dropping the ambient identity is what makes an unauthenticated profile
    fail closed ("No server configured") instead of silently falling back to
    the owner's ``MULTICA_TOKEN``.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("MULTICA_")}
    env["HOME"] = str(home)
    return env


class _SubprocessExecutor:
    """Default executor: runs the real ``multica`` binary."""

    def __init__(self, binary: str, timeout: int):
        self.binary = binary
        self.timeout = timeout

    def raw(
        self,
        args: Sequence[str],
        *,
        prepend: Sequence[str],
        env: Dict[str, str],
        stdin: Optional[str] = None,
    ) -> ExecResult:
        cmd = [self.binary, *prepend, *args]
        try:
            proc = subprocess.run(
                cmd, input=stdin, capture_output=True, text=True,
                env=env, timeout=self.timeout,
            )
        except FileNotFoundError:
            return ExecResult(127, "", "multica binary not found")
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", "multica command timed out")
        return ExecResult(proc.returncode, proc.stdout or "", proc.stderr or "")

    def json(
        self,
        args: Sequence[str],
        *,
        prepend: Sequence[str],
        env: Dict[str, str],
    ) -> Any:
        return run_cli(
            list(args), binary=self.binary, timeout=self.timeout,
            env=env, prepend=list(prepend),
        )


def resolve_workspace(
    workspaces: List[Dict[str, Any]], label: Optional[str]
) -> Dict[str, Any]:
    """Pick exactly one workspace for a connection; never guess or inherit.

    ``label`` is the free text the user typed when connecting. A workspace is
    matched by exact id, slug or name (case-insensitive), or by a >=4-char id
    prefix (the same rule ``multica workspace switch`` accepts). With no label
    the only safe pick is a sole workspace; anything ambiguous or unmatched is
    an error, so the owner's default workspace can never stand in.
    """
    normalized = []
    for ws in workspaces:
        wid = str(ws.get("id") or "")
        if not wid:
            continue
        normalized.append(ws)
    if not normalized:
        raise CliProfileError("the connection's token has no accessible workspace")

    if not label:
        if len(normalized) == 1:
            return normalized[0]
        raise CliProfileError(
            "the connection's token has multiple workspaces but none was selected"
        )

    key = label.strip().lower()
    exact = [
        ws
        for ws in normalized
        if str(ws.get("id", "")).lower() == key
        or str(ws.get("slug", "")).lower() == key
        or str(ws.get("name", "")).lower() == key
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise CliProfileError("the connection's workspace label is ambiguous")

    if len(key) >= 4:
        prefix = [
            ws for ws in normalized if str(ws.get("id", "")).lower().startswith(key)
        ]
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise CliProfileError("the connection's workspace label is ambiguous")

    raise CliProfileError("the connection's workspace could not be resolved")


class ConnectionCliProfile:
    """One connection's isolated official-CLI identity, as a context manager."""

    def __init__(self, config: Config, user_id: int, *, executor=None):
        self.config = config
        self.user_id = canonical_tenant_id(user_id)
        self.profile = _profile_name(self.user_id)
        self.home = Path(config.cli_profiles_dir)
        self.official_url = config.multica_official_url
        self._executor = executor or _SubprocessExecutor(
            config.cli_bin, config.cli_timeout_seconds
        )
        self._workspace_id: Optional[str] = None

    # -- isolation primitives ------------------------------------------------

    def _env(self) -> Dict[str, str]:
        return scrubbed_env(self.home)

    def _base(self) -> List[str]:
        """Global flags pinned on every call: isolated profile + official host."""
        return ["--profile", self.profile, "--server-url", self.official_url]

    def _profile_dir(self) -> Path:
        return self.home / ".multica" / "profiles" / self.profile

    def _assert_safe_storage(self) -> None:
        """Reject a symlinked/non-directory storage chain before any token use.

        Validates the whole credential-bearing chain — the task-owned HOME
        (``AISTAT_CLI_PROFILES_DIR``), ``.multica``, ``profiles`` and this
        connection's own profile directory — so a linked or foreign target can
        never receive the PAT or be deleted as residue. Fails closed with no
        fallback to another location.
        """
        multica = self.home / ".multica"
        profiles = multica / "profiles"
        for component in (self.home, multica, profiles, self._profile_dir()):
            _assert_safe_component(component)

    def _prepare_home(self) -> None:
        # Fail closed before the PAT ever touches disk: a symlinked or
        # non-directory storage component is rejected with no fallback, so the
        # token can never be redirected to a linked/foreign target.
        self._assert_safe_storage()
        self.config.ensure_cli_profiles_dir()
        # A crashed prior cycle may have left a stale token file; start clean so
        # a revoked/replaced credential is never resurrected on restart. If the
        # residue cannot be removed, fail closed rather than reuse it.
        self._remove_residue()

    def _remove_residue(self) -> None:
        try:
            shutil.rmtree(self._profile_dir())
        except FileNotFoundError:
            return
        except OSError:
            # Never silent: a residue we cannot erase could resurrect a revoked
            # or replaced token, so fail closed and let the caller record a safe
            # per-connection failure. The message carries no path (``from None``
            # drops the OS error so its filename never surfaces in a trace).
            raise CliProfileError(
                "the connection profile residue could not be removed"
            ) from None

    def discard_residue(self) -> None:
        """Erase on-disk profile residue without login/logout or any network call.

        Used when a connection was revoked between listing and reading its
        token: a prior crashed cycle may have left a stale token config that
        must not survive revocation. Fails closed if the storage is unsafe or
        the residue cannot be removed, and never logs in or contacts the host.
        """
        self._assert_safe_storage()
        self._remove_residue()

    # -- lifecycle -----------------------------------------------------------

    def login(self, token: str) -> None:
        """Authenticate the profile with the user's PAT via the stdin prompt.

        The token is written to the CLI's stdin only; it is never placed in
        argv, the environment or any message this function raises or logs.
        """
        self._prepare_home()
        if not token:
            raise CliProfileError("no token available for the connection")
        result = self._executor.raw(
            ["login", "--token"], prepend=self._base(), env=self._env(),
            stdin=token if token.endswith("\n") else token + "\n",
        )
        if result.returncode != 0:
            # Deliberately generic: the CLI's stderr may echo request context;
            # the connection status must never carry a PAT or profile path.
            raise CliProfileError("official CLI login failed for the connection")

    def select_workspace(self, label: Optional[str]) -> Dict[str, Any]:
        """List the PAT's workspaces and pin exactly one for this connection."""
        try:
            data = self._executor.json(
                ["workspace", "list"], prepend=self._base(), env=self._env()
            )
        except CliError as exc:
            raise CliProfileError("could not list the connection's workspaces") from exc
        workspaces = data if isinstance(data, list) else (data.get("workspaces") or [])
        chosen = resolve_workspace(workspaces, label)
        self._workspace_id = str(chosen["id"])
        return chosen

    def runner(self, args: List[str]) -> Any:
        """A ``Poller``-compatible runner bound to this connection's identity.

        Every data call re-pins the official host and the explicitly selected
        workspace, so no ambient MULTICA_* value and no owner default can
        redirect the read.
        """
        if self._workspace_id is None:
            raise CliProfileError("workspace was not selected before polling")
        prepend = self._base() + ["--workspace-id", self._workspace_id]
        return self._executor.json(args, prepend=prepend, env=self._env())

    def logout(self) -> None:
        # Pin the isolated profile *and* the official host on logout too, so
        # every lifecycle invocation — not just data calls — is bound to the
        # deterministic per-user profile and the trusted host.
        result = self._executor.raw(
            ["auth", "logout"], prepend=self._base(), env=self._env(),
        )
        if result.returncode != 0:
            logger.warning("auth logout returned non-zero for user %s", self.user_id)

    def cleanup(self) -> None:
        """Log out and erase the profile's on-disk residue (token included).

        A logout failure must still trigger local residue removal, so the
        removal runs in a ``finally``. A residue that cannot be erased is never
        silent: ``_remove_residue`` raises so the caller records a safe
        per-connection failure instead of treating the credential as gone.
        """
        try:
            self.logout()
        finally:
            self._remove_residue()

    def __enter__(self) -> "ConnectionCliProfile":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.cleanup()
