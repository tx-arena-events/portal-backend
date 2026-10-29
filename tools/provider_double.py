# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Test double of a provider system (C10-M1 11.2): `tokenUrl`, `setupUrl`, `callbackUrl`.

Mounted with prefix `/test-tools/provider`. Answers every call with 200 and records it;
`GET /test-tools/provider/calls` lists the calls (secrets masked).
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Body, Form, Header, HTTPException

CLIENT_ID, CLIENT_SECRET = "portal", "test-only-provider-secret"
calls: list[dict] = []
_tokens: set[str] = set()

router = APIRouter(prefix="/test-tools/provider", tags=["Provider system test double (C10-M1 11.2)"])


def reset() -> None:
    calls.clear()
    _tokens.clear()


def _mask(o):
    if isinstance(o, dict):
        return {k: ("***" if "secret" in k.lower() else _mask(v)) for k, v in o.items()}
    return [_mask(x) for x in o] if isinstance(o, list) else o


def _check(authorization: str | None) -> None:
    if not authorization or authorization.removeprefix("Bearer ") not in _tokens:
        raise HTTPException(401, "unauthorized")


@router.post("/token")
def token(grant_type: str = Form(...), client_id: str = Form(...), client_secret: str = Form(...)):
    if grant_type != "client_credentials" or (client_id, client_secret) != (CLIENT_ID, CLIENT_SECRET):
        raise HTTPException(401, "invalid_client")
    t = secrets.token_urlsafe(16)
    _tokens.add(t)
    return {"access_token": t, "token_type": "Bearer", "expires_in": 300}


@router.post("/setup")
def setup(body: dict = Body(...), authorization: str | None = Header(None)):
    _check(authorization)
    calls.append({"kind": "setup", "body": body})
    return {"received": True}


@router.post("/callback")
def callback(body: dict = Body(...), authorization: str | None = Header(None)):
    _check(authorization)
    calls.append({"kind": "callback", "body": body,
                  "hasSecret": bool((body.get("technicalUser") or {}).get("clientSecret"))})
    return {"received": True}


@router.get("/calls")
def list_calls():
    return [_mask(c) for c in calls]
