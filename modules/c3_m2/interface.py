# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C3-M2 stand-in: the steps of the onboarding and of connectors and the
query `find-companies`, against the CSP at `CSP_URL` (shared CSP stand-in or a real CSP) or, with
an empty `CSP_URL`, against the built-in stand-in (11.3)."""
from __future__ import annotations

import os

from portal_common import csp, events, neighbours, settings, standin
from portal_common.problems import Problem
from tools.csp_standin.core import CoreServices

MODULE = "c3-m2"
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
    company = neighbours.query(MODULE, "c3-m1", "query_company", companyId=company_id)
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


def registered(company_id: str, bpn: str) -> None:
    registrations[company_id] = {"bpn": bpn, "status": "REGISTERED"}
    events.publish({"eventType": "CompanyRegisteredAtCsp", "companyId": company_id, "bpn": bpn})


def register_company(input: dict) -> dict:
    cid = input.get("companyId")
    reg = registrations.get(cid)
    if reg and reg["status"] == "REGISTERED":
        return standin.step_reply(input, {"bpn": reg["bpn"]})
    if reg and reg["status"] == "PENDING":
        return standin.step_reply(input, state="RUNNING")
    payload = {k: input[k] for k in ("legalName", "country", "ebwoid", "euid", "address") if input.get(k)}
    payload["identifiers"] = [{"type": i["type"], "value": i["value"]} for i in input.get("identifiers") or []]
    status, answer = call_csp("POST", "/companies", payload)
    if status == 201:
        registered(cid, answer["bpn"])
        return standin.step_reply(input, {"bpn": answer["bpn"]})
    if status == 202:
        registrations[cid] = {"status": "PENDING", "operationId": answer["operationId"]}
        return standin.step_reply(input, state="RUNNING")
    if status == 409:
        registrations[cid] = {"status": "FAILED", "reason": "REGISTERED_AT_OTHER_OSP"}
        events.publish({"eventType": "CompanyRegistrationAtCspFailed", "companyId": cid,
                        "reason": "REGISTERED_AT_OTHER_OSP"})
        raise Problem(MODULE, 409, "registered-at-other-osp", retryable=False)
    _check(status, answer)


def register_did(input: dict) -> dict:
    cid, did = input.get("companyId"), input.get("did")
    if not did:
        raise Problem(MODULE, 400, "validation", detail="did", retryable=False)
    bpn = _bpn_of(cid)
    _check(*call_csp("PUT", f"/companies/{bpn}/did", {"did": did}))
    events.publish({"eventType": "CompanyDidRegistered", "companyId": cid, "bpn": bpn, "did": did})
    return standin.step_reply(input, {"bpn": bpn, "did": did})


def _membership(input: dict, member: bool) -> dict:
    cid = input.get("companyId")
    bpn = _bpn_of(cid)
    _check(*call_csp("PUT", f"/companies/{bpn}/membership", {"member": member}))
    events.publish({"eventType": "MembershipRegistered" if member else "MembershipDeregistered", "companyId": cid,
                    "bpn": bpn})
    return standin.step_reply(input, {"bpn": bpn})


def register_membership(input: dict) -> dict:
    return _membership(input, True)


def deregister_membership(input: dict) -> dict:
    return _membership(input, False)


def register_connector(input: dict) -> dict:
    cid, con = input.get("companyId"), input.get("connectorId")
    bpn = _bpn_of(cid)
    _check(*call_csp("PUT", f"/companies/{bpn}/connectors/{con}", {"endpointUrl": input.get("endpointUrl")}))
    events.publish({"eventType": "ConnectorRegisteredAtCsp", "companyId": cid, "bpn": bpn, "connectorId": con})
    return standin.step_reply(input, {"bpn": bpn})


def deregister_connector(input: dict) -> dict:
    cid, con = input.get("companyId"), input.get("connectorId")
    bpn = _bpn_of(cid)
    _check(*call_csp("DELETE", f"/companies/{bpn}/connectors/{con}"))
    events.publish({"eventType": "ConnectorDeregisteredAtCsp", "companyId": cid, "bpn": bpn, "connectorId": con})
    return standin.step_reply(input, {"bpn": bpn})


def find_companies(*, ebwoid: str | None = None, euid: str | None = None, identifier: list[str] | str | None = None,
                   legalName: str | None = None, country: str | None = None):
    params = {k: v for k, v in {"ebwoid": ebwoid, "euid": euid, "identifier": identifier or None,
                                "legalName": legalName, "country": country}.items() if v}
    status, answer = call_csp("GET", "/companies", params=params)
    if status == 400:
        raise Problem(MODULE, 400, "validation")
    _check(status, answer)
    return answer


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
