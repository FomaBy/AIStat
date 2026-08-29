"""Deployed release identity from the package build manifest (FAN-3569).

Reads only the fixed ``PACKAGE-MANIFEST.json`` written at the package root by
``scripts/build_cpanel_package.sh`` and returns the three fields that identify
the exact deployed candidate. Never exposes the manifest's file listing or any
path — those stay internal to this module.
"""

import hashlib
import json
from pathlib import Path

MANIFEST_NAME = "PACKAGE-MANIFEST.json"
_EXPECTED_FORMAT = "aistat-cpanel-package"
_EXPECTED_FORMAT_VERSION = 1
# Real manifests list a few dozen source files; this is a generous fail-closed
# bound against reading an arbitrarily large substituted file.
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_HEX_DIGITS = set("0123456789abcdef")


class ReleaseIdentityUnavailable(Exception):
    """The deployed manifest is missing, unreadable or fails validation."""


def _is_hex40(value) -> bool:
    return isinstance(value, str) and len(value) == 40 and set(value) <= _HEX_DIGITS


def load_release_identity(package_root) -> dict:
    """Return ``source_commit_sha``, ``source_tree_sha`` and ``manifest_sha256``.

    Raises ``ReleaseIdentityUnavailable`` for any missing, non-regular,
    out-of-boundary, oversized or malformed manifest. Callers must treat the
    exception message as internal only and respond with a generic error.
    """
    root = Path(package_root).resolve()
    manifest_path = root / MANIFEST_NAME
    if manifest_path.is_symlink():
        raise ReleaseIdentityUnavailable("manifest path is a symlink")
    try:
        resolved = manifest_path.resolve(strict=True)
    except OSError:
        raise ReleaseIdentityUnavailable("manifest is missing")
    if resolved.parent != root or not resolved.is_file():
        raise ReleaseIdentityUnavailable("manifest is outside the package root")
    try:
        raw = resolved.read_bytes()
    except OSError:
        raise ReleaseIdentityUnavailable("manifest could not be read")
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ReleaseIdentityUnavailable("manifest is oversized")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ReleaseIdentityUnavailable("manifest is not valid JSON")
    if not isinstance(manifest, dict):
        raise ReleaseIdentityUnavailable("manifest is not a JSON object")
    if manifest.get("format") != _EXPECTED_FORMAT:
        raise ReleaseIdentityUnavailable("manifest format is unrecognized")
    if manifest.get("format_version") != _EXPECTED_FORMAT_VERSION:
        raise ReleaseIdentityUnavailable("manifest format_version is unrecognized")
    commit_sha = manifest.get("source_commit_sha")
    tree_sha = manifest.get("source_tree_sha")
    if not _is_hex40(commit_sha) or not _is_hex40(tree_sha):
        raise ReleaseIdentityUnavailable("manifest commit/tree sha is invalid")
    return {
        "source_commit_sha": commit_sha,
        "source_tree_sha": tree_sha,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }
