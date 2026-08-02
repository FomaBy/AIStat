"""Automated, verifiable at-rest backup and restore of AIStat's data.

Story FAN-1185. The owner analytics database (``data/aistat.db``) and, once the
"connect your Multica" feature is enabled, the accounts store (``security.db``),
the encrypted worker-token store and the per-tenant databases hold the only
non-reproducible user data on the trusted local machine. The snapshot machinery
in :mod:`aistat.snapshot` moves a coherent copy *between contours*; it is not a
backup at rest. This module fills that gap:

* ``create``    — one integrity-checked, compressed generation per run, pruned
                  to ``AISTAT_BACKUP_RETENTION`` generations;
* ``list``      — enumerate the generations with their manifests;
* ``verify``    — decompress and re-check a generation end to end;
* ``restore``   — atomically install a generation, keeping a ``.pre-restore``
                  safety copy of whatever it replaces;
* ``self-test`` — create → restore into a scratch dir → re-open → verify, the
                  acceptance evidence that a fresh backup really restores;
* ``clean``     — remove orphaned snapshot sidecars left in the data directory.

Everything is standard-library only so a cPanel cron one-shot (no SSH) can run
``python -m aistat.backup create`` directly.
"""

import argparse
import gzip
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import Config
from .db import SCHEMA_VERSION, utcnow_iso
from .snapshot import (
    SnapshotError,
    cleanup_orphan_snapshot_sidecars,
    create_compressed_snapshot,
    validate_snapshot,
)
from .snapshot_recovery import fsync_file

logger = logging.getLogger("aistat.backup")

MANIFEST_NAME = "manifest.json"
BACKUP_PREFIX = "aistat-"
_INCOMING_PREFIX = ".incoming-"
_SIDECAR_SUFFIXES = ("-wal", "-shm")
# The main analytics database's basename; only it carries the AIStat schema, so
# only it gets the schema/required-tables validation on top of integrity_check.
MAIN_DB_NAME = "aistat.db"


class BackupError(Exception):
    """A backup could not be created, verified or restored safely."""


