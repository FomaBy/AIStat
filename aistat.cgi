#!/usr/bin/python3
"""Dependency-free CGI entry point for shared hosts without WSGI support."""

from __future__ import print_function

import os
import sys

# Safe after _drop_untrusted_cgi_proxy removes the HTTPoxy input.
from wsgiref.handlers import CGIHandler  # nosec B412


def _load_private_environment():
    home = os.path.expanduser("~")
    env_path = os.environ.get(
        "AISTAT_CGI_ENV_FILE",
        os.path.join(home, "aistat-private", "aistat.env"),
    )
    try:
        with open(env_path, "r") as env_file:
            lines = env_file.readlines()
    except IOError:
        return

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("AISTAT_") and key not in os.environ:
            os.environ[key] = value.strip()


def _drop_untrusted_cgi_proxy():
    # CGI promotes the inbound Proxy header to HTTP_PROXY. Never let Python
    # networking libraries interpret that attacker-controlled value.
    os.environ.pop("HTTP_PROXY", None)


def main():
    home = os.path.expanduser("~")
    app_root = os.environ.get(
        "AISTAT_APP_ROOT",
        os.path.join(home, "aistat_app"),
    )
    if app_root not in sys.path:
        sys.path.insert(0, app_root)

    _drop_untrusted_cgi_proxy()
    _load_private_environment()

    from aistat.legacy_wsgi import application

    # HTTP_PROXY was removed before constructing the CGI environment.
    CGIHandler().run(application)  # nosec B412


if __name__ == "__main__":
    main()
