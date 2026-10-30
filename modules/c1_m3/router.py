# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C1-M3 Application Review: review case per application, duplicate check at the CSP
through C3-M2 `find-companies`, approve and decline by OSP staff. In memory.
"""
from __future__ import annotations

from fastapi import Body

from portal_common import events, standin
from portal_common.problems import Problem

from . import interface
from .interface import MODULE, cases

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()


def _case(case_id: str) -> dict:
    if case_id not in cases:
        raise Problem(MODULE, 404, "not-found")
    return cases[case_id]


@router.post("/steps/open-review")
def open_review(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/open-review")):
    return interface.open_review(body)


@router.post("/steps/close-review")
def close_review(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/close-review")):
    return interface.close_review(body)


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
