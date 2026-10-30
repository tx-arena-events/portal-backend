# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Test double of the C5-M1 EBW-Connector for the verification attestation (C5-M1 14.5).

- `sign(company)` returns a verification attestation (JWS, ES256, `typ` `ebw-attestation+jwt`) for a
  company with Business Wallet of `testdata/applicant-companies.json`, with PID and mandate of the
  given person and `client_id` of the client that fetched it.
- Clients as registered at the EBW-Connector (`CLIENTS_FILE`, C5-M1 6.8, format of 14.2): per
  installation the client of its UI, `osp-a-ui` or `osp-b-ui` (FRAMEWORK 7a). Without
  `CLIENTS_FILE` only `demo-osp` (C5-M1 14.2). A relative path counts from the repository root.
- Mounted with prefix `/ebw`: `GET /ebw/jwks` (path of C5-M1 `openapi.yaml`) and, test only,
  `POST /ebw/test/attestations` `{company, givenName, familyName, clientId}` -> `{attestation}`;
  without `clientId` the first registered client.
The installations set `ATTESTATION_JWKS_URL` (C1-M2) to `<BASE_URL>/ebw/jwks` and
`EBW_CONNECTOR_URL` (C3-M1) to `<BASE_URL>/ebw`; `ATTESTATION_ISSUER` is `ISSUER`.
"""
from __future__ import annotations

import json
import os
import time
import uuid

import jwt
from fastapi import APIRouter, Body, HTTPException

from portal_common.spec import ROOT
from tools.keys import ec_jwk

ISSUER = "https://ebw-connector.example.org"
AUDIENCE = "portal-registration"
COMPANIES = {k: c for k, c in json.loads((ROOT / "testdata" / "applicant-companies.json")
                                         .read_text(encoding="utf-8"))["companies"].items() if c.get("ebwoid")}

_key: tuple[dict, dict] | None = None


def key() -> tuple[dict, dict]:
    global _key
    if _key is None:
        _key = ec_jwk("ebw-test-1")
    return _key


def jwks() -> dict:
    return {"keys": [key()[1]]}


def client_ids() -> list[str]:
    path = os.environ.get("CLIENTS_FILE")
    if not path:
        return ["demo-osp"]
    file = ROOT / path   # an absolute path stays absolute
    return [c["clientId"] for c in json.loads(file.read_text(encoding="utf-8"))]


def sign(company: str = "maschinenbau", given_name: str = "Erika", family_name: str = "Mustermann",
         client_id: str = "demo-osp", **over) -> str:
    c = COMPANIES[company]
    now = int(time.time())
    payload = {
        "iss": ISSUER, "sub": c["ebwoid"], "aud": AUDIENCE, "attestation_version": "0.2", "purpose": "onboarding",
        "iat": now - 60, "exp": now + 29 * 86400, "verified_at": now - 60, "jti": str(uuid.uuid4()),
        "client_id": client_id, "person": {"given_name": given_name, "family_name": family_name},
        "company": {"legal_name": c["legalName"], "ebwoid": c["ebwoid"], "euid": c["euid"], "address": c["address"],
                    "country": c["address"]["country"]},
        "representation": {"confirmed": True, "result": "CONFIRMED", "type": "legal_representative"},
        "evidence": [   # C5-M1 / C1-M2 14.4
            {"type": "PID", "vct": "urn:eudi:pid:1", "issuer": "https://pid.test.example.org",
             "wallet": "EUDI_WALLET", "status_checked": False},
            {"type": "MANDATE", "vct": "urn:tx:test:mandate:1", "issuer": "https://register.test.example.org",
             "wallet": "EUDI_WALLET", "status_checked": True},
            {"type": "LPID", "vct": "urn:tx:test:lpid:1", "issuer": "https://register.test.example.org",
             "wallet": "BUSINESS_WALLET", "status_checked": False}],
        **over,
    }
    priv = key()[0]
    return jwt.encode(payload, jwt.PyJWK(priv).key, algorithm="ES256",
                      headers={"typ": "ebw-attestation+jwt", "kid": priv["kid"]})


def reset() -> None:
    global _key
    _key = None


router = APIRouter(prefix="/ebw", tags=["EBW-Connector test double (C5-M1)"])


@router.get("/jwks")
def get_jwks():
    return jwks()


@router.post("/test/attestations")
def new_attestation(body: dict = Body(default={})):
    company = body.get("company", "maschinenbau")
    if company not in COMPANIES:
        raise HTTPException(404, "unknown test company or company without Business Wallet")
    clients = client_ids()
    client = body.get("clientId") or clients[0]
    if client not in clients:
        raise HTTPException(400, f"client {client!r} is not registered (CLIENTS_FILE)")
    return {"attestation": sign(company, body.get("givenName", "Erika"), body.get("familyName", "Mustermann"), client)}
