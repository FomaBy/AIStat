"""Independent, encrypted off-site copy of AIStat backups (FAN-3462).

``aistat.backup`` (FAN-1185) keeps integrity-checked generations on the same
machine as the live databases. This module adds the missing half of the
recovery story:

* ``push``       — bundle the newest local generation, encrypt it with a key
                   that lives only in the environment, and atomically publish
                   it to a target directory that must be outside the local
                   backup tree (a mounted external volume, an rclone/SSHFS
                   mount of a free tier, or another machine's disk — anything
                   reachable as a path, at no cost);
* ``list``       — enumerate the off-site bundles with their metadata;
* ``verify``     — decrypt a bundle and re-check every member checksum;
* ``drill``      — an *isolated* restore drill: decrypt, extract, checksum,
                   integrity-check and run the critical queries against the
                   restored copies only. Live data is never touched, and the
                   measured wall time is the RTO evidence;
* ``rotate-log`` — size/age-capped rotation for operational logs that must
                   never grow unbounded yet never delete audit evidence
                   (manifests, drill reports and alert events are not logs).

Everything is standard-library plus the system ``openssl`` binary, so a cPanel
cron one-shot can run it directly. The encryption key is read from
``AISTAT_BACKUP_ENCRYPTION_KEY`` at call time and handed to openssl via
``-pass env:`` — it never appears in argv, logs, manifests or reports.
"""

import argparse
import hashlib
import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from .backup import (
    BackupError,
    _decompress_member,
    _table_row_counts,
    _verify_db_file,
    load_manifest,
    resolve_backup,
)
from .config import Config
from .db import utcnow_iso

logger = logging.getLogger("aistat.backup_offsite")

KEY_ENV = "AISTAT_BACKUP_ENCRYPTION_KEY"
BUNDLE_SUFFIX = ".tar.gz.enc"
META_SUFFIX = ".json"
ALERTS_NAME = "alerts.jsonl"
DRILL_REPORT_NAME = "drill-report.json"
_INCOMING_PREFIX = ".incoming-"
_ENCRYPTION = "aes-256-cbc-pbkdf2"
# Matches the documented recovery objective; the drill only *measures* against
# it, a slower drill is a warning in the report, not a failure.
RTO_TARGET_SECONDS = 4 * 3600


class OffsiteError(Exception):
    """An off-site copy could not be pushed, verified or drilled."""


def _assert_no_symlink_components(path, description: str) -> None:
    """Reject parent traversal and symlinks anywhere in a managed path."""
    raw = Path(path)
    if ".." in raw.parts:
        raise OffsiteError("%s contains a parent traversal: %s" % (description, path))
    absolute = Path(os.path.abspath(str(raw)))
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = os.lstat(str(current)).st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise OffsiteError(
                "cannot inspect %s %s: %s" % (description, path, exc)
            )
        if stat.S_ISLNK(mode):
            raise OffsiteError(
                "%s must not contain a symlink: %s" % (description, current)
            )


# --------------------------------------------------------------------------- #
# target independence
# --------------------------------------------------------------------------- #
def _resolve_target_dir(cfg: Config) -> Path:
    """Validate the off-site target and return it, creating it if needed.

    The target must be outside the local backup tree (and the local backup tree
    must not be inside it) so an accident on one directory can never take both
    copies. Whether it sits on a different *physical* device is reported by
    ``push`` as ``same_device`` — the runbook tells the operator to mount a
    genuinely independent volume there.
    """
    target = Path(cfg.offsite_backup_dir)
    backup_dir = Path(cfg.backup_dir)
    _assert_no_symlink_components(target, "off-site backup target")
    try:
        target_resolved = target.resolve()
        backup_resolved = backup_dir.resolve()
    except OSError as exc:
        raise OffsiteError("cannot resolve off-site target %s: %s" % (target, exc))
    if (
        target_resolved == backup_resolved
        or target_resolved in backup_resolved.parents
        or backup_resolved in target_resolved.parents
    ):
        raise OffsiteError(
            "off-site target %s must be outside the local backup tree %s"
            % (target, backup_dir)
        )
    target.mkdir(parents=True, exist_ok=True)
    return target


def _same_device(a: Path, b: Path) -> bool:
    try:
        return os.stat(str(a)).st_dev == os.stat(str(b)).st_dev
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# encryption
# --------------------------------------------------------------------------- #
def _require_key() -> str:
    key = os.environ.get(KEY_ENV, "")
    if not key:
        raise OffsiteError(
            "%s is not set; refusing to write an unencrypted off-site copy" % KEY_ENV
        )
    return key


