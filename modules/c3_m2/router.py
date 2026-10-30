# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C3-M2 CSP Connection: the steps of the onboarding and of connectors, the query
`find-companies` and the callback `/csp-notifications`, against the CSP at `CSP_URL` (shared CSP
stand-in or a real CSP) or, with an empty `CSP_URL`, against the built-in stand-in (11.3).
"""
from __future__ import annotations

from fastapi import Body, Query

from portal_common import events, standin

from . import interface
from .interface import MODULE, registered, registrations, seen_notifications

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()



@router.post("/steps/register-company")
def register_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-company")):
    return interface.register_company(body)


@router.post("/steps/register-did")
def register_did(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-did")):
    return interface.register_did(body)


@router.post("/steps/register-membership")
def register_membership(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-membership")):
    return interface.register_membership(body)


@router.post("/steps/deregister-membership")
def deregister_membership(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/deregister-membership")):
    return interface.deregister_membership(body)


@router.post("/steps/register-connector")
def register_connector(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-connector")):
    return interface.register_connector(body)


@router.post("/steps/deregister-connector")
def deregister_connector(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/deregister-connector")):
    return interface.deregister_connector(body)


@router.get("/internal/find-companies")
def find_companies(_: dict = standin.perm(MODULE, "GET", "/internal/find-companies"), ebwoid: str | None = None,
                   euid: str | None = None, identifier: list[str] = Query(default=[]), legalName: str | None = None,
                   country: str | None = None):
    return interface.find_companies(ebwoid=ebwoid, euid=euid, identifier=identifier, legalName=legalName,
                                    country=country)


@router.post("/csp-notifications")
def csp_notification(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/csp-notifications")):
    nid = body.get("notificationId")
    if nid in seen_notifications:
        return {"accepted": True}
    seen_notifications.add(nid)
    if body.get("type") == "OperationCompleted":
        cid = next((c for c, r in registrations.items() if r.get("operationId") == body.get("operationId")), None)
        if cid and body.get("outcome") == "SUCCEEDED":
            registered(cid, body["bpn"])
        elif cid:
            registrations[cid] = {"status": "FAILED", "reason": body.get("reason")}
            events.publish({"eventType": "CompanyRegistrationAtCspFailed", "companyId": cid, "reason": body.get("reason")})
    elif body.get("type") == "CompanyTransferredAway":
        cid = next((c for c, r in registrations.items() if r.get("bpn") == body.get("bpn")), None)
        if cid:
            events.publish({"eventType": "CompanyTransferredAway", "companyId": cid, "bpn": body["bpn"]})
    return {"accepted": True}
