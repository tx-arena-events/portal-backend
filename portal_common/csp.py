# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Access to the CSP (C3-M2 section 4, C4-M1 section 4): OAuth 2.0 client credentials with
`private_key_jwt` (RFC 7523). `client_id` = `ospId` from the settings, signed with the key in
`OSP_SIGNING_KEY_FILE` (JWK, ES256). The access token is reused until shortly before it expires.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

import httpx
import jwt

from . import http, settings

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
_lock = threading.Lock()
_token: dict[str, tuple[str, float]] = {}


class CspUnavailable(Exception):
    """No answer from the CSP (timeout, connection, token endpoint refused)."""


def signing_key() -> dict:
    path = os.environ.get("OSP_SIGNING_KEY_FILE")
    if not path:
        raise CspUnavailable("OSP_SIGNING_KEY_FILE is not set")
    return json.loads(Path(path).read_text())


def client_assertion(token_url: str) -> str:
    key = signing_key()
    osp = settings.get("ospId")
    now = int(time.time())
    return jwt.encode({"iss": osp, "sub": osp, "aud": token_url, "iat": now, "exp": now + 60, "jti": str(uuid.uuid4())},
                      jwt.PyJWK(key).key, algorithm="ES256", headers={"kid": key.get("kid")})


def access_token(token_url: str | None = None) -> str:
    token_url = token_url or os.environ.get("CSP_TOKEN_URL", "")
    with _lock:
        cached = _token.get(token_url)
        if cached and cached[1] > time.time() + 30:
            return cached[0]
    try:
        r = http.request("POST", token_url, data={"grant_type": "client_credentials", "client_assertion_type": ASSERTION_TYPE,
                                                  "client_assertion": client_assertion(token_url)})
    except httpx.HTTPError as e:
        raise CspUnavailable(f"token endpoint: {type(e).__name__}") from e
    if r.status_code != 200:
        raise CspUnavailable(f"token endpoint answered {r.status_code}")
    body = r.json()
    with _lock:
        _token[token_url] = (body["access_token"], time.time() + int(body.get("expires_in", 300)))
    return body["access_token"]


def request(base_url: str, method: str, path: str, json_body: object = None, params: dict | None = None,
            headers: dict | None = None) -> tuple[int, object]:
    """Call of a CSP API (core services, issuer, wallet). Returns (status, JSON body or None)."""
    try:
        r = http.request(method, base_url.rstrip("/") + path, json=json_body, params=params,
                         headers={"Authorization": "Bearer " + access_token(), **(headers or {})})
    except httpx.HTTPError as e:
        raise CspUnavailable(type(e).__name__) from e
    try:
        body = r.json() if r.content else None
    except ValueError:
        body = None
    return r.status_code, body


def reset() -> None:
    with _lock:
        _token.clear()
