# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C8-M1 Offers, hackathon core path: a provider creates an offer (company role checked
at C3-M1), sets roles and the technical user profile, submits; OSP staff approve (`OfferApproved`);
`GET /internal/offers/{offerId}` serves the lookup `offer` of Process Orchestration.
"""
from __future__ import annotations

import uuid

from fastapi import Body

from portal_common import events, neighbours, standin
from portal_common.problems import Problem

STANDIN = True
MODULE = "c8-m1"
router = standin.router(MODULE)
offers: dict[str, dict] = {}
ROLE_FOR = {"APP": "APP_PROVIDER", "SERVICE": "SERVICE_PROVIDER"}


def reset() -> None:
    offers.clear()


def _own(claims: dict, offer_id: str) -> dict:
    o = offers.get(offer_id)
    standin.company_scope(MODULE, claims, o and o["providerCompanyId"])
    return o


@router.post("/offers", status_code=201)
def create(body: dict = Body(...), claims: dict = standin.perm(MODULE, "POST", "/offers")):
    cid, kind = claims.get("company_id"), body.get("offerType")
    if kind not in ROLE_FOR or not body.get("name"):
        raise Problem(MODULE, 400, "validation", detail="offerType APP or SERVICE and name")
    company = neighbours.get_json(MODULE, "c3-m1", f"/internal/companies/{cid}") if cid else None
    if not company or ROLE_FOR[kind] not in company.get("companyRoles", []):
        raise Problem(MODULE, 403, "company-role-missing", detail=f"company role {ROLE_FOR[kind]} missing")
    o = {"offerId": str(uuid.uuid4()), "status": "DRAFT", "offerType": kind, "name": body["name"],
         "providerCompanyId": cid, "instanceKind": body.get("instanceKind", "MULTI") if kind == "APP" else None,
         "instanceUrl": body.get("instanceUrl"), "roles": [], "technicalUserProfile": None, "agreements": []}
    offers[o["offerId"]] = o
    return o


@router.put("/offers/{offerId}/roles")
def put_roles(offerId: str, body: list = Body(...), claims: dict = standin.perm(MODULE, "PUT", "/offers/{offerId}/roles")):
    o = _own(claims, offerId)
    o["roles"] = [{"name": r["name"], "descriptions": r.get("descriptions", [])} for r in body]
    return o["roles"]


@router.put("/offers/{offerId}/technical-user-profile")
def put_profile(offerId: str, body: dict = Body(...),
                claims: dict = standin.perm(MODULE, "PUT", "/offers/{offerId}/technical-user-profile")):
    o = _own(claims, offerId)
    if not body.get("name") or not body.get("roles"):
        raise Problem(MODULE, 400, "validation")
    o["technicalUserProfile"] = {"name": body["name"], "roles": [{"name": r["name"]} for r in body["roles"]]}
    return o["technicalUserProfile"]


@router.post("/offers/{offerId}/submit")
def submit(offerId: str, claims: dict = standin.perm(MODULE, "POST", "/offers/{offerId}/submit")):
    o = _own(claims, offerId)
    if o["status"] != "DRAFT":
        raise Problem(MODULE, 409, "invalid-state")
    if o["offerType"] == "APP" and not o["roles"]:
        raise Problem(MODULE, 409, "incomplete", detail="an app needs roles")
    o["status"] = "IN_REVIEW"
    return o


@router.post("/osp/offers/{offerId}/approve")
def approve(offerId: str, _: dict = standin.perm(MODULE, "POST", "/osp/offers/{offerId}/approve")):
    o = offers.get(offerId)
    if not o:
        raise Problem(MODULE, 404, "not-found")
    if o["status"] != "IN_REVIEW":
        raise Problem(MODULE, 409, "invalid-state")
    o["status"] = "ACTIVE"
    events.publish({"eventType": "OfferApproved", "offerId": offerId, "offerType": o["offerType"],
                    "providerCompanyId": o["providerCompanyId"]})
    return o


@router.get("/internal/offers/{offerId}")
def internal_offer(offerId: str, _: dict = standin.perm(MODULE, "GET", "/internal/offers/{offerId}")):
    o = offers.get(offerId)
    if not o:
        raise Problem(MODULE, 404, "not-found")
    return o | {"hasTechnicalUserProfile": o["technicalUserProfile"] is not None}


@router.get("/offers/{offerId}")
def get_own(offerId: str, claims: dict = standin.perm(MODULE, "GET", "/offers/{offerId}")):
    return _own(claims, offerId)
