"""Worker-side client of the "connect your Multica" token handoff channel.

Runs on the trusted local machine only. Each cycle pulls pending user
connections from the public host over the signed worker channel, stores the
tokens encrypted at rest, deletes locally revoked ones, and acknowledges every
action so the host can physically erase its temporary token copies.

Token values are never logged and never appear in printed summaries.
"""

import argparse
import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from . import handoff
from .config import Config
from .worker_store import WorkerStoreError, WorkerTokenStore

logger = logging.getLogger("aistat.worker_sync")
Opener = Callable[..., Any]

MAX_RESPONSE_BYTES = 1024 * 1024


class WorkerSyncError(RuntimeError):
    """Raised when the worker cannot complete a handoff cycle."""


def _validate_worker_sync_config(config: Config) -> None:
    if not config.worker_sync_url:
        raise WorkerSyncError("AISTAT_WORKER_SYNC_URL is not configured")
    parsed = urlsplit(config.worker_sync_url)
    if parsed.scheme != "https" and not config.allow_insecure_publish:
        raise WorkerSyncError(
            "AISTAT_WORKER_SYNC_URL must use HTTPS "
            "(set AISTAT_ALLOW_INSECURE_PUBLISH=1 only for local tests)"
        )
    if not parsed.netloc:
        raise WorkerSyncError("AISTAT_WORKER_SYNC_URL is invalid")
    secret = config.worker_secret or ""
    if len(secret.encode("utf-8")) < 32:
        raise WorkerSyncError(
            "AISTAT_WORKER_SECRET must contain at least 32 bytes"
        )
    if config.ingest_secret and secret == config.ingest_secret:
        raise WorkerSyncError(
            "the worker secret must be independent from the ingest secret"
        )
    if config.worker_pull_interval_seconds < 60:
        raise WorkerSyncError(
            "AISTAT_WORKER_PULL_INTERVAL_SECONDS must be at least 60"
        )


def _call(
    config: Config,
    opener: Opener,
    path: str,
    payload: Dict[str, Any],
    now: Optional[int] = None,
) -> Dict[str, Any]:
    body = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    timestamp = int(time.time()) if now is None else int(now)
    nonce = secrets.token_urlsafe(24)
    request = urllib.request.Request(
        config.worker_sync_url.rstrip("/") + path,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-AIStat-Timestamp": str(timestamp),
            "X-AIStat-Nonce": nonce,
            "X-AIStat-Signature": handoff.worker_signature(
                config.worker_secret, path, timestamp, nonce, body
            ),
            "User-Agent": "AIStat-Worker/1",
        },
    )
    try:
        with opener(request, timeout=config.publish_timeout_seconds) as response:
            raw = response.read(MAX_RESPONSE_BYTES)
            status = getattr(response, "status", response.getcode())
    except urllib.error.HTTPError as exc:
        raise WorkerSyncError(
            f"host rejected worker request with HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise WorkerSyncError(f"cannot reach host: {exc.reason}") from exc
    if status < 200 or status >= 300:
        raise WorkerSyncError(f"host returned unexpected HTTP {status}")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerSyncError("host returned an invalid response") from exc
    if not isinstance(result, dict):
        raise WorkerSyncError("host returned an invalid response")
    return result


def pull_once(
    config: Config, opener: Opener = urllib.request.urlopen
) -> Dict[str, Any]:
    """One handoff cycle: pull -> store/delete locally -> acknowledge."""
    _validate_worker_sync_config(config)
    store = WorkerTokenStore(config.worker_store_path, config.worker_key_path)
    state = _call(config, opener, handoff.WORKER_PULL_PATH, {})
    acks = []
    stored = 0
    for item in state.get("pending") or []:
        try:
            user_id = int(item["user_id"])
            accepted = store.store_token(
                user_id,
                str(item["server_url"]),
                item.get("workspace_label"),
                str(item["token"]),
                int(item["token_epoch"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerSyncError("host sent an invalid pending entry") from exc
        if not accepted:
            logger.warning(
                "ignored stale encrypted-token update for user %s", user_id
            )
            continue
        acks.append(
            {
                "user_id": user_id,
                "token_epoch": int(item["token_epoch"]),
                "lease_id": item.get("lease_id"),
                "result": "stored",
            }
        )
        stored += 1
        logger.info("stored encrypted token for user %s", user_id)
    revoked = 0
    for item in state.get("revoked") or []:
        try:
            user_id = int(item["user_id"])
            epoch = int(item["token_epoch"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerSyncError("host sent an invalid revoked entry") from exc
        accepted = store.delete_connection(user_id, epoch)
        if not accepted:
            logger.warning(
                "ignored stale encrypted-token revoke for user %s", user_id
            )
            continue
        acks.append(
            {"user_id": user_id, "token_epoch": epoch, "result": "revoked"}
        )
        revoked += 1
        logger.info("deleted local token for revoked user %s", user_id)
    results = []
    if acks:
        results = _call(
            config, opener, handoff.WORKER_ACK_PATH, {"acks": acks}
        ).get("results") or []
        for entry in results:
            if not entry.get("ok"):
                logger.warning(
                    "host did not accept ack for user %s: %s",
                    entry.get("user_id"),
                    entry.get("reason"),
                )
    return {"stored": stored, "revoked": revoked, "results": results}


def report_sync(
    config: Config,
    user_id: int,
    token_epoch: int,
    ok: bool,
    error: Optional[str] = None,
    opener: Opener = urllib.request.urlopen,
) -> Dict[str, Any]:
    """Report one connection's collection outcome for the user's cabinet."""
    _validate_worker_sync_config(config)
    ack = {
        "user_id": int(user_id),
        "token_epoch": int(token_epoch),
        "result": "sync_ok" if ok else "sync_error",
    }
    if not ok:
        ack["error"] = (error or "sync failed")[: handoff.MAX_SYNC_ERROR_LENGTH]
    results = _call(
        config, opener, handoff.WORKER_ACK_PATH, {"acks": [ack]}
    ).get("results") or []
    return results[0] if results else {"ok": False, "reason": "no-response"}


def watch(config: Config) -> int:
    _validate_worker_sync_config(config)
    while True:
        try:
            summary = pull_once(config)
            if summary["stored"] or summary["revoked"]:
                logger.info(
                    "handoff cycle done: stored=%s revoked=%s",
                    summary["stored"],
                    summary["revoked"],
                )
        except (WorkerSyncError, WorkerStoreError) as exc:
            logger.error("handoff cycle failed: %s", exc)
        time.sleep(config.worker_pull_interval_seconds)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull user Multica tokens into the encrypted worker store"
    )
    parser.add_argument(
        "--watch", action="store_true", help="run continuously"
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
        summary = pull_once(config)
    except (WorkerSyncError, WorkerStoreError, KeyboardInterrupt) as exc:
        if isinstance(exc, KeyboardInterrupt):
            return 0
        logger.error("%s", exc)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
