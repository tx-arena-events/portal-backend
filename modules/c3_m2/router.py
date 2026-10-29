# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C3-M2 CSP Connection: the steps of the onboarding and of connectors, the query
`find-companies` and the callback `/csp-notifications`, against the CSP at `CSP_URL` (shared CSP
stand-in or a real CSP) or, with an empty `CSP_URL`, against the built-in stand-in (11.3).
"""
from __future__ import annotations

import os

from fastapi import Body, Query

from portal_common import csp, events, neighbours, settings, standin
from portal_common.problems import Problem
from tools.csp_standin.core import CoreServices

STANDIN = True
MODULE = "c3-m2"
router = standin.router(MODULE)
registrations: dict[str, dict] = {}     # companyId -> {bpn, status, operationId}
seen_notifications: set[str] = set()
_builtin: CoreServices | None = None


def reset() -> None:
    global _builtin
    registrations.clear()
    seen_notifications.clear()
    _builtin = None


def call_csp(method: str, path: str, body: dict | None = None, params: dict | None = None) -> tuple[int, object]:
    """One call of the core services API (11.1); built-in stand-in with an empty `CSP_URL`."""
    global _builtin
    base = os.environ.get("CSP_URL", "")
    if not base:
        if _builtin is None:
            _builtin = CoreServices({settings.get("ospId"): {"name": settings.get("ospName")}})
        return _builtin.dispatch(settings.get("ospId"), method, path, body, params)
    try:
        return csp.request(base, method, path, body, params)
    except csp.CspUnavailable as e:
        raise Problem(MODULE, 502, "upstream-error", detail=str(e), retryable=True) from e


def _bpn_of(company_id: str) -> str:
    reg = registrations.get(company_id)
    if reg and reg.get("bpn"):
        return reg["bpn"]
    company = neighbours.get_json(MODULE, "c3-m1", f"/internal/companies/{company_id}")
    if not company or not company.get("bpn"):
        raise Problem(MODULE, 409, "invalid-state", detail="company has no BPN", retryable=False)
    return company["bpn"]


def _check(status: int, body: object, ok=(200, 201, 204)) -> None:
    if status in ok:
        return
    retryable = status >= 500 or status == 401
    name = "upstream-error" if retryable else "csp-rejected"
    raise Problem(MODULE, 502 if retryable else 409, name, detail=f"CSP answered {status}",
                  cspType=(body or {}).get("type") if isinstance(body, dict) else None, retryable=retryable)


def _registered(company_id: str, bpn: str) -> None:
    registrations[company_id] = {"bpn": bpn, "status": "REGISTERED"}
    events.publish({"eventType": "CompanyRegisteredAtCsp", "companyId": company_id, "bpn": bpn})


@router.post("/steps/register-company")
def register_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-company")):
    cid = body.get("companyId")
    reg = registrations.get(cid)
    if reg and reg["status"] == "REGISTERED":
        return standin.step_reply(body, {"bpn": reg["bpn"]})
    if reg and reg["status"] == "PENDING":
        return standin.step_reply(body, state="RUNNING")
    payload = {k: body[k] for k in ("legalName", "country", "ebwoid", "euid", "address") if body.get(k)}
    payload["identifiers"] = [{"type": i["type"], "value": i["value"]} for i in body.get("identifiers") or []]
    status, answer = call_csp("POST", "/companies", payload)
    if status == 201:
        _registered(cid, answer["bpn"])
        return standin.step_reply(body, {"bpn": answer["bpn"]})
    if status == 202:
        registrations[cid] = {"status": "PENDING", "operationId": answer["operationId"]}
        return standin.step_reply(body, state="RUNNING")
    if status == 409:
        registrations[cid] = {"status": "FAILED", "reason": "REGISTERED_AT_OTHER_OSP"}
        events.publish({"eventType": "CompanyRegistrationAtCspFailed", "companyId": cid,
                        "reason": "REGISTERED_AT_OTHER_OSP"})
        raise Problem(MODULE, 409, "registered-at-other-osp", retryable=False)
    _check(status, answer)


@router.post("/steps/register-did")
def register_did(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-did")):
    cid, did = body.get("companyId"), body.get("did")
    if not did:
        raise Problem(MODULE, 400, "validation", detail="did", retryable=False)
    bpn = _bpn_of(cid)
    _check(*call_csp("PUT", f"/companies/{bpn}/did", {"did": did}))
    events.publish({"eventType": "CompanyDidRegistered", "companyId": cid, "bpn": bpn, "did": did})
    return standin.step_reply(body, {"bpn": bpn, "did": did})


def _membership(body: dict, member: bool) -> dict:
    cid = body.get("companyId")
    bpn = _bpn_of(cid)
    _check(*call_csp("PUT", f"/companies/{bpn}/membership", {"member": member}))
    events.publish({"eventType": "MembershipRegistered" if member else "MembershipDeregistered", "companyId": cid,
                    "bpn": bpn})
    return standin.step_reply(body, {"bpn": bpn})


@router.post("/steps/register-membership")
def register_membership(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-membership")):
    return _membership(body, True)


@router.post("/steps/deregister-membership")
def deregister_membership(body: dict = Body(...),
                          _: dict = standin.perm(MODULE, "POST", "/steps/deregister-membership")):
    return _membership(body, False)


@router.post("/steps/register-connector")
def register_connector(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/register-connector")):
    cid, con = body.get("companyId"), body.get("connectorId")
    bpn = _bpn_of(cid)
    _check(*call_csp("PUT", f"/companies/{bpn}/connectors/{con}", {"endpointUrl": body.get("endpointUrl")}))
    events.publish({"eventType": "ConnectorRegisteredAtCsp", "companyId": cid, "bpn": bpn, "connectorId": con})
    return standin.step_reply(body, {"bpn": bpn})


@router.post("/steps/deregister-connector")
def deregister_connector(body: dict = Body(...),
                         _: dict = standin.perm(MODULE, "POST", "/steps/deregister-connector")):
    cid, con = body.get("companyId"), body.get("connectorId")
    bpn = _bpn_of(cid)
    _check(*call_csp("DELETE", f"/companies/{bpn}/connectors/{con}"))
    events.publish({"eventType": "ConnectorDeregisteredAtCsp", "companyId": cid, "bpn": bpn, "connectorId": con})
    return standin.step_reply(body, {"bpn": bpn})


@router.get("/internal/find-companies")
def find_companies(_: dict = standin.perm(MODULE, "GET", "/internal/find-companies"), ebwoid: str | None = None,
                   euid: str | None = None, identifier: list[str] = Query(default=[]), legalName: str | None = None,
                   country: str | None = None):
    params = {k: v for k, v in {"ebwoid": ebwoid, "euid": euid, "identifier": identifier or None,
                                "legalName": legalName, "country": country}.items() if v}
    status, answer = call_csp("GET", "/companies", params=params)
    if status == 400:
        raise Problem(MODULE, 400, "validation")
    _check(status, answer)
    return answer


@router.post("/csp-notifications")
def csp_notification(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/csp-notifications")):
    nid = body.get("notificationId")
    if nid in seen_notifications:
        return {"accepted": True}
    seen_notifications.add(nid)
    if body.get("type") == "OperationCompleted":
        cid = next((c for c, r in registrations.items() if r.get("operationId") == body.get("operationId")), None)
        if cid and body.get("outcome") == "SUCCEEDED":
            _registered(cid, body["bpn"])
        elif cid:
            registrations[cid] = {"status": "FAILED", "reason": body.get("reason")}
            events.publish({"eventType": "CompanyRegistrationAtCspFailed", "companyId": cid, "reason": body.get("reason")})
    elif body.get("type") == "CompanyTransferredAway":
        cid = next((c for c, r in registrations.items() if r.get("bpn") == body.get("bpn")), None)
        if cid:
            events.publish({"eventType": "CompanyTransferredAway", "companyId": cid, "bpn": body["bpn"]})
    return {"accepted": True}