def _assert_no_symlink_components(path: Path, description: str) -> None:
    """Reject symlinks anywhere in a managed filesystem path."""
    raw = Path(path)
    if ".." in raw.parts:
        raise BackupError("%s contains a parent traversal: %s" % (description, path))
    absolute = Path(os.path.abspath(str(raw)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = os.lstat(str(current)).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise BackupError("cannot inspect %s %s: %s" % (description, path, exc))
        if stat.S_ISLNK(mode):
            raise BackupError("%s must not contain a symlink: %s" % (description, current))
        if current != absolute and not stat.S_ISDIR(mode):
            raise BackupError(
                "%s has a non-directory ancestor: %s" % (description, current)
            )


def _lstat_regular_or_missing(path: Path, description: str) -> bool:
    """Validate a managed path and return whether a regular file exists."""
    _assert_no_symlink_components(path, description)
    try:
        mode = os.lstat(str(path)).st_mode
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BackupError("cannot inspect %s %s: %s" % (description, path, exc))
    if not stat.S_ISREG(mode):
        raise BackupError("%s must be a regular file: %s" % (description, path))
    return True


# --------------------------------------------------------------------------- #
# Discovery of the durable databases
# --------------------------------------------------------------------------- #
def _tenant_stores(cfg: Config) -> List[Tuple[str, Path]]:
    """The two per-tenant database directories, by manifest label prefix.

    ``tenants`` holds the snapshots served to a connected tenant; the collector
    writes each connection's own database into ``worker_tenants``. Both are
    keyed by user id, so the *same* basename legitimately exists in both.
    """
    return [
        ("tenants", Path(cfg.tenants_dir)),
        ("worker_tenants", Path(cfg.worker_tenants_dir)),
    ]


def _canonical_targets(cfg: Config) -> Dict[str, Path]:
    """Map each top-level backup member label to the live path it restores to.

    Restore never trusts a path recorded inside a manifest: it looks the member
    up here or in :func:`_tenant_stores` (both derived only from the current
    config), so a tampered manifest can never redirect ``os.replace`` outside
    the data directory.
    """
    targets: Dict[str, Path] = {
        cfg.db_path.name: cfg.db_path,
        cfg.security_db_path.name: cfg.security_db_path,
        cfg.worker_store_path.name: cfg.worker_store_path,
    }
    return targets


def _member_stem(label: str) -> str:
    """Flat, collision-free file stem for a member label inside a generation."""
    return label.replace("/", "__")


def _is_tenant_db_name(name: str) -> bool:
    """True for a plain ``*.db`` basename — never a path or a traversal step."""
    return (
        bool(name)
        and name.endswith(".db")
        and name == os.path.basename(name)
        and "\\" not in name
        and name not in (".", "..")
    )


def _restore_target(cfg: Config, label: str) -> Path:
    """Resolve a manifest label to the live path it may be restored to.

    A top-level label resolves through the canonical map and a
    ``<store>/<basename>`` label through that store's configured directory. A
    bare unknown label keeps generations written before the stores were
    labelled restorable (they only ever held ``tenants`` members). Anything
    else — an unknown store, a nested path, a traversal step — fails closed.
    """
    targets = _canonical_targets(cfg)
    if label in targets:
        return targets[label]
    store, separator, name = label.partition("/")
    if not separator:
        store, name = "tenants", label
    directory = dict(_tenant_stores(cfg)).get(store)
    if directory is None or not _is_tenant_db_name(name):
        raise BackupError(
            "member %s has no known restore target in this config" % label
        )
    return directory / name


def _durable_databases(cfg: Config) -> List[Tuple[str, Path]]:
    """Return ``(label, path)`` for every durable database that exists now.

    The three top-level stores plus every ``*.db`` file directly inside each
    tenant store. Tenant labels carry their store prefix because one user id
    yields the same basename in both stores — an unprefixed label would drop
    one of the two databases from the generation.
    """
    seen: Dict[str, Path] = {}
    candidates: List[Tuple[str, Path]] = [
        (path.name, path)
        for path in (cfg.db_path, cfg.security_db_path, cfg.worker_store_path)
    ]
    for store, directory in _tenant_stores(cfg):
        _assert_no_symlink_components(directory, "%s tenant store" % store)
        if os.path.lexists(str(directory)) and not directory.is_dir():
            raise BackupError("tenant store must be a directory: %s" % directory)
        if directory.is_dir():
            candidates.extend(
                (store + "/" + path.name, path)
                for path in sorted(directory.glob("*.db"))
            )
    for label, path in candidates:
        if not _lstat_regular_or_missing(path, "backup source"):
            continue
        if label in seen:
            logger.warning("skipping duplicate backup member name: %s", label)
            continue
        seen[label] = path
    return list(seen.items())


# --------------------------------------------------------------------------- #
# Integrity helpers
# --------------------------------------------------------------------------- #
def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _integrity_check(path: Path) -> str:
    """Run the full ``PRAGMA integrity_check`` and return ``"ok"`` or the fault."""
    conn = _open_readonly(path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    messages = [str(r[0]) for r in rows]
    if messages == ["ok"]:
        return "ok"
    return "; ".join(messages) or "unknown integrity fault"


def _table_row_counts(path: Path) -> Dict[str, int]:
    conn = _open_readonly(path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: int(conn.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0])
            for name in tables
        }
    finally:
        conn.close()


def _decompress_member(gz_path: Path, target: Path) -> None:
    with gzip.open(str(gz_path), "rb") as source, target.open("wb") as out:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def _verify_db_file(path: Path, *, is_main: bool) -> Optional[int]:
    """Integrity-check ``path`` and, for the main DB, its schema. Returns the
    schema version when known. Raises :class:`BackupError` on any fault."""
    fault = _integrity_check(path)
    if fault != "ok":
        raise BackupError("integrity check failed for %s: %s" % (path.name, fault))
    if is_main:
        try:
            return validate_snapshot(path).schema_version
        except SnapshotError as exc:
            raise BackupError("schema validation failed for %s: %s" % (path.name, exc))
    try:
        conn = _open_readonly(path)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
def _generation_name(now_iso: str) -> str:
    stamp = now_iso.replace("-", "").replace(":", "")
    return BACKUP_PREFIX + stamp


def create_backup(cfg: Config, *, now_iso: Optional[str] = None) -> Path:
    """Create one integrity-checked, compressed backup generation. Returns its
    directory. Prunes older generations to ``cfg.backup_retention``."""
    cfg.ensure_backup_dir()
    databases = _durable_databases(cfg)
    if not databases:
        raise BackupError("no durable databases found to back up")

    now = now_iso or utcnow_iso()
    final_dir = cfg.backup_dir / _generation_name(now)
    suffix = 1
    while final_dir.exists():
        suffix += 1
        final_dir = cfg.backup_dir / (_generation_name(now) + "-%d" % suffix)

    staging = Path(
        tempfile.mkdtemp(prefix=_INCOMING_PREFIX, dir=str(cfg.backup_dir))
    )
    try:
        members = []
        for name, path in databases:
            gz_bytes = create_compressed_snapshot(path)
            member_file = staging / (_member_stem(name) + ".gz")
            member_file.write_bytes(gz_bytes)
            try:
                os.chmod(member_file, 0o600)
            except OSError:
                pass
            # Decompress into a scratch file and prove it restores cleanly
            # *before* the generation is published — a backup that fails its
            # own integrity check is worse than none.
            scratch = staging / (_member_stem(name) + ".check")
            _decompress_member(member_file, scratch)
            try:
                schema_version = _verify_db_file(
                    scratch, is_main=(name == MAIN_DB_NAME)
                )
                plain = scratch.read_bytes()
                members.append(
                    {
                        "label": name,
                        "file": member_file.name,
                        "sha256": hashlib.sha256(plain).hexdigest(),
                        "size_bytes": len(plain),
                        "gz_size_bytes": member_file.stat().st_size,
                        "integrity": "ok",
                        "schema_version": schema_version,
                        "row_counts": _table_row_counts(scratch),
                    }
                )
            finally:
                _unlink_quiet(scratch)

        manifest = {
            "tool": "aistat.backup",
            "created_at": now,
            "server_schema_version": SCHEMA_VERSION,
            "members": members,
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _fsync_dir(staging)
        # A dir rename is atomic on one filesystem, so a reader never observes a
        # half-written generation: it appears only once complete.
        os.replace(str(staging), str(final_dir))
        _fsync_dir(cfg.backup_dir)
    except BaseException:
        _rmtree(staging)
        raise

    _prune(cfg)
    logger.info("created backup %s with %d member(s)", final_dir.name, len(databases))
    return final_dir


def _prune(cfg: Config) -> List[str]:
    generations = _list_generation_dirs(cfg)
    excess = generations[cfg.backup_retention :]
    removed = []
    for gen in excess:
        _rmtree(gen)
        removed.append(gen.name)
    if removed:
        logger.info("pruned %d old backup(s): %s", len(removed), ", ".join(removed))
    return removed


# --------------------------------------------------------------------------- #
# list / verify
# --------------------------------------------------------------------------- #
def _list_generation_dirs(cfg: Config) -> List[Path]:
    if not cfg.backup_dir.is_dir():
        return []
    dirs = [
        d
        for d in cfg.backup_dir.iterdir()
        if d.is_dir()
        and d.name.startswith(BACKUP_PREFIX)
        and (d / MANIFEST_NAME).is_file()
    ]
    # Newest first: the directory name is a sortable UTC stamp.
    return sorted(dirs, key=lambda d: d.name, reverse=True)


def load_manifest(generation: Path) -> dict:
    try:
        return json.loads((generation / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError("cannot read manifest in %s: %s" % (generation.name, exc))


def list_backups(cfg: Config) -> List[dict]:
    out = []
    for gen in _list_generation_dirs(cfg):
        manifest = load_manifest(gen)
        out.append({"name": gen.name, "path": str(gen), "manifest": manifest})
    return out


def resolve_backup(cfg: Config, ref: str) -> Path:
    """Resolve ``latest``, a generation name, or an explicit path to a dir."""
    if ref == "latest":
        generations = _list_generation_dirs(cfg)
        if not generations:
            raise BackupError("no backups found in %s" % cfg.backup_dir)
        return generations[0]
    candidate = Path(ref)
    if not candidate.is_absolute():
        candidate = cfg.backup_dir / ref
    if not (candidate / MANIFEST_NAME).is_file():
        raise BackupError("not a backup generation: %s" % ref)
    return candidate


def _validated_manifest_members(cfg: Config, generation: Path, manifest: dict):
    """Validate all manifest paths before any archive member is read."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("members"), list):
        raise BackupError("manifest members must be a list")
    members = manifest["members"]
    if not members:
        raise BackupError("backup manifest has no members")
    seen_labels = set()
    seen_files = set()
    validated = []
    for member in members:
        if not isinstance(member, dict):
            raise BackupError("manifest member must be an object")
        label = member.get("label")
        member_file = member.get("file")
        if not isinstance(label, str) or not label:
            raise BackupError("manifest member has an invalid label")
        if not isinstance(member_file, str) or not member_file:
            raise BackupError("manifest member %s has an invalid file" % label)
        target = _restore_target(cfg, label)
        expected_file = _member_stem(label) + ".gz"
        if (
            member_file != os.path.basename(member_file)
            or "\\" in member_file
            or member_file in (".", "..")
            or member_file != expected_file
        ):
            raise BackupError(
                "member %s must use archive file %s" % (label, expected_file)
            )
        if label in seen_labels:
            raise BackupError("duplicate manifest member label: %s" % label)
        if member_file in seen_files:
            raise BackupError("duplicate manifest member file: %s" % member_file)
        seen_labels.add(label)
        seen_files.add(member_file)
        source = generation / member_file
        try:
            mode = os.lstat(str(source)).st_mode
        except OSError as exc:
            raise BackupError("cannot inspect member file %s: %s" % (member_file, exc))
        if not stat.S_ISREG(mode):
            raise BackupError(
                "member file must be a direct regular non-symlink child: %s"
                % member_file
            )
        validated.append((member, label, source, target))
    return validated


def verify_backup(cfg: Config, ref: str) -> dict:
    """Decompress every member of a generation and re-check it end to end."""
    generation = resolve_backup(cfg, ref)
    manifest = load_manifest(generation)
    members = _validated_manifest_members(cfg, generation, manifest)
    checked = []
    with tempfile.TemporaryDirectory(prefix=".aistat-verify-") as tmp:
        tmp_dir = Path(tmp)
        for member, label, gz_path, _target in members:
            scratch = tmp_dir / _member_stem(label)
            _decompress_member(gz_path, scratch)
            digest = hashlib.sha256(scratch.read_bytes()).hexdigest()
            if digest != member.get("sha256"):
                raise BackupError(
                    "checksum mismatch for %s in %s"
                    % (label, generation.name)
                )
            _verify_db_file(scratch, is_main=(label == MAIN_DB_NAME))
            checked.append(label)
    return {"name": generation.name, "verified_members": checked}


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def _restore_state_paths(target: Path) -> List[Path]:
    return [
        target,
        target.with_name(target.name + ".pre-restore"),
    ] + [Path(str(target) + suffix) for suffix in _SIDECAR_SUFFIXES]


def _validate_restore_target(target: Path) -> None:
    """Reject every target hazard before the first live path is changed."""
    for path in _restore_state_paths(target):
        _lstat_regular_or_missing(path, "restore path")


def _missing_directories(parent: Path) -> List[Path]:
    missing = []
    current = parent
    while not os.path.lexists(str(current)):
        missing.append(current)
        if current == current.parent:
            break
        current = current.parent
    return missing


def _snapshot_restore_state(planned, snapshot_dir: Path):
    """Copy the exact pre-command file state before commit begins."""
    state = []
    missing_dirs = set()
    for index, (_label, _scratch, target) in enumerate(planned):
        missing_dirs.update(_missing_directories(target.parent))
        for path_index, path in enumerate(_restore_state_paths(target)):
            snapshot = None
            if os.path.lexists(str(path)):
                snapshot = snapshot_dir / ("%d-%d" % (index, path_index))
                shutil.copy2(str(path), str(snapshot))
            state.append((path, snapshot))
    return state, missing_dirs


def _same_file_state(path: Path, snapshot: Path) -> bool:
    try:
        current_stat = path.stat()
        snapshot_stat = snapshot.stat()
    except OSError:
        return False
    if (
        stat.S_IMODE(current_stat.st_mode) != stat.S_IMODE(snapshot_stat.st_mode)
        or current_stat.st_size != snapshot_stat.st_size
        or current_stat.st_mtime_ns != snapshot_stat.st_mtime_ns
    ):
        return False
    with path.open("rb") as current, snapshot.open("rb") as saved:
        while True:
            current_chunk = current.read(1024 * 1024)
            if current_chunk != saved.read(1024 * 1024):
                return False
            if not current_chunk:
                return True


def _rollback_restore(state, missing_dirs) -> None:
    errors = []
    for path, snapshot in reversed(state):
        try:
            if snapshot is None:
                if os.path.lexists(str(path)):
                    _unlink_quiet(path)
            elif not _same_file_state(path, snapshot):
                shutil.copy2(str(snapshot), str(path))
                fsync_file(path)
                _fsync_dir(path.parent)
        except OSError as exc:
            errors.append("%s: %s" % (path, exc))
    for directory in sorted(
        missing_dirs, key=lambda path: len(path.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append("%s: %s" % (directory, exc))
    if errors:
        raise BackupError("restore rollback failed: %s" % "; ".join(errors))


def _atomic_install(scratch: Path, target: Path) -> None:
    """Swap ``scratch`` over ``target`` after copying the old file aside.

    The caller snapshots all targets and rolls the whole plan back if this
    synchronous install raises.
    """
    _validate_restore_target(target)
    if not target.parent.is_dir():
        # A restore onto a fresh machine recreates a missing store directory
        # (e.g. data/worker_tenants) owner-only, like the runtime itself does.
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(target.parent, 0o700)
        except OSError:
            pass
    previous = target.with_name(target.name + ".pre-restore")
    if target.exists():
        shutil.copy2(str(target), str(previous))
        try:
            os.chmod(previous, 0o600)
        except OSError:
            pass
    # A restored file comes from a checkpointed .backup() copy, so it has no
    # live WAL; clear any stale sidecar so the new inode is not shadowed.
    for suffix in _SIDECAR_SUFFIXES:
        _unlink_quiet(Path(str(target) + suffix))
    os.replace(str(scratch), str(target))
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    fsync_file(target)
    _fsync_dir(target.parent)


def restore_backup(
    cfg: Config, ref: str, *, only: Optional[str] = None, dry_run: bool = False
) -> dict:
    """Restore a generation into the live data directory.

    Every member is decompressed, integrity-checked and checksum-matched before
    anything live is touched; the replaced file is preserved as ``.pre-restore``.
    Synchronous commit failures roll every member back to its pre-command state.
    ``only`` limits the restore to a single member label.
    """
    generation = resolve_backup(cfg, ref)
    manifest = load_manifest(generation)
    members = _validated_manifest_members(cfg, generation, manifest)
    selected = [entry for entry in members if not only or entry[1] == only]
    if only and not selected:
        raise BackupError("member not found in backup: %s" % only)

    targets = set()
    for _member, label, _source, target in selected:
        target_key = os.path.abspath(str(target))
        if target_key in targets:
            raise BackupError("multiple manifest members restore to %s" % target)
        targets.add(target_key)
        _validate_restore_target(target)
    _assert_no_symlink_components(cfg.db_path.parent, "restore scratch directory")

    with tempfile.TemporaryDirectory(
        prefix=".aistat-restore-", dir=str(cfg.db_path.parent)
    ) as tmp:
        tmp_dir = Path(tmp)
        planned = []
        for member, label, gz_path, target in selected:
            scratch = tmp_dir / _member_stem(label)
            _decompress_member(gz_path, scratch)
            digest = hashlib.sha256(scratch.read_bytes()).hexdigest()
            if digest != member.get("sha256"):
                raise BackupError("checksum mismatch for %s" % label)
            _verify_db_file(scratch, is_main=(label == MAIN_DB_NAME))
            planned.append((label, scratch, target))

        if dry_run:
            return {
                "name": generation.name,
                "dry_run": True,
                "would_restore": [
                    {"label": lbl, "target": str(tgt)} for lbl, _s, tgt in planned
                ],
            }
        state, missing_dirs = _snapshot_restore_state(planned, tmp_dir)
        try:
            for _label, scratch, target in planned:
                _atomic_install(scratch, target)
        except BaseException as exc:
            try:
                _rollback_restore(state, missing_dirs)
            except BackupError as rollback_exc:
                raise BackupError(
                    "restore failed (%s) and compensation failed (%s)"
                    % (exc, rollback_exc)
                ) from exc
            raise
        restored = [
            {"label": label, "target": str(target)}
            for label, _scratch, target in planned
        ]
    logger.info("restored %d member(s) from %s", len(restored), generation.name)
    return {"name": generation.name, "restored": restored}


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
def self_test(cfg: Config, *, now_iso: Optional[str] = None) -> dict:
    """Create a fresh backup, restore it into a scratch directory, re-open the
    restored databases and confirm they match the backup. Never touches live
    data. Returns ``{"ok": bool, ...}``; raises :class:`BackupError` on failure."""
    generation = create_backup(cfg, now_iso=now_iso)
    manifest = load_manifest(generation)
    members = _validated_manifest_members(cfg, generation, manifest)
    results = []
    with tempfile.TemporaryDirectory(prefix=".aistat-selftest-") as tmp:
        tmp_dir = Path(tmp)
        for member, label, gz_path, _target in members:
            restored = tmp_dir / _member_stem(label)
            _decompress_member(gz_path, restored)
            digest = hashlib.sha256(restored.read_bytes()).hexdigest()
            if digest != member.get("sha256"):
                raise BackupError("self-test checksum mismatch for %s" % label)
            _verify_db_file(restored, is_main=(label == MAIN_DB_NAME))
            counts = _table_row_counts(restored)
            if counts != member.get("row_counts"):
                raise BackupError(
                    "self-test row counts differ for %s after restore" % label
                )
            results.append({"label": label, "row_counts": counts})
    return {"ok": True, "backup": generation.name, "members": results}


# --------------------------------------------------------------------------- #
# clean (orphan snapshot sidecars)
# --------------------------------------------------------------------------- #
def clean(cfg: Config, *, dry_run: bool = True) -> dict:
    """Report (and, unless ``dry_run``, remove) orphaned snapshot sidecars.

    Only the throwaway ``.aistat-snapshot-*.db-{wal,shm}`` sidecars whose parent
    temp database is already gone are touched. Real databases, ``.env`` files,
    operator credentials and the TLS bundle are never candidates.
    """
    parent = cfg.db_path.parent
    orphans = sorted(
        str(p)
        for p in parent.glob(".aistat-snapshot-*.db-*")
        if p.name.endswith(_SIDECAR_SUFFIXES)
        and not p.with_name(p.name[:-4]).exists()
    )
    removed = 0
    if not dry_run:
        removed = cleanup_orphan_snapshot_sidecars(parent)
    return {"orphan_sidecars": orphans, "removed": removed, "dry_run": dry_run}


# --------------------------------------------------------------------------- #
# small filesystem helpers
# --------------------------------------------------------------------------- #
def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(str(path), ignore_errors=True)


def _unlink_quiet(path: Path) -> None:
    """Delete ``path`` if it exists, ignoring a missing file.

    ``Path.unlink(missing_ok=True)`` is Python 3.8+; the production host runs
    Python 3.6.8, so the missing-file case is swallowed explicitly instead.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aistat.backup",
        description="Backup, verify and restore AIStat's SQLite data.",
    )
    # ``add_subparsers(required=...)`` is Python 3.7+; the production host runs
    # 3.6.8, so leave the subparsers optional here and enforce a command after
    # parsing (below) instead.
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("create", help="create one integrity-checked backup generation")
    sub.add_parser("list", help="list backup generations")
    p_verify = sub.add_parser("verify", help="verify a generation end to end")
    p_verify.add_argument("ref", nargs="?", default="latest")
    p_restore = sub.add_parser("restore", help="restore a generation into data/")
    p_restore.add_argument("ref", nargs="?", default="latest")
    p_restore.add_argument(
        "--only", help="restore a single member label (e.g. worker_tenants/7.db)"
    )
    p_restore.add_argument(
        "--dry-run", action="store_true", help="show what would be restored"
    )
    p_restore.add_argument(
        "--yes", action="store_true", help="required to overwrite live data"
    )
    sub.add_parser(
        "self-test", help="create+restore into a scratch dir and verify (no live write)"
    )
    p_clean = sub.add_parser("clean", help="remove orphan snapshot sidecars")
    p_clean.add_argument(
        "--apply", action="store_true", help="actually delete (default is dry-run)"
    )
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_usage(sys.stderr)
        print("error: a command is required", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    cfg = Config()

    try:
        if args.command == "create":
            path = create_backup(cfg)
            print(path)
            return 0
        if args.command == "list":
            backups = list_backups(cfg)
            if not backups:
                print("(no backups)")
            for entry in backups:
                members = entry["manifest"].get("members", [])
                labels = ", ".join(m["label"] for m in members)
                print(
                    "%s  created=%s  members=%s"
                    % (entry["name"], entry["manifest"].get("created_at"), labels)
                )
            return 0
        if args.command == "verify":
            report = verify_backup(cfg, args.ref)
            print("OK %s verified: %s" % (report["name"], ", ".join(report["verified_members"])))
            return 0
        if args.command == "restore":
            if not args.dry_run and not args.yes:
                print(
                    "refusing to overwrite live data without --yes "
                    "(use --dry-run to preview)",
                    file=sys.stderr,
                )
                return 2
            report = restore_backup(
                cfg, args.ref, only=args.only, dry_run=args.dry_run
            )
            print(json.dumps(report, indent=2))
            return 0
        if args.command == "self-test":
            report = self_test(cfg)
            print("PASS restore self-test from %s" % report["backup"])
            for member in report["members"]:
                print("  %s: %d table(s)" % (member["label"], len(member["row_counts"])))
            return 0
        if args.command == "clean":
            report = clean(cfg, dry_run=not args.apply)
            if not report["orphan_sidecars"]:
                print("no orphan snapshot sidecars found")
            else:
                verb = "removed" if args.apply else "would remove"
                print("%s %d orphan sidecar(s):" % (verb, len(report["orphan_sidecars"])))
                for path in report["orphan_sidecars"]:
                    print("  %s" % path)
            return 0
    except BackupError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
