# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Test double of the C5-M1 EBW-Connector for the verification attestation (C5-M1 14.5).

- `sign(company)` returns a verification attestation (JWS, ES256, `typ` `ebw-attestation+jwt`) for a
  company of `testdata/applicant-companies.json`, with PID and mandate of the given person.
- Mounted with prefix `/ebw`: `GET /ebw/jwks` (path of C5-M1 `openapi.yaml`) and, test only,
  `POST /ebw/test/attestations` `{company, givenName, familyName}` -> `{attestation}`.
Set `EBW_CONNECTOR_URL` of the installation to `<BASE_URL>/ebw`.
"""
from __future__ import annotations

import json
import time
import uuid

import jwt
from fastapi import APIRouter, Body, HTTPException

from portal_common.spec import ROOT
from tools.keys import ec_jwk

ISSUER = "https://ebw-connector.example.org"
AUDIENCE = "portal-registration"
COMPANIES = json.loads((ROOT / "testdata" / "applicant-companies.json").read_text(encoding="utf-8"))["companies"]

_key: tuple[dict, dict] | None = None


def key() -> tuple[dict, dict]:
    global _key
    if _key is None:
        _key = ec_jwk("ebw-test-1")
    return _key


def jwks() -> dict:
    return {"keys": [key()[1]]}


def sign(company: str = "maschinenbau", given_name: str = "Erika", family_name: str = "Mustermann", **over) -> str:
    c = COMPANIES[company]
    now = int(time.time())
    payload = {
        "iss": ISSUER, "sub": c["ebwoid"], "aud": AUDIENCE, "attestation_version": "0.2", "purpose": "onboarding",
        "iat": now - 60, "exp": now + 29 * 86400, "verified_at": now - 60, "jti": str(uuid.uuid4()),
        "person": {"given_name": given_name, "family_name": family_name},
        "company": {"legal_name": c["legalName"], "ebwoid": c["ebwoid"], "euid": c["euid"], "address": c["address"],
                    "country": c["address"]["country"]},
        "representation": {"confirmed": True, "result": "CONFIRMED", "type": "legal_representative"},
        "evidence": [{"type": "PID", "wallet": "EUDI_WALLET"}, {"type": "MANDATE", "wallet": "EUDI_WALLET"},
                     {"type": "LPID", "wallet": "BUSINESS_WALLET"}],
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
        raise HTTPException(404, "unknown test company")
    return {"attestation": sign(company, body.get("givenName", "Erika"), body.get("familyName", "Mustermann"))}
