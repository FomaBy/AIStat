"""Dependency-free tenant identifiers, paths and snapshot signatures.

This module is intentionally Python 3.6-compatible because the legacy cPanel
WSGI entry point imports it without third-party dependencies.
"""

import hashlib
import hmac
import os

MAX_SQLITE_INTEGER = 9223372036854775807
INGEST_SIGNATURE_PREFIX = "v2="


def canonical_tenant_id(value):
    """Return a positive SQLite integer from an internal id or HTTP header.

    String values must already be canonical ASCII decimal representations.
    This rejects aliases, separators and traversal text before a path is ever
    derived from the identifier.
    """
    if isinstance(value, bool):
        raise ValueError("invalid tenant id")
    if isinstance(value, int):
        tenant_id = value
    elif isinstance(value, str):
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("invalid tenant id")
        if not value or not value.isdecimal():
            raise ValueError("invalid tenant id")
        if value != str(int(value)):
            raise ValueError("invalid tenant id")
        tenant_id = int(value)
    else:
        raise ValueError("invalid tenant id")
    if tenant_id <= 0 or tenant_id > MAX_SQLITE_INTEGER:
        raise ValueError("invalid tenant id")
    return tenant_id


def tenant_db_path(tenants_dir, tenant_id):
    """Derive ``<trusted root>/<canonical numeric id>.db``."""
    tenant_id = canonical_tenant_id(tenant_id)
    root = os.path.realpath(os.fspath(tenants_dir))
    candidate = os.path.join(root, "{}.db".format(tenant_id))
    if os.path.dirname(os.path.realpath(candidate)) != root:
        raise ValueError("tenant path escapes configured directory")
    return candidate


def snapshot_signature(secret, tenant_id, timestamp, body):
    """Bind a compressed snapshot to exactly one tenant."""
    tenant_id = canonical_tenant_id(tenant_id)
    timestamp = int(timestamp)
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = "aistat-snapshot-v2\n{}\n{}\n{}".format(
        tenant_id, timestamp, body_digest
    ).encode("ascii")
    digest = hmac.new(
        secret.encode("utf-8"), canonical, hashlib.sha256
    ).hexdigest()
    return INGEST_SIGNATURE_PREFIX + digest
