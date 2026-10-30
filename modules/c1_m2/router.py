# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C1-M2 Registration: just enough for the onboarding path (FRAMEWORK 5.2).

Implemented: create an application with a verification attestation (checked against the JWKS of
`EBW_CONNECTOR_URL` and `ATTESTATION_CLIENT_IDS`), read it, submit it with consents, the query `ApplicationInternal`, the steps
`complete-application` and `close-application`. Everything else of the spec answers 501.
In memory. Replace this folder with the team's module.
"""
from __future__ import annotations

import datetime as dt
import os
import re
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


def _verify_attestation(token: str) -> dict:
    base = os.environ.get("EBW_CONNECTOR_URL", "").rstrip("/")
    try:
        keys = http.request("GET", f"{base}/jwks").json()["keys"]
        kid = jwt.get_unverified_header(token).get("kid")
        jwk = next(k for k in keys if k.get("kid") == kid)
        claims = jwt.decode(token, jwt.PyJWK(jwk).key, algorithms=["ES256"], audience="portal-registration")
    except Exception as e:  # noqa: BLE001 - every failure is an invalid attestation
        raise Problem(MODULE, 400, "attestation-invalid", detail=type(e).__name__) from e
    allowed = [c.strip() for c in os.environ.get("ATTESTATION_CLIENT_IDS", "").split(",") if c.strip()]
    if allowed and claims.get("client_id") not in allowed:   # C1-M2 6.4 rule 9
        raise Problem(MODULE, 400, "attestation-invalid", reason="client")
    return claims


def _address(a: dict) -> dict:
    """Attestation address (C5-M1) -> C1-M2 Address."""
    m = re.match(r"^(.*?)\s+(\d+\w*)$", a.get("street_address", ""))
    street, number = (m.group(1), m.group(2)) if m else (a.get("street_address"), None)
    return {"streetName": street, "streetNumber": number, "postalCode": a.get("postal_code"), "city": a.get("locality"),
            "countryCode": a.get("country")}


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
    att = _verify_attestation(body["attestation"])
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
         "_attestation": {"jti": att.get("jti"), "verifiedAt": att.get("verified_at"), "issuer": att.get("iss")}}
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
    required = [x for x in testdata.agreements_for_roles(a["companyRoles"])]
    missing = [x["agreementKey"] for x in required if (x["agreementKey"], x["version"]) not in given]
    if missing:
        raise Problem(MODULE, 400, "consent-missing", missing=missing)
    a["consents"] = [{**c, "givenAt": _now()} for c in body["consents"]]
    a["status"] = "SUBMITTED"
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
