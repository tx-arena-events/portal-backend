# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""One HTTP entry point for all outgoing calls (neighbours, IAM, CSP, issuers, providers).

An app registered with `register_local(base_url, app)` is called in the same process through
its ASGI interface instead of the network. That is how `in-process` neighbours work
(FRAMEWORK 1, 4.7), and how tests run several systems without opening ports. The request
still carries a token and passes the same routes, checks and Problem Details as over HTTP.
"""
from __future__ import annotations

import warnings

import httpx

with warnings.catch_warnings():   # starlette announces a successor of httpx for its test client
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

DEFAULT_TIMEOUT = 10.0
_local: dict[str, object] = {}


def register_local(base_url: str, app) -> None:
    _local[base_url.rstrip("/")] = app


def unregister_local(base_url: str) -> None:
    _local.pop(base_url.rstrip("/"), None)


def _local_app(url: str):
    for base, app in _local.items():
        if url == base or url.startswith(base + "/"):
            return base, app
    return None, None


def request(method: str, url: str, timeout: float = DEFAULT_TIMEOUT, **kw) -> httpx.Response:
    """Like `httpx.request`; raises `httpx.HTTPError` on timeouts and connection errors."""
    base, app = _local_app(url)
    if app is None:
        return httpx.request(method, url, timeout=timeout, **kw)
    # MINIMAL: one TestClient per call (thread-safe, starts a short-lived event loop thread);
    # upgrade: a pooled ASGI transport if call volume ever matters.
    client = TestClient(app, base_url=base, raise_server_exceptions=False)
    return client.request(method, url[len(base):] or "/", **kw)
