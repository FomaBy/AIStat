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
import sqlite3
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


# --------------------------------------------------------------------------- #
# Discovery of the durable databases
# --------------------------------------------------------------------------- #
def _canonical_targets(cfg: Config) -> Dict[str, Path]:
    """Map each backup member basename to the live path it restores to.

    Restore never trusts a path recorded inside a manifest: it looks the member
    up in this map (derived only from the current config), so a tampered
    manifest can never redirect ``os.replace`` outside the data directory.
    """
    targets: Dict[str, Path] = {
        cfg.db_path.name: cfg.db_path,
        cfg.security_db_path.name: cfg.security_db_path,
        cfg.worker_store_path.name: cfg.worker_store_path,
    }
    return targets


def _durable_databases(cfg: Config) -> List[Tuple[str, Path]]:
    """Return ``(basename, path)`` for every durable database that exists now.

    The three top-level stores plus every ``*.db`` file directly inside the
    tenants directory. Basenames are unique across these sources by
    construction; a duplicate is skipped rather than silently overwritten.
    """
    seen: Dict[str, Path] = {}
    candidates: List[Path] = [
        cfg.db_path,
        cfg.security_db_path,
        cfg.worker_store_path,
    ]
    if cfg.tenants_dir.is_dir():
        candidates.extend(sorted(cfg.tenants_dir.glob("*.db")))
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        if name in seen:
            logger.warning("skipping duplicate backup member name: %s", name)
            continue
        seen[name] = path
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
            member_file = staging / (name + ".gz")
            member_file.write_bytes(gz_bytes)
            try:
                os.chmod(member_file, 0o600)
            except OSError:
                pass
            # Decompress into a scratch file and prove it restores cleanly
            # *before* the generation is published — a backup that fails its
            # own integrity check is worse than none.
            scratch = staging / (name + ".check")
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


def verify_backup(cfg: Config, ref: str) -> dict:
    """Decompress every member of a generation and re-check it end to end."""
    generation = resolve_backup(cfg, ref)
    manifest = load_manifest(generation)
    checked = []
    with tempfile.TemporaryDirectory(prefix=".aistat-verify-") as tmp:
        tmp_dir = Path(tmp)
        for member in manifest.get("members", []):
            gz_path = generation / member["file"]
            if not gz_path.is_file():
                raise BackupError("missing member file: %s" % member["file"])
            scratch = tmp_dir / member["label"]
            _decompress_member(gz_path, scratch)
            digest = hashlib.sha256(scratch.read_bytes()).hexdigest()
            if digest != member.get("sha256"):
                raise BackupError(
                    "checksum mismatch for %s in %s"
                    % (member["label"], generation.name)
                )
            _verify_db_file(scratch, is_main=(member["label"] == MAIN_DB_NAME))
            checked.append(member["label"])
    return {"name": generation.name, "verified_members": checked}


# --------------------------------------------------------------------------- #
# restore
# --------------------------------------------------------------------------- #
def _atomic_install(scratch: Path, target: Path) -> None:
    """Swap ``scratch`` over ``target`` after copying the old file aside.

    Everything before ``os.replace`` may raise and leaves the live file intact;
    everything after it is best-effort. A caught error therefore always means
    the live database was not touched.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise BackupError("restore target must not be a symlink: %s" % target)
    previous = target.with_name(target.name + ".pre-restore")
    if previous.is_symlink():
        raise BackupError("pre-restore backup must not be a symlink: %s" % previous)
    if target.exists():
        import shutil

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
    ``only`` limits the restore to a single member basename.
    """
    generation = resolve_backup(cfg, ref)
    manifest = load_manifest(generation)
    targets = _canonical_targets(cfg)
    planned = []
    restored = []
    with tempfile.TemporaryDirectory(
        prefix=".aistat-restore-", dir=str(cfg.db_path.parent)
    ) as tmp:
        tmp_dir = Path(tmp)
        for member in manifest.get("members", []):
            label = member["label"]
            if only and label != only:
                continue
            target = targets.get(label)
            if target is None and cfg.tenants_dir.is_dir():
                # A tenant member restores back into the tenants directory only.
                target = cfg.tenants_dir / label
            if target is None:
                raise BackupError(
                    "member %s has no known restore target in this config" % label
                )
            gz_path = generation / member["file"]
            if not gz_path.is_file():
                raise BackupError("missing member file: %s" % member["file"])
            scratch = tmp_dir / label
            _decompress_member(gz_path, scratch)
            digest = hashlib.sha256(scratch.read_bytes()).hexdigest()
            if digest != member.get("sha256"):
                raise BackupError("checksum mismatch for %s" % label)
            _verify_db_file(scratch, is_main=(label == MAIN_DB_NAME))
            planned.append((label, scratch, target))

        if only and not planned:
            raise BackupError("member not found in backup: %s" % only)
        if dry_run:
            return {
                "name": generation.name,
                "dry_run": True,
                "would_restore": [
                    {"label": lbl, "target": str(tgt)} for lbl, _s, tgt in planned
                ],
            }
        for label, scratch, target in planned:
            _atomic_install(scratch, target)
            restored.append({"label": label, "target": str(target)})
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
    members = manifest.get("members", [])
    if not members:
        raise BackupError("fresh backup has no members to test")
    results = []
    with tempfile.TemporaryDirectory(prefix=".aistat-selftest-") as tmp:
        tmp_dir = Path(tmp)
        for member in members:
            label = member["label"]
            gz_path = generation / member["file"]
            restored = tmp_dir / label
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
    p_restore.add_argument("--only", help="restore a single member basename")
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
