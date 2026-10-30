# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C1-M2 Registration: just enough for the onboarding path (FRAMEWORK 5.2).

Implemented: create an application with a verification attestation (6.4: `ATTESTATION_ISSUER`, JWKS
of `ATTESTATION_JWKS_URL`, `ATTESTATION_CLIENT_IDS`, person of the token), read it, submit it with
the consents of 6.8, the queries 9.1 to 9.3, the steps `complete-application` and
`close-application`. Everything else of the spec answers 501.
In memory. Replace this folder with the team's module.
"""
from __future__ import annotations

import datetime as dt
import os
import re
import unicodedata
import uuid

import jwt
from fastapi import Body

from portal_common import events, http, standin
from portal_common.problems import Problem
from tools import testdata

from . import interface
from .interface import MODULE, applications, public

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _norm(name: str | None) -> str:
    """Normalisation of names as in C5-M1 6.5: Unicode NFC, trimmed, case ignored."""
    return unicodedata.normalize("NFC", name or "").strip().casefold()


def _verify_attestation(token: str, claims: dict) -> dict:
    """6.4 in short: issuer, signature, audience, client, person (no age and claim checks)."""
    issuer = os.environ.get("ATTESTATION_ISSUER", "").rstrip("/")
    url = os.environ.get("ATTESTATION_JWKS_URL") or f"{issuer}/jwks"
    try:
        if jwt.decode(token, options={"verify_signature": False}).get("iss", "").rstrip("/") != issuer:
            raise Problem(MODULE, 400, "attestation-invalid", reason="issuer")
        keys = http.request("GET", url).json()["keys"]
        kid = jwt.get_unverified_header(token).get("kid")
        jwk = next(k for k in keys if k.get("kid") == kid)
        att = jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["ES256"], audience="portal-registration")
    except Problem:
        raise
    except Exception as e:  # noqa: BLE001 - every failure is an invalid attestation
        raise Problem(MODULE, 400, "attestation-invalid", detail=type(e).__name__) from e
    allowed = [c.strip() for c in os.environ.get("ATTESTATION_CLIENT_IDS", "").split(",") if c.strip()]
    if allowed and att.get("client_id") not in allowed:   # C1-M2 6.4 rule 9
        raise Problem(MODULE, 400, "attestation-invalid", reason="client")
    person = att.get("person") or {}
    if not claims.get("given_name") or not claims.get("family_name") or \
            (_norm(person.get("given_name")), _norm(person.get("family_name"))) != \
            (_norm(claims["given_name"]), _norm(claims["family_name"])):   # 6.4 rule 10
        raise Problem(MODULE, 400, "attestation-invalid", reason="person")
    return att


def _address(a: dict) -> dict:
    """Attestation address (C5-M1) -> C1-M2 Address."""
    m = re.match(r"^(.*?)\s+(\d+\w*)$", a.get("street_address", ""))
    street, number = (m.group(1), m.group(2)) if m else (a.get("street_address"), None)
    return {"streetName": street, "streetNumber": number, "postalCode": a.get("postal_code"), "city": a.get("locality"),
            "countryCode": a.get("country")}


def _ts(epoch: int | None) -> str | None:
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat() if epoch else None


def _attestation_detail(token: str, att: dict) -> dict:
    """`AttestationDetail` of `ApplicationInternal` (9.1)."""
    rep, company, addr = att.get("representation") or {}, att.get("company") or {}, (att.get("company") or {}).get("address") or {}
    return {"attestationId": att.get("jti"), "issuer": att.get("iss"), "issuedAt": _ts(att.get("iat")),
            "expiresAt": _ts(att.get("exp")), "verifiedAt": _ts(att.get("verified_at")),
            "person": {"givenName": (att.get("person") or {}).get("given_name"),
                       "familyName": (att.get("person") or {}).get("family_name")},
            "representation": {"confirmed": bool(rep.get("confirmed")), "result": rep.get("result"), "type": rep.get("type")},
            "keyId": jwt.get_unverified_header(token).get("kid"), "clientId": att.get("client_id"),
            "company": {"legalName": company.get("legal_name"), "ebwoid": company.get("ebwoid"), "euid": company.get("euid"),
                        "countryCode": company.get("country"),
                        "address": {"streetAddress": addr.get("street_address"), "postalCode": addr.get("postal_code"),
                                    "locality": addr.get("locality"), "country": addr.get("country")}},
            "evidence": [{"type": e.get("type"), "vct": e.get("vct"), "issuer": e.get("issuer"), "wallet": e.get("wallet"),
                          "statusChecked": e.get("status_checked")} for e in att.get("evidence") or []],
            "signatureCheck": {"result": "VALID", "checkedAt": _now()}}


def _own(claims: dict, application_id: str) -> dict:
    a = applications.get(application_id)
    if not a or a["applicant"]["sub"] != claims["sub"]:
        raise Problem(MODULE, 404, "not-found")
    return a


@router.post("/applications", status_code=201)
def create_application(body: dict = Body(...), claims: dict = standin.perm(MODULE, "POST", "/applications")):
    if claims.get("company_id"):
        raise Problem(MODULE, 403, "account-bound")
    if body.get("kind") != "ONBOARDING" or body.get("verification") != "EBW" or not body.get("attestation"):
        raise Problem(MODULE, 400, "validation", detail="stand-in: kind ONBOARDING with verification EBW only")
    att = _verify_attestation(body["attestation"], claims)
    company = att["company"]
    given = body.get("companyData") or {}
    identifiers = [{"type": "EBWOID", "value": company["ebwoid"], "source": "ATTESTATION"},
                   {"type": "EUID", "value": company["euid"], "source": "ATTESTATION"}]
    identifiers += [{**i, "source": "APPLICANT"} for i in given.get("identifiers", [])]
    # MINIMAL: C1-M2 7.2 generates the companyId; the stand-in takes the test ID of FRAMEWORK 7a for a
    # test company once per installation, so that the IDs of 7a appear in the flows. A module of a
    # team may generate a random one; demo.py does not depend on it.
    cid = testdata.test_company_id(company.get("ebwoid"))
    if not cid or any(a["companyId"] == cid for a in applications.values()):
        cid = str(uuid.uuid4())
    aid = str(uuid.uuid4())
    a = {"id": aid, "companyId": cid, "kind": "ONBOARDING", "existingCompany": False, "verification": "EBW",
         "applicant": {"sub": claims["sub"], "givenName": claims.get("given_name"), "familyName": claims.get("family_name")},
         "status": "CREATED", "companyRoles": body.get("companyRoles") or ["ACTIVE_PARTICIPANT"],
         "companyData": {"legalName": company["legal_name"], "shortName": given.get("shortName"),
                         "address": _address(company.get("address") or {}), "identifiers": identifiers},
         "comparison": [], "documents": [], "consents": [], "createdAt": _now(),
         "_attestation": _attestation_detail(body["attestation"], att)}
    applications[aid] = a
    events.publish({"eventType": "ApplicationCreated", "applicationId": aid, "companyId": cid, "kind": a["kind"],
                    "existingCompany": False})
    return public(a)


@router.get("/applications/{applicationId}")
def get_application(applicationId: str, claims: dict = standin.perm(MODULE, "GET", "/applications/{applicationId}")):
    return public(_own(claims, applicationId))


@router.post("/applications/{applicationId}/submit")
def submit(applicationId: str, body: dict = Body(...),
           claims: dict = standin.perm(MODULE, "POST", "/applications/{applicationId}/submit")):
    a = _own(claims, applicationId)
    if a["status"] != "CREATED":
        raise Problem(MODULE, 409, "invalid-state")
    given = {(c.get("agreementKey"), c.get("version")) for c in body.get("consents") or []}
    missing = [x for x in interface.required_agreements(a) if (x["agreementKey"], x["version"]) not in given]
    if missing:   # 6.8, 7.10 rule 5
        raise Problem(MODULE, 400, "consents-incomplete", missing=missing)
    a["consents"] = [{**c, "givenAt": _now()} for c in body["consents"]]
    a["status"], a["submittedAt"] = "SUBMITTED", _now()
    events.publish({"eventType": "ApplicationSubmitted", "applicationId": a["id"], "companyId": a["companyId"],
                    "kind": a["kind"], "existingCompany": a["existingCompany"], "applicantSub": a["applicant"]["sub"]})
    return public(a)


@router.get("/internal/applications/{applicationId}")
def internal_application(applicationId: str,
                         _: dict = standin.perm(MODULE, "GET", "/internal/applications/{applicationId}")):
    return interface.get_application_internal(applicationId=applicationId)


@router.post("/steps/complete-application")
def complete(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/complete-application")):
    return interface.complete_application(body)


@router.post("/steps/close-application")
def close(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/close-application")):
    return interface.close_application(body)
