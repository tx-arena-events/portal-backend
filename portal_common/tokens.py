# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Token validation (FRAMEWORK 4.2, C2-M5 7.6) and service tokens for calls between modules.

Validation: signature against the JWKS of the installation's Portal IAM (RS256), `iss` equal to
`{IAM_URL}/realms/{IAM_REALM}` (or `IAM_ISSUER`), `exp` with 30 s tolerance, `aud` contains
`portal-core`, `typ` = `Bearer`. A token of another installation fails at `iss` -> 401.

`service_token(module)`: access token of the service account `svc-<module>` (client credentials
with `SERVICE_CLIENT_SECRET`), used for steps, queries and lookups over HTTP. The test IAM gives
these accounts every step and query permission of the spec (C2-M5 14.2, `svc-test-internal`).
"""
from __future__ import annotations

import os
import threading
import time

import httpx
import jwt
from fastapi import Header

from . import http, settings, spec
from .problems import Problem

AUDIENCE = "portal-core"
LEEWAY = 30
DEFAULT_SERVICE_SECRET = "test-only-service-secret"   # test installation only (C2-M5 14.1)

_lock = threading.Lock()
_jwks: dict[str, jwt.PyJWK] = {}
_service_tokens: dict[str, tuple[str, float]] = {}


def iam_url() -> str:
    return (os.environ.get("IAM_URL") or settings.base_url() + "/auth").rstrip("/")


def realm() -> str:
    return os.environ.get("IAM_REALM", "CX-Central")


def issuer() -> str:
    return os.environ.get("IAM_ISSUER") or f"{iam_url()}/realms/{realm()}"


def token_endpoint() -> str:
    return f"{iam_url()}/realms/{realm()}/protocol/openid-connect/token"


def _load_jwks() -> None:
    r = http.request("GET", f"{iam_url()}/realms/{realm()}/protocol/openid-connect/certs")
    r.raise_for_status()
    keys = {k["kid"]: jwt.PyJWK(k) for k in r.json().get("keys", []) if k.get("kid")}
    with _lock:
        _jwks.clear()
        _jwks.update(keys)


def _key(kid: str | None) -> jwt.PyJWK:
    if kid not in _jwks:
        _load_jwks()   # unknown kid: one reload (C2-M5 7.6)
    if kid not in _jwks:
        raise jwt.InvalidTokenError("unknown kid")
    return _jwks[kid]


def verify(token: str) -> dict:
    header = jwt.get_unverified_header(token)
    claims = jwt.decode(token, _key(header.get("kid")).key, algorithms=["RS256"], audience=AUDIENCE,
                        issuer=issuer(), leeway=LEEWAY, options={"require": ["exp", "iss", "sub"]})
    if claims.get("typ", "Bearer") != "Bearer":
        raise jwt.InvalidTokenError("typ")
    return claims


def claims_from(authorization: str | None, module: str) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise Problem(module, 401, "unauthorized")
    try:
        return verify(authorization.removeprefix("Bearer "))
    except (jwt.PyJWTError, httpx.HTTPError) as e:
        raise Problem(module, 401, "unauthorized", detail=type(e).__name__) from e


def permissions(claims: dict) -> set[str]:
    return set(claims.get("resource_access", {}).get(AUDIENCE, {}).get("roles", []))


def check(authorization: str | None, module: str, permission: str | None) -> dict:
    c = claims_from(authorization, module)
    if permission and permission not in permissions(c):
        raise Problem(module, 403, "forbidden", detail=f"permission {permission} missing")
    return c


def require(module: str, permission: str):
    """FastAPI dependency: 401 without a valid token, 403 without the permission; returns claims."""
    def dependency(authorization: str | None = Header(default=None)) -> dict:
        return check(authorization, module, permission)
    return dependency


def require_spec(module: str, method: str, path: str):
    """Like `require`, with the permission that the module's openapi.yaml names for this operation
    (`x-permission`), so that a renamed permission in the spec needs no code change."""
    op = spec.module(module).find(method, path)
    if op is None:
        raise KeyError(f"{method} {path} is not in the openapi.yaml of {module}")
    return require(module, op.permission)


def service_token(module: str) -> str:
    """Token of the service account `svc-<module>` of this installation, cached until shortly before expiry."""
    with _lock:
        cached = _service_tokens.get(module)
        if cached and cached[1] > time.time() + 30:
            return cached[0]
    r = http.request("POST", token_endpoint(), data={
        "grant_type": "client_credentials", "client_id": f"svc-{module}",
        "client_secret": os.environ.get("SERVICE_CLIENT_SECRET", DEFAULT_SERVICE_SECRET)})
    if r.status_code != 200:
        raise Problem(module, 502, "upstream-error", detail=f"Portal IAM token endpoint answered {r.status_code}",
                      retryable=True)
    body = r.json()
    with _lock:
        _service_tokens[module] = (body["access_token"], time.time() + int(body.get("expires_in", 300)))
    return body["access_token"]


def reset() -> None:
    with _lock:
        _jwks.clear()
        _service_tokens.clear()
