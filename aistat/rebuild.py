"""Deterministic Multica recovery replay on isolated captured inputs.

Capture records only CLI JSON and the pinned pricing table.  Replay never calls
Multica and writes a fresh SQLite database plus immutable input/output manifests.
All paths are caller-provided recovery copies; this module has no live-database,
publisher, deployment, or Git-ref operation.
"""

import argparse
import copy
from datetime import datetime
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import string
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

from . import backup, normalize
from .cli import run_cli
from .config import Config
from .db import connect, init_db
from .poller import Poller
from .snapshot import SnapshotError, validate_snapshot
from .snapshot_recovery import file_sha256, swap_staged_into_place

INPUT_MANIFEST = "input-manifest.json"
OUTPUT_MANIFEST = "output-manifest.json"
DATABASE_NAME = "aistat.db"
PRICING_NAME = "pricing.json"
PRICING_OVERRIDES_NAME = "pricing-overrides.json"
RESPONSES_DIR = "responses"
FORMAT = "aistat-multica-rebuild"
FORMAT_VERSION = 1
MAX_USAGE_DAYS = 365
ALL_DETAILS = 1000000000
COUNTERS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class RebuildError(ValueError):
    """Captured recovery input or its isolated replay is unsafe or inconsistent."""


def _canonical(value: Any) -> bytes:
    try:
        return (
            json.dumps(
                value, allow_nan=False, ensure_ascii=True, separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RebuildError("input is not canonical JSON") from exc


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path, *, require_canonical: bool = True) -> Any:
    def reject_constant(value: str):
        raise ValueError("non-finite number: %s" % value)

    def reject_duplicate_pairs(pairs: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key: %s" % key)
            result[key] = value
        return result

    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise RebuildError("cannot read canonical JSON: %s" % path.name) from exc
    if require_canonical and raw != _canonical(value):
        raise RebuildError("non-canonical JSON: %s" % path.name)
    return value


def _regular_file(path: Path, description: str) -> None:
    try:
        os.lstat(str(path))
    except OSError as exc:
        raise RebuildError("cannot inspect %s" % description) from exc
    if not os.path.isfile(str(path)) or os.path.islink(str(path)):
        raise RebuildError("%s must be a regular non-symlink file" % description)


def _owner_only(path: Path) -> None:
    try:
        os.chmod(str(path), 0o600)
    except OSError as exc:
        raise RebuildError("cannot make recovery artifact owner-only") from exc


def _write_private(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    _owner_only(path)


def _freeze_json(source: Path, destination: Path, description: str) -> Dict[str, Any]:
    _regular_file(source, description)
    payload = _canonical(_read_json(source, require_canonical=False))
    _write_private(destination, payload)
    return {
        "path": destination.name,
        "sha256": _sha256_bytes(payload),
        "size_bytes": len(payload),
    }


def _create_private(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except OSError as exc:
        raise RebuildError("cannot create private recovery database") from exc


def _prepare_output(path: Path) -> Path:
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise RebuildError("output path already exists: %s" % path)
    parent = path.parent
    if not parent.is_dir() or parent.is_symlink():
        raise RebuildError("output parent must be an existing regular directory")
    return Path(tempfile.mkdtemp(prefix=".aistat-rebuild-", dir=str(parent)))


def _discard(path: Path) -> None:
    if path.exists() and not path.is_symlink():
        shutil.rmtree(str(path))


def _publish(staging: Path, output: Path) -> None:
    os.replace(str(staging), str(output))


def _source(source: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(source, dict) or set(source) != {
        "base_sha", "base_ref", "base_tree", "captured_at"
    }:
        raise RebuildError("source must record base_sha, base_ref, base_tree, captured_at")
    checked = {key: source[key] for key in source}
    if not all(isinstance(value, str) and value for value in checked.values()):
        raise RebuildError("source provenance must contain non-empty strings")
    if (
        len(checked["base_sha"]) != 40
        or len(checked["base_tree"]) != 40
        or not all(value in string.hexdigits for value in checked["base_sha"])
        or not all(value in string.hexdigits for value in checked["base_tree"])
    ):
        raise RebuildError("source SHA/tree must be exact 40-character values")
    if not UTC_TIMESTAMP_RE.match(checked["captured_at"]):
        raise RebuildError("source captured_at must be an exact UTC timestamp")
    try:
        datetime.strptime(checked["captured_at"], "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise RebuildError("source captured_at must be an exact UTC timestamp") from exc
    return checked  # type: ignore[return-value]


def _poller_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Keep every config value that affects frozen commands or calculated cost."""
    if not isinstance(settings, dict) or set(settings) != {
        "credits_per_usd", "issue_page_limit"
    }:
        raise RebuildError("input must record deterministic poller settings")
    page_limit = settings["issue_page_limit"]
    credits_per_usd = settings["credits_per_usd"]
    if (
        isinstance(page_limit, bool)
        or not isinstance(page_limit, int)
        or page_limit < 1
        or isinstance(credits_per_usd, bool)
        or not isinstance(credits_per_usd, (int, float))
        or not math.isfinite(float(credits_per_usd))
    ):
        raise RebuildError("invalid deterministic poller settings")
    return {
        "credits_per_usd": float(credits_per_usd),
        "issue_page_limit": page_limit,
    }


class _RecordingRunner:
    def __init__(self, root: Path, runner: Callable[[List[str]], Any]):
        self.root = root
        self.runner = runner
        self.entries: List[Dict[str, Any]] = []

    def __call__(self, args: List[str]) -> Any:
        value = self.runner(list(args))
        payload = _canonical(value)
        relative = "%s/%06d.json" % (RESPONSES_DIR, len(self.entries))
        path = self.root / relative
        path.parent.mkdir(exist_ok=True)
        try:
            os.chmod(str(path.parent), 0o700)
        except OSError as exc:
            raise RebuildError("cannot make response directory owner-only") from exc
        _write_private(path, payload)
        self.entries.append(
            {
                "command": list(args),
                "path": relative,
                "sha256": _sha256_bytes(payload),
                "size_bytes": len(payload),
            }
        )
        return value


def capture_inputs(
    output_dir: Path,
    *,
    config: Config,
    source: Dict[str, Any],
    runner: Callable[[List[str]], Any] = None,
) -> Dict[str, Any]:
    """Freeze one complete available-range CLI pass into ``output_dir``.

    The production runner is used only when a caller does not inject one.  It
    reads Multica but never opens an existing AIStat database: Poller runs in a
    disposable staging database to discover and record every required response.
    """
    source = _source(source)
    output_dir = Path(output_dir)
    staging = _prepare_output(output_dir)
    try:
        pricing_path = staging / PRICING_NAME
        pricing = {
            "base": _freeze_json(
                Path(config.pricing_path), pricing_path, "pricing input"
            ),
            "overrides": None,
        }
        override_path = config.pricing_overrides_path
        if override_path is not None:
            pricing["overrides"] = _freeze_json(
                Path(override_path),
                staging / PRICING_OVERRIDES_NAME,
                "pricing override input",
            )

        capture_config = copy.copy(config)
        capture_config.db_path = staging / ".capture.db"
        capture_config.usage_days = MAX_USAGE_DAYS
        capture_config.pricing_path = pricing_path
        capture_config.pricing_overrides_path = (
            staging / PRICING_OVERRIDES_NAME if pricing["overrides"] else None
        )
        poller_settings = _poller_settings(
            {
                "credits_per_usd": capture_config.credits_per_usd,
                "issue_page_limit": capture_config.issue_page_limit,
            }
        )
        conn = connect(capture_config.db_path)
        try:
            init_db(conn)
            if runner is None:
                runner = lambda args: run_cli(
                    args,
                    binary=capture_config.cli_bin,
                    timeout=capture_config.cli_timeout_seconds,
                    env=capture_config.poller_cli_env(),
                )
            recorder = _RecordingRunner(staging, runner)
            poller = Poller(
                capture_config,
                conn,
                runner=recorder,
                now=lambda: source["captured_at"],
            )
            result = poller.run_cycle(detail_budget=ALL_DETAILS)
            if not result.ok:
                raise RebuildError("capture failed; no partial input was published")
        finally:
            conn.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(capture_config.db_path) + suffix).unlink()
            except FileNotFoundError:
                pass

        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "poller": poller_settings,
            "pricing": pricing,
            "responses": recorder.entries,
            "source": source,
            "usage_days": MAX_USAGE_DAYS,
        }
        _write_private(staging / INPUT_MANIFEST, _canonical(manifest))
        frozen = _validate_input(staging)
        _expected_daily_usage(frozen)
        _publish(staging, output_dir)
        return manifest
    except BaseException:
        _discard(staging)
        raise


class _FrozenInput:
    def __init__(self, root: Path, manifest: Dict[str, Any], responses: List[Any]):
        self.root = root
        self.manifest = manifest
        self.responses = responses
        self.index = 0

    def runner(self, args: List[str]) -> Any:
        if self.index >= len(self.manifest["responses"]):
            raise RebuildError("replay requested an unrecorded CLI command")
        entry = self.manifest["responses"][self.index]
        if entry["command"] != list(args):
            raise RebuildError("replay CLI command differs from frozen input")
        value = self.responses[self.index]
        self.index += 1
        return value

    def require_consumed(self) -> None:
        if self.index != len(self.responses):
            raise RebuildError("frozen input contains unused CLI response")


def _validate_input(root: Path) -> _FrozenInput:
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise RebuildError("input must be a regular directory")
    manifest_path = root / INPUT_MANIFEST
    _regular_file(manifest_path, "input manifest")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or set(manifest) != {
        "format", "format_version", "poller", "pricing", "responses", "source",
        "usage_days"
    }:
        raise RebuildError("unexpected input manifest shape")
    if manifest["format"] != FORMAT or manifest["format_version"] != FORMAT_VERSION:
        raise RebuildError("unsupported input manifest format")
    source = _source(manifest["source"])
    poller_settings = _poller_settings(manifest["poller"])
    if manifest["usage_days"] != MAX_USAGE_DAYS:
        raise RebuildError("input does not cover the full available usage range")
    pricing = manifest["pricing"]
    if not isinstance(pricing, dict) or set(pricing) != {"base", "overrides"}:
        raise RebuildError("invalid pricing manifest")
    pricing_paths = set()
    for label, expected_name in (
        ("base", PRICING_NAME), ("overrides", PRICING_OVERRIDES_NAME)
    ):
        item = pricing[label]
        if item is None:
            if label == "base":
                raise RebuildError("pricing manifest has no base file")
            continue
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size_bytes"}:
            raise RebuildError("invalid pricing manifest file")
        if item["path"] != expected_name:
            raise RebuildError("unexpected pricing path")
        pricing_path = root / expected_name
        _regular_file(pricing_path, "pricing %s input" % label)
        pricing_bytes = pricing_path.read_bytes()
        if (
            _sha256_bytes(pricing_bytes) != item["sha256"]
            or len(pricing_bytes) != item["size_bytes"]
        ):
            raise RebuildError("pricing input hash mismatch")
        _read_json(pricing_path)
        pricing_paths.add(expected_name)

    entries = manifest["responses"]
    if not isinstance(entries, list) or not entries:
        raise RebuildError("input must contain captured CLI responses")
    expected_paths = {INPUT_MANIFEST} | pricing_paths
    values = []
    for index, entry in enumerate(entries):
        expected_path = "%s/%06d.json" % (RESPONSES_DIR, index)
        if not isinstance(entry, dict) or set(entry) != {
            "command", "path", "sha256", "size_bytes"
        }:
            raise RebuildError("invalid response manifest entry")
        if entry["path"] != expected_path:
            raise RebuildError("response path is not canonical")
        if (
            not isinstance(entry["command"], list)
            or not all(isinstance(item, str) and item for item in entry["command"])
            or not isinstance(entry["sha256"], str)
            or not isinstance(entry["size_bytes"], int)
        ):
            raise RebuildError("invalid response provenance")
        expected_paths.add(expected_path)
        path = root / expected_path
        _regular_file(path, "captured CLI response")
        raw = path.read_bytes()
        if _sha256_bytes(raw) != entry["sha256"] or len(raw) != entry["size_bytes"]:
            raise RebuildError("captured CLI response hash mismatch")
        values.append(_read_json(path))

    files = {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    directories = {
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_dir()
    }
    if files != expected_paths or directories != {RESPONSES_DIR}:
        raise RebuildError("input contains unknown or missing files")
    manifest["source"] = source
    manifest["poller"] = poller_settings
    return _FrozenInput(root, manifest, values)


def _expected_daily_usage(frozen: _FrozenInput) -> Dict[Tuple[str, str, str], Tuple[int, int, int, int]]:
    expected: Dict[Tuple[str, str, str], Tuple[int, int, int, int]] = {}
    for entry, value in zip(frozen.manifest["responses"], frozen.responses):
        command = entry["command"]
        if command[:2] != ["runtime", "usage"]:
            continue
        if len(command) != 5 or command[3:] != ["--days", str(MAX_USAGE_DAYS)]:
            raise RebuildError("usage command does not pin the full available range")
        if not isinstance(value, list):
            raise RebuildError("runtime usage response must be a list")
        for item in value:
            if not isinstance(item, dict):
                raise RebuildError("runtime usage row must be an object")
            for name in COUNTERS:
                raw = item.get(name)
                if isinstance(raw, float) and not raw.is_integer():
                    raise RebuildError("daily usage counter must be integral")
            row = normalize.normalize_daily_usage(item)
            if row["runtime_id"] != command[2]:
                raise RebuildError("usage row runtime differs from its source command")
            key = (row["runtime_id"], row["model"], row["date"])
            counters = tuple(int(row[name]) for name in COUNTERS)
            if (
                not all(isinstance(value, str) and value for value in key)
                or any(value < 0 for value in counters)
                or key in expected
            ):
                raise RebuildError("duplicate or invalid daily usage counter")
            expected[key] = counters
    if not expected:
        raise RebuildError("captured input has no runtime usage rows")
    return expected


def _daily_report(conn: sqlite3.Connection) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT runtime_id, model, date, input_tokens, output_tokens, "
        "cache_read_tokens, cache_write_tokens FROM daily_usage "
        "ORDER BY runtime_id, model, date"
    ).fetchall()
    usage = [
        {
            "runtime_id": row["runtime_id"],
            "model": row["model"],
            "date": row["date"],
            **{name: int(row[name]) for name in COUNTERS},
        }
        for row in rows
    ]
    dates = [row["date"] for row in usage]
    totals = {name: sum(row[name] for row in usage) for name in COUNTERS}
    return {
        "range": {"first": min(dates), "last": max(dates)},
        "row_count": len(usage),
        "rows": usage,
        "totals": totals,
        "watermark": max(dates),
    }


def _verify_daily_usage(
    report: Dict[str, Any], expected: Dict[Tuple[str, str, str], Tuple[int, int, int, int]]
) -> None:
    actual = {
        (row["runtime_id"], row["model"], row["date"]): tuple(row[name] for name in COUNTERS)
        for row in report["rows"]
    }
    if len(actual) != len(report["rows"]) or actual != expected:
        raise RebuildError("daily usage counters differ from captured Multica rows")


def _quote_identifier(value: str) -> str:
    return '"%s"' % value.replace('"', '""')


def _output_manifest(
    frozen: _FrozenInput, conn: sqlite3.Connection, database: Path
) -> Dict[str, Any]:
    expected = _expected_daily_usage(frozen)
    report = _daily_report(conn)
    _verify_daily_usage(report, expected)
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RebuildError("rebuilt database failed foreign-key check")
    try:
        snapshot = validate_snapshot(database)
    except SnapshotError as exc:
        raise RebuildError("rebuilt database failed validation") from exc
    report["rows_sha256"] = _sha256_bytes(_canonical(report.pop("rows")))
    table_counts = {
        row[0]: int(
            conn.execute("SELECT COUNT(*) FROM %s" % _quote_identifier(row[0]))
            .fetchone()[0]
        )
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    }
    manifest_bytes = (frozen.root / INPUT_MANIFEST).read_bytes()
    return {
        "database_sha256": _sha256_bytes(database.read_bytes()),
        "database_size_bytes": database.stat().st_size,
        "daily_usage": report,
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "input_manifest_sha256": _sha256_bytes(manifest_bytes),
        "schema_version": snapshot.schema_version,
        "source": frozen.manifest["source"],
        "table_counts": table_counts,
    }


def _make_standalone(conn: sqlite3.Connection) -> None:
    """Fold WAL state into the immutable output before its bytes are hashed."""
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    mode = conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]
    if str(mode).lower() != "delete":
        raise RebuildError("rebuilt database could not leave WAL mode")
    conn.commit()


def _remove_rebuild_sidecars(database: Path) -> None:
    """A closed DELETE-journal database may retain only a harmless stale shm file."""
    wal = Path(str(database) + "-wal")
    if wal.exists():
        raise RebuildError("rebuilt database left a WAL sidecar")
    shm = Path(str(database) + "-shm")
    try:
        shm.unlink()
    except FileNotFoundError:
        pass


def rebuild(input_dir: Path, output_dir: Path) -> Dict[str, Any]:
    """Replay frozen Multica input into a new, atomically published copy."""
    frozen = _validate_input(Path(input_dir))
    _expected_daily_usage(frozen)
    poller_settings = frozen.manifest["poller"]
    output_dir = Path(output_dir)
    staging = _prepare_output(output_dir)
    database = staging / DATABASE_NAME
    try:
        config = Config(
            db_path=database,
            credits_per_usd=poller_settings["credits_per_usd"],
            issue_page_limit=poller_settings["issue_page_limit"],
            usage_days=MAX_USAGE_DAYS,
            pricing_path=frozen.root / PRICING_NAME,
            pricing_overrides_path=(
                frozen.root / PRICING_OVERRIDES_NAME
                if frozen.manifest["pricing"]["overrides"] else None
            ),
        )
        _create_private(database)
        conn = connect(database)
        try:
            init_db(conn)
            result = Poller(
                config,
                conn,
                runner=frozen.runner,
                now=lambda: frozen.manifest["source"]["captured_at"],
            ).run_cycle(detail_budget=ALL_DETAILS)
            if not result.ok:
                raise RebuildError("replay failed; no output was published")
            frozen.require_consumed()
            _make_standalone(conn)
            manifest = _output_manifest(frozen, conn, database)
        finally:
            conn.close()
        _remove_rebuild_sidecars(database)
        _write_private(staging / OUTPUT_MANIFEST, _canonical(manifest))
        _publish(staging, output_dir)
        return manifest
    except BaseException:
        _discard(staging)
        raise


def verify_rebuild(input_dir: Path, output_dir: Path) -> Dict[str, Any]:
    """Fail closed unless an existing isolated output still matches its input."""
    frozen = _validate_input(Path(input_dir))
    output_dir = Path(output_dir)
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise RebuildError("output must be a regular directory")
    expected_files = {DATABASE_NAME, OUTPUT_MANIFEST}
    files = {
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if files != expected_files or any(path.is_dir() for path in output_dir.rglob("*")):
        raise RebuildError("output contains unknown or missing files")
    database = output_dir / DATABASE_NAME
    manifest_path = output_dir / OUTPUT_MANIFEST
    _regular_file(database, "rebuilt database")
    _regular_file(manifest_path, "output manifest")
    manifest = _read_json(manifest_path)
    conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        actual = _output_manifest(frozen, conn, database)
    finally:
        conn.close()
    if manifest != actual:
        raise RebuildError("output manifest differs from rebuilt database")
    return actual


def _copy_to_stage(source: Path, target: Path) -> Path:
    handle, name = tempfile.mkstemp(
        prefix=".aistat-rebuild-stage-", suffix=".db", dir=str(target.parent)
    )
    os.close(handle)
    staged = Path(name)
    try:
        shutil.copyfile(str(source), str(staged))
        if file_sha256(staged) != file_sha256(source):
            raise RebuildError("staged copy hash mismatch")
        return staged
    except BaseException:
        try:
            staged.unlink()
        except FileNotFoundError:
            pass
        raise


def rebuild_self_test(input_dir: Path, recovery_root: Path) -> Dict[str, Any]:
    """Prove replay, backup restore, atomic copy cutover and rollback in a scratch root."""
    recovery_root = Path(recovery_root)
    if recovery_root.exists() or recovery_root.is_symlink():
        raise RebuildError("recovery root must not already exist")
    if not recovery_root.parent.is_dir() or recovery_root.parent.is_symlink():
        raise RebuildError("recovery root parent must be an existing regular directory")
    recovery_root.mkdir(mode=0o700)
    first = recovery_root / "first"
    second = recovery_root / "second"
    target = recovery_root / "target.db"
    try:
        first_manifest = rebuild(input_dir, first)
        second_manifest = rebuild(input_dir, second)
        if first_manifest != second_manifest:
            raise RebuildError("two frozen-input rebuilds produced different output")
        source_database = first / DATABASE_NAME
        shutil.copyfile(str(source_database), str(target))
        conn = sqlite3.connect(str(target))
        try:
            conn.execute("UPDATE sync_beats SET phase = 'pre-cutover'")
            conn.commit()
        finally:
            conn.close()
        original_sha = file_sha256(target)
        candidate = second / DATABASE_NAME
        staged = _copy_to_stage(candidate, target)
        swap_staged_into_place(str(staged), str(target))
        candidate_sha = file_sha256(candidate)
        if file_sha256(target) != candidate_sha:
            raise RebuildError("atomic cutover did not install the candidate copy")

        backup_config = Config(
            db_path=target,
            security_db_path=recovery_root / "missing-security.db",
            worker_store_path=recovery_root / "missing-worker.db",
            tenants_dir=recovery_root / "missing-tenants",
            worker_tenants_dir=recovery_root / "missing-worker-tenants",
            backup_dir=recovery_root / "backups",
        )
        backup_evidence = backup.self_test(
            backup_config, now_iso=first_manifest["source"]["captured_at"]
        )

        previous = Path(str(target) + ".previous")
        staged = _copy_to_stage(previous, target)
        swap_staged_into_place(str(staged), str(target))
        if file_sha256(target) != original_sha:
            raise RebuildError("rollback did not restore the original isolated copy")
        return {
            "backup_restore": backup_evidence,
            "cutover": {
                "candidate_sha256": candidate_sha,
                "original_sha256": original_sha,
                "rolled_back": True,
            },
            "first": first_manifest,
            "ok": True,
            "second": second_manifest,
        }
    except BaseException:
        raise


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay frozen Multica rows into an isolated AIStat recovery copy."
    )
    sub = parser.add_subparsers(dest="command")
    capture_parser = sub.add_parser("capture", help="freeze one complete CLI input")
    capture_parser.add_argument("output")
    capture_parser.add_argument("--base-sha", required=True)
    capture_parser.add_argument("--base-ref", required=True)
    capture_parser.add_argument("--base-tree", required=True)
    capture_parser.add_argument("--captured-at", required=True)
    rebuild_parser = sub.add_parser("rebuild", help="replay frozen input")
    rebuild_parser.add_argument("input")
    rebuild_parser.add_argument("output")
    verify_parser = sub.add_parser("verify", help="verify an existing isolated replay")
    verify_parser.add_argument("input")
    verify_parser.add_argument("output")
    self_test_parser = sub.add_parser("self-test", help="prove replay and rollback on copies")
    self_test_parser.add_argument("input")
    self_test_parser.add_argument("recovery_root")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.error("a subcommand is required")
    if args.command == "capture":
        result = capture_inputs(
            Path(args.output),
            config=Config(),
            source={
                "base_sha": args.base_sha,
                "base_ref": args.base_ref,
                "base_tree": args.base_tree,
                "captured_at": args.captured_at,
            },
        )
    elif args.command == "rebuild":
        result = rebuild(Path(args.input), Path(args.output))
    elif args.command == "verify":
        result = verify_rebuild(Path(args.input), Path(args.output))
    else:
        result = rebuild_self_test(Path(args.input), Path(args.recovery_root))
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
