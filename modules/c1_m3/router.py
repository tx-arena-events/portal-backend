# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C1-M3 Application Review: review case per application, duplicate check at the CSP
through C3-M2 `find-companies`, approve and decline by OSP staff. In memory.
"""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import Body

from portal_common import events, neighbours, standin
from portal_common.problems import Problem

STANDIN = True
MODULE = "c1-m3"
router = standin.router(MODULE)
cases: dict[str, dict] = {}


def reset() -> None:
    cases.clear()


def _case(case_id: str) -> dict:
    if case_id not in cases:
        raise Problem(MODULE, 404, "not-found")
    return cases[case_id]


@router.post("/steps/open-review")
def open_review(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/open-review")):
    aid = body.get("applicationId")
    case = next((c for c in cases.values() if c["applicationId"] == aid), None)
    if case is None:
        app = neighbours.get_json(MODULE, "c1-m2", f"/internal/applications/{aid}")
        if app is None:
            raise Problem(MODULE, 404, "not-found", detail="application unknown", retryable=False)
        ids = {i["type"]: i["value"] for i in app["companyData"]["identifiers"]}
        found = neighbours.get_json(MODULE, "c3-m2", "/internal/find-companies",
                                    params={"ebwoid": ids.get("EBWOID"), "euid": ids.get("EUID")}) or []
        found = found.get("items", []) if isinstance(found, dict) else found
        case = {"id": str(uuid.uuid4()), "applicationId": aid, "companyId": app["companyId"], "kind": app["kind"],
                "existingCompany": app["existingCompany"], "applicantSub": app["applicant"]["sub"], "status": "OPEN",
                "legalName": app["companyData"]["legalName"],
                "checks": [{"checkKey": "DUPLICATE_AT_CSP", "result": "FAILED" if found else "PASSED",
                            "details": [f["bpn"] for f in found]}],
                "openedAt": dt.datetime.now(dt.UTC).isoformat()}
        cases[case["id"]] = case
    return standin.step_reply(body, {"reviewCaseId": case["id"]})


@router.post("/steps/close-review")
def close_review(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/close-review")):
    case = next((c for c in cases.values() if c["applicationId"] == body.get("applicationId")), None)
    if case and case["status"] == "APPROVED":
        raise Problem(MODULE, 409, "invalid-state", retryable=False)
    if case:
        case["status"] = "CLOSED"
    return standin.step_reply(body)


@router.get("/review-cases")
def list_cases(_: dict = standin.perm(MODULE, "GET", "/review-cases"), page: int = 0, size: int = 20):
    items = sorted(cases.values(), key=lambda c: c["openedAt"], reverse=True)
    return {"items": items[page * size:(page + 1) * size], "page": page, "size": size, "total": len(items)}


@router.get("/review-cases/{reviewCaseId}")
def get_case(reviewCaseId: str, _: dict = standin.perm(MODULE, "GET", "/review-cases/{reviewCaseId}")):
    return _case(reviewCaseId)


def _decide(case_id: str, claims: dict, outcome: str, reason: str | None = None) -> dict:
    case = _case(case_id)
    if case["status"] != "OPEN":
        raise Problem(MODULE, 409, "invalid-state")
    if case["applicantSub"] == claims["sub"]:
        raise Problem(MODULE, 403, "own-application")
    case["status"], case["decidedBy"] = outcome, claims["sub"]
    ev = {"applicationId": case["applicationId"], "companyId": case["companyId"], "kind": case["kind"],
          "existingCompany": case["existingCompany"]}
    if outcome == "APPROVED":
        events.publish({"eventType": "ApplicationApproved", **ev})
    else:
        events.publish({"eventType": "ApplicationDeclined", **ev, "reason": reason or "declined"})
    return case


@router.post("/review-cases/{reviewCaseId}/approve")
def approve(reviewCaseId: str, claims: dict = standin.perm(MODULE, "POST", "/review-cases/{reviewCaseId}/approve")):
    return _decide(reviewCaseId, claims, "APPROVED")


@router.post("/review-cases/{reviewCaseId}/decline")
def decline(reviewCaseId: str, body: dict = Body(default={}),
            claims: dict = standin.perm(MODULE, "POST", "/review-cases/{reviewCaseId}/decline")):
    return _decide(reviewCaseId, claims, "DECLINED", body.get("reason"))