def _run_openssl(args: List[str], key: str, data: bytes) -> bytes:
    """Run ``openssl enc`` with the key passed via the environment only."""
    env = dict(os.environ)
    env[KEY_ENV] = key
    try:
        proc = subprocess.run(
            ["/usr/bin/openssl" if os.path.exists("/usr/bin/openssl") else "openssl"]
            + args
            + ["-pass", "env:%s" % KEY_ENV],
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise OffsiteError("cannot run openssl: %s" % exc)
    if proc.returncode != 0:
        # stderr never contains the key; it is safe to surface.
        raise OffsiteError(
            "openssl failed (%d): %s"
            % (proc.returncode, proc.stderr.decode("utf-8", "replace").strip())
        )
    return proc.stdout


def _encrypt(key: str, plain: bytes) -> bytes:
    return _run_openssl(["enc", "-aes-256-cbc", "-salt", "-pbkdf2"], key, plain)


def _decrypt(key: str, blob: bytes) -> bytes:
    return _run_openssl(["enc", "-d", "-aes-256-cbc", "-pbkdf2"], key, blob)


# --------------------------------------------------------------------------- #
# alerts (dedupe-ready; never pruned by log rotation)
# --------------------------------------------------------------------------- #
def record_alert(
    cfg: Config, kind: str, dedupe_key: str, reason: str, *, now_iso: Optional[str] = None
) -> dict:
    """Append one alert event to the off-site target's ``alerts.jsonl``.

    The ``dedupe_key`` is stable for the same failure class + generation, so an
    alert consumer can collapse retries. Never raises: alerting must not mask
    the original backup error.
    """
    event = {
        "timestamp": now_iso or utcnow_iso(),
        "kind": kind,
        "dedupe_key": dedupe_key,
        "reason": reason,
    }
    try:
        target = _resolve_target_dir(cfg)
        with (target / ALERTS_NAME).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
    except (OffsiteError, BackupError, OSError) as exc:
        logger.error("could not record off-site alert event: %s", exc)
    return event


# --------------------------------------------------------------------------- #
# bundle helpers
# --------------------------------------------------------------------------- #
def _bundle_paths(target: Path, generation: Path):
    stem = generation.name
    return target / (stem + BUNDLE_SUFFIX), target / (stem + META_SUFFIX)


def _meta_path(enc_path: Path) -> Path:
    """``<generation>.tar.gz.enc`` -> ``<generation>.json`` sidecar."""
    return enc_path.with_name(
        enc_path.name[: -len(BUNDLE_SUFFIX)] + META_SUFFIX
    )


def _list_bundles(cfg: Config) -> List[dict]:
    target = _resolve_target_dir(cfg)
    out = []
    for meta in sorted(target.glob("*" + META_SUFFIX)):
        if meta.name in (ALERTS_NAME, DRILL_REPORT_NAME):
            continue
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data.setdefault("name", meta.name[: -len(META_SUFFIX)])
        out.append(data)
    return sorted(out, key=lambda d: str(d.get("generation", "")), reverse=True)


def _resolve_bundle(cfg: Config, ref: str):
    """Resolve ``latest``, a generation name or an explicit ``.enc`` path."""
    if ref == "latest":
        bundles = _list_bundles(cfg)
        if not bundles:
            raise OffsiteError("no off-site bundles found")
        return Path(bundles[0]["enc_path"])
    candidate = Path(ref)
    if not candidate.is_absolute():
        candidate = Path(cfg.offsite_backup_dir) / ref
        if not candidate.name.endswith(BUNDLE_SUFFIX):
            candidate = candidate.with_name(candidate.name + BUNDLE_SUFFIX)
    if not candidate.is_file():
        raise OffsiteError("not an off-site bundle: %s" % ref)
    return candidate


def _read_bundle(cfg: Config, enc_path: Path) -> dict:
    """Decrypt a bundle, verify its tar checksum and return the extracted
    generation directory inside a caller-managed temp directory."""
    key = _require_key()
    meta_path = _meta_path(enc_path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OffsiteError("cannot read bundle metadata %s: %s" % (meta_path.name, exc))
    blob = enc_path.read_bytes()
    if hashlib.sha256(blob).hexdigest() != meta.get("enc_sha256"):
        raise OffsiteError("off-site bundle checksum mismatch: %s" % enc_path.name)
    tar_bytes = _decrypt(key, blob)
    if hashlib.sha256(tar_bytes).hexdigest() != meta.get("tar_sha256"):
        raise OffsiteError(
            "decrypted bundle checksum mismatch: %s (wrong key or corrupt copy)"
            % enc_path.name
        )
    extract_dir = enc_path.parent / (
        ".extract-" + enc_path.name[: -len(BUNDLE_SUFFIX)]
    )
    if extract_dir.exists():
        shutil.rmtree(str(extract_dir), ignore_errors=True)
    extract_dir.mkdir(parents=True)
    # Safe extraction: only regular files/dirs below extract_dir.
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            dest = (extract_dir / member.name).resolve()
            if extract_dir.resolve() not in dest.parents and dest != extract_dir.resolve():
                raise OffsiteError(
                    "bundle %s contains an unsafe path: %s" % (enc_path.name, member.name)
                )
            tar.extract(member, str(extract_dir))
    generation = extract_dir / str(meta.get("generation", ""))
    if not (generation / "manifest.json").is_file():
        raise OffsiteError("bundle %s has no backup generation inside" % enc_path.name)
    return {"meta": meta, "generation": generation, "extract_dir": extract_dir}


# --------------------------------------------------------------------------- #
# push
# --------------------------------------------------------------------------- #
def push_offsite(cfg: Config, *, now_iso: Optional[str] = None) -> dict:
    """Publish the newest local backup generation as an encrypted off-site bundle.

    The bundle is staged under ``.incoming-`` and atomically renamed, so a
    failure or retry never damages the previous good bundle. Any failure
    records a dedupe-ready alert event and re-raises.
    """
    target = _resolve_target_dir(cfg)
    now = now_iso or utcnow_iso()
    try:
        generation = resolve_backup(cfg, "latest")
    except BackupError as exc:
        record_alert(cfg, "offsite_push_failed", "push:no-local-generation", str(exc), now_iso=now)
        raise OffsiteError(str(exc))

    def _fail(dedupe_key: str, reason: str) -> OffsiteError:
        record_alert(cfg, "offsite_push_failed", dedupe_key, reason, now_iso=now)
        return OffsiteError(reason)

    enc_path, meta_path = _bundle_paths(target, generation)
    if enc_path.exists():
        # Already published; idempotent no-op keeps retries safe.
        return {"pushed": False, "reason": "already-published", "bundle": str(enc_path)}

    key = _require_key()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(str(generation), arcname=generation.name)
    tar_bytes = buf.getvalue()
    blob = _encrypt(key, tar_bytes)

    staging = target / (
        _INCOMING_PREFIX + generation.name + BUNDLE_SUFFIX
    )
    try:
        _assert_no_symlink_components(staging, "off-site staging file")
        staging.write_bytes(blob)
        try:
            os.chmod(str(staging), 0o600)
        except OSError:
            pass
        meta = {
            "tool": "aistat.backup_offsite",
            "encryption": _ENCRYPTION,
            "generation": generation.name,
            "enc_path": str(enc_path),
            "pushed_at": now,
            "enc_sha256": hashlib.sha256(blob).hexdigest(),
            "enc_size_bytes": len(blob),
            "tar_sha256": hashlib.sha256(tar_bytes).hexdigest(),
            "same_device_as_local": _same_device(target, Path(cfg.backup_dir)),
        }
        meta_staging = target / (_INCOMING_PREFIX + meta_path.name)
        meta_staging.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        os.replace(str(staging), str(enc_path))
        os.replace(str(meta_staging), str(meta_path))
    except (OffsiteError, BackupError, OSError) as exc:
        for leftover in (staging, target / (_INCOMING_PREFIX + meta_path.name)):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise _fail("push:%s" % generation.name, "push failed: %s" % exc) from exc

    removed = _prune_offsite(cfg)
    logger.info(
        "pushed off-site bundle %s (%d bytes)%s",
        enc_path.name,
        len(blob),
        "" if removed is None else "; pruned %d old bundle(s)" % removed,
    )
    return {
        "pushed": True,
        "bundle": str(enc_path),
        "generation": generation.name,
        "enc_size_bytes": len(blob),
        "same_device_as_local": meta["same_device_as_local"],
        "pruned": removed or 0,
    }


def _prune_offsite(cfg: Config) -> Optional[int]:
    """Keep only the newest ``cfg.offsite_retention`` bundles. Never touches
    alerts, drill reports or anything that is not a bundle+metadata pair."""
    target = Path(cfg.offsite_backup_dir)
    bundles = _list_bundles(cfg)
    excess = bundles[cfg.offsite_retention :]
    removed = 0
    for meta in excess:
        enc_path = Path(meta.get("enc_path", ""))
        if enc_path.is_file():
            try:
                enc_path.unlink()
                removed += 1
            except OSError:
                continue
        meta_path = target / (meta["name"] + META_SUFFIX)
        try:
            meta_path.unlink()
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------- #
# verify / isolated restore drill
# --------------------------------------------------------------------------- #
def _check_generation(generation: Path) -> List[dict]:
    """Checksum, integrity-check and count rows for every member of an
    extracted generation. Raises :class:`OffsiteError` on the first fault."""
    manifest = load_manifest(generation)
    members = manifest.get("members")
    if not isinstance(members, list) or not members:
        raise OffsiteError("bundle manifest has no members: %s" % generation.name)
    checked = []
    with tempfile.TemporaryDirectory(prefix=".aistat-offsite-") as tmp:
        tmp_dir = Path(tmp)
        for member in members:
            label = member.get("label")
            gz_path = generation / (str(label).replace("/", "__") + ".gz")
            scratch = tmp_dir / str(label).replace("/", "__")
            _decompress_member(gz_path, scratch)
            digest = hashlib.sha256(scratch.read_bytes()).hexdigest()
            if digest != member.get("sha256"):
                raise OffsiteError("checksum mismatch for %s in %s" % (label, generation.name))
            schema_version = _verify_db_file(scratch, is_main=(label == "aistat.db"))
            counts = _table_row_counts(scratch)
            if counts != member.get("row_counts"):
                raise OffsiteError(
                    "row counts differ for %s after restore" % label
                )
            checked.append(
                {
                    "label": label,
                    "sha256": digest,
                    "schema_version": schema_version,
                    "row_counts": counts,
                }
            )
    return checked


def _cleanup_extract(extract_dir: Path) -> None:
    shutil.rmtree(str(extract_dir), ignore_errors=True)


def verify_offsite(cfg: Config, ref: str = "latest") -> dict:
    """Decrypt and fully re-check one off-site bundle."""
    enc_path = _resolve_bundle(cfg, ref)
    bundle = _read_bundle(cfg, enc_path)
    try:
        checked = _check_generation(bundle["generation"])
    finally:
        _cleanup_extract(bundle["extract_dir"])
    return {
        "bundle": str(enc_path),
        "generation": bundle["meta"].get("generation"),
        "verified_members": [c["label"] for c in checked],
    }


def restore_drill_offsite(cfg: Config, ref: str = "latest") -> dict:
    """Isolated restore drill: decrypt → extract → checksum → integrity →
    critical queries, entirely in scratch space. Live data is never touched.

    Returns a report with per-step timings (the measured RTO evidence) and
    writes it next to the bundles as ``drill-report.json``.
    """
    started = time.monotonic()
    enc_path = _resolve_bundle(cfg, ref)
    steps = {}

    def _step(name):
        class _Timer(object):
            def __enter__(self):
                self.t0 = time.monotonic()
                return self

            def __exit__(self, *exc):
                steps[name] = round(time.monotonic() - self.t0, 3)

        return _Timer()

    ok = False
    failure = None
    checked: List[dict] = []
    bundle = None
    try:
        with _step("decrypt_extract"):
            bundle = _read_bundle(cfg, enc_path)
        with _step("verify_members"):
            checked = _check_generation(bundle["generation"])
        with _step("critical_queries"):
            main = [c for c in checked if c["label"] == "aistat.db"]
            if not main:
                raise OffsiteError("bundle has no aistat.db member")
            counts = main[0]["row_counts"]
            if int(counts.get("issues", -1)) < 0 or "issues" not in counts:
                raise OffsiteError("critical query failed: issues table missing")
        ok = True
    except (OffsiteError, BackupError) as exc:
        failure = str(exc)
        record_alert(cfg, "offsite_drill_failed", "drill:%s" % enc_path.name, failure)
    finally:
        if bundle is not None:
            _cleanup_extract(bundle["extract_dir"])

    elapsed = round(time.monotonic() - started, 3)
    report = {
        "tool": "aistat.backup_offsite.drill",
        "executed_at": utcnow_iso(),
        "bundle": str(enc_path),
        "generation": bundle["meta"].get("generation") if bundle else None,
        "ok": ok,
        "failure": failure,
        "steps_seconds": steps,
        "measured_rto_seconds": elapsed,
        "rto_target_seconds": RTO_TARGET_SECONDS,
        "within_rto_target": elapsed <= RTO_TARGET_SECONDS,
        "verified_members": [c["label"] for c in checked],
    }
    try:
        target = _resolve_target_dir(cfg)
        report_path = target / DRILL_REPORT_NAME
        report_tmp = target / (_INCOMING_PREFIX + DRILL_REPORT_NAME)
        report_tmp.write_text(json.dumps(report, indent=2), encoding="utf-8")
        os.replace(str(report_tmp), str(report_path))
    except (OffsiteError, BackupError, OSError) as exc:
        logger.error("could not persist drill report: %s", exc)
    if not ok:
        raise OffsiteError(failure or "restore drill failed")
    logger.info(
        "restore drill PASS for %s in %.1fs (RTO target %ds)",
        enc_path.name,
        elapsed,
        RTO_TARGET_SECONDS,
    )
    return report


# --------------------------------------------------------------------------- #
# log rotation
# --------------------------------------------------------------------------- #
def rotate_log(
    path: Path, *, max_bytes: int, keep: int, max_age_days: float = 0.0
) -> dict:
    """Rotate ``path`` when it exceeds ``max_bytes`` or ``max_age_days``.

    ``path`` becomes ``path.1``, previous shifts up, the oldest beyond ``keep``
    is deleted. Only the numbered sidecars of this one log are ever touched —
    manifests, alert events and drill reports are not logs and survive every
    rotation.
    """
    path = Path(path)
    try:
        st = os.stat(str(path))
    except OSError:
        return {"rotated": False, "reason": "missing"}
    too_big = st.st_size > max_bytes
    too_old = max_age_days > 0 and (time.time() - st.st_mtime) > max_age_days * 86400
    if not (too_big or too_old):
        return {"rotated": False}
    try:
        oldest = path.with_name("%s.%d" % (path.name, max(1, keep)))
        oldest.unlink()
    except OSError:
        pass
    for index in range(max(1, keep) - 1, 0, -1):
        src = path.with_name("%s.%d" % (path.name, index))
        if src.exists():
            os.replace(str(src), str(path.with_name("%s.%d" % (path.name, index + 1))))
    os.replace(str(path), str(path.with_name(path.name + ".1")))
    return {"rotated": True, "reason": "size" if too_big else "age"}


def rotate_backup_logs(
    cfg: Config, *, max_bytes: int = 5 * 1024 * 1024, keep: int = 5, max_age_days: float = 30.0
) -> List[dict]:
    """Rotate the operational backup logs. Never touches audit evidence."""
    logs = [Path(cfg.db_path.parent) / "backup.log"]
    offsite_log = Path(cfg.offsite_backup_dir) / "offsite-backup.log"
    if _list_safe(offsite_log):
        logs.append(offsite_log)
    return [rotate_log(p, max_bytes=max_bytes, keep=keep, max_age_days=max_age_days) for p in logs]


def _list_safe(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m aistat.backup_offsite",
        description="Encrypted off-site copies and isolated restore drills.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("push", help="publish the newest local generation off-site")
    sub.add_parser("list", help="list off-site bundles")
    p_verify = sub.add_parser("verify", help="decrypt and re-check a bundle")
    p_verify.add_argument("ref", nargs="?", default="latest")
    p_drill = sub.add_parser(
        "drill", help="isolated restore drill with checksums and critical queries"
    )
    p_drill.add_argument("ref", nargs="?", default="latest")
    p_rotate = sub.add_parser("rotate-log", help="rotate an operational log file")
    p_rotate.add_argument("path")
    p_rotate.add_argument("--max-bytes", type=int, default=5 * 1024 * 1024)
    p_rotate.add_argument("--keep", type=int, default=5)
    p_rotate.add_argument("--max-age-days", type=float, default=30.0)
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
        if args.command == "push":
            print(json.dumps(push_offsite(cfg), indent=2))
            return 0
        if args.command == "list":
            for entry in _list_bundles(cfg):
                print(
                    "%s  pushed=%s  enc_sha256=%s  same_device=%s"
                    % (
                        entry.get("generation"),
                        entry.get("pushed_at"),
                        entry.get("enc_sha256"),
                        entry.get("same_device_as_local"),
                    )
                )
            return 0
        if args.command == "verify":
            print(json.dumps(verify_offsite(cfg, args.ref), indent=2))
            return 0
        if args.command == "drill":
            report = restore_drill_offsite(cfg, args.ref)
            print(
                "PASS drill %s rto=%.1fs members=%s"
                % (
                    report["generation"],
                    report["measured_rto_seconds"],
                    ", ".join(report["verified_members"]),
                )
            )
            return 0
        if args.command == "rotate-log":
            print(
                json.dumps(
                    rotate_log(
                        Path(args.path),
                        max_bytes=args.max_bytes,
                        keep=args.keep,
                        max_age_days=args.max_age_days,
                    )
                )
            )
            return 0
    except OffsiteError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
