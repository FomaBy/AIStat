"""Publish a signed AIStat data snapshot to the protected public host.

The uploader runs on the trusted Multica machine. It never sends the Multica
token or CLI configuration: only a coherent, gzip-compressed SQLite snapshot
signed with an independent HMAC secret.
"""

import argparse
import gzip
import hashlib
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from .config import Config
from .security import snapshot_signature
from .snapshot import SnapshotError, create_compressed_snapshot

logger = logging.getLogger("aistat.publish")
Opener = Callable[..., Any]


class PublishError(RuntimeError):
    """Raised when a snapshot cannot be built or accepted by the host."""


def _validate_publish_config(config: Config) -> None:
    if not config.publish_url:
        raise PublishError("AISTAT_PUBLISH_URL is not configured")
    parsed = urlsplit(config.publish_url)
    if parsed.scheme != "https" and not config.allow_insecure_publish:
        raise PublishError(
            "AISTAT_PUBLISH_URL must use HTTPS "
            "(set AISTAT_ALLOW_INSECURE_PUBLISH=1 only for local tests)"
        )
    if not config.ingest_secret or len(config.ingest_secret.encode("utf-8")) < 32:
        raise PublishError("AISTAT_INGEST_SECRET must contain at least 32 bytes")
    if config.publish_tenant_id is None:
        raise PublishError("AISTAT_TENANT_ID is not configured")
    if config.publish_interval_seconds < 60:
        raise PublishError("AISTAT_PUBLISH_INTERVAL_SECONDS must be at least 60")


def current_marker(db_path: Path) -> Optional[str]:
    try:
        conn = sqlite3.connect(
            Path(db_path).resolve().as_uri() + "?mode=ro", uri=True
        )
        try:
            beat = conn.execute(
                "SELECT seq, at, phase FROM sync_beats WHERE id = 1"
            ).fetchone()
            cycle = conn.execute(
                "SELECT id, finished_at FROM poll_cycles "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not beat and not cycle:
                return None
            return repr((beat, cycle))
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def publish_once(
    config: Config,
    opener: Opener = urllib.request.urlopen,
    now: Optional[int] = None,
) -> Dict[str, Any]:
    _validate_publish_config(config)
    try:
        payload = create_compressed_snapshot(config.db_path)
    except SnapshotError as exc:
        raise PublishError(str(exc)) from exc
    if len(payload) > config.max_snapshot_bytes:
        raise PublishError("compressed snapshot exceeds the configured limit")
    expected_data = gzip.decompress(payload)
    expected_sha256 = hashlib.sha256(expected_data).hexdigest()
    expected_size = len(expected_data)
    if expected_size > config.max_snapshot_bytes:
        raise PublishError("decompressed snapshot exceeds the configured limit")

    timestamp = int(time.time()) if now is None else int(now)
    signature = snapshot_signature(
        config.ingest_secret, config.publish_tenant_id, timestamp, payload
    )
    request = urllib.request.Request(
        config.publish_url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/vnd.aistat.snapshot+gzip",
            "X-AIStat-Tenant": str(config.publish_tenant_id),
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Signature": signature,
            "User-Agent": "AIStat-Publisher/1",
        },
    )
    try:
        with opener(request, timeout=config.publish_timeout_seconds) as response:
            body = response.read(64 * 1024)
            status = getattr(response, "status", response.getcode())
    except urllib.error.HTTPError as exc:
        raise PublishError(f"host rejected snapshot with HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise PublishError(f"cannot reach public host: {exc.reason}") from exc
    if status < 200 or status >= 300:
        raise PublishError(f"host returned unexpected HTTP {status}")
    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PublishError("host returned an invalid response") from exc
    if result.get("status") != "ok":
        raise PublishError("host did not confirm snapshot installation")
    if (
        result.get("tenant_id") != config.publish_tenant_id
        or result.get("sha256") != expected_sha256
        or result.get("size_bytes") != expected_size
    ):
        raise PublishError("host confirmation does not match the sent snapshot")
    return result


def watch(config: Config) -> int:
    _validate_publish_config(config)
    last_published_marker = None
    while True:
        marker = current_marker(config.db_path)
        if marker and marker != last_published_marker:
            try:
                result = publish_once(config)
                last_published_marker = marker
                logger.info(
                    "snapshot published: schema=%s size=%s sha256=%s",
                    result.get("schema_version"),
                    result.get("size_bytes"),
                    str(result.get("sha256", ""))[:12],
                )
            except PublishError as exc:
                logger.error("snapshot publish failed: %s", exc)
        time.sleep(config.publish_interval_seconds)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Publish AIStat snapshot")
    parser.add_argument(
        "--watch", action="store_true", help="publish changed data continuously"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config()
    try:
        if args.watch:
            return watch(config)
        result = publish_once(config)
    except (PublishError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            return 0
        logger.error("%s", exc)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
