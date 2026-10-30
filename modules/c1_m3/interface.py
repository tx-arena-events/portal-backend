# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C1-M3 stand-in: the steps `open-review` and `close-review` and the query
`getReviewStatusByApplication`. Of the checks of 6.2 the stand-in runs `NO_DUPLICATE` (C3-M1
`findIdentifierMatches`) and `CSP_MATCH_EXPECTED` for `ONBOARDING` (C3-M2 `findCompanies`, 6.3)."""
from __future__ import annotations

import datetime as dt
import os
import uuid

from portal_common import neighbours, settings, standin
from portal_common.problems import Problem

MODULE = "c1-m3"
cases: dict[str, dict] = {}


def reset() -> None:
    cases.clear()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _check(key: str, result: str, **details) -> dict:
    return {"checkKey": key, "result": result, "details": details, "checkedAt": _now(), "attempts": 1}


def _no_duplicate(app: dict) -> dict:
    types = [t.strip() for t in os.environ.get("DUPLICATE_CHECK_IDENTIFIER_TYPES", "EUID,EBWOID,VAT_ID,LEI_CODE").split(",")]
    ids = [{"type": i["type"], "value": i["value"]} for i in app["companyData"]["identifiers"] if i["type"] in types]
    if not ids:
        return _check("NO_DUPLICATE", "PASSED", duplicates=[])
    found = neighbours.query(MODULE, "c3-m1", "find_identifier_matches", identifiers=ids,
                             excludeCompanyId=app["companyId"], statuses=["REQUESTED", "ACTIVE", "INACTIVE"]) or {}
    dups = [{"companyId": m["companyId"], "companyStatus": m["status"], "matchedOn": m["matchedOn"]}
            for m in found.get("matches", [])]
    return _check("NO_DUPLICATE", "FAILED" if dups else "PASSED", duplicates=dups)


def _csp_match(app: dict) -> dict:
    """6.3 for ONBOARDING only (the stand-in has no TRANSFER and REACTIVATION)."""
    ids = {i["type"]: i["value"] for i in app["companyData"]["identifiers"]}
    found = neighbours.query(MODULE, "c3-m2", "find_companies", ebwoid=ids.get("EBWOID"), euid=ids.get("EUID"),
                             legalName=app["companyData"]["legalName"],
                             country=app["companyData"]["address"].get("countryCode")) or []
    found = found.get("items", []) if isinstance(found, dict) else found
    merged: dict[str, dict] = {}
    for f in found:
        m = merged.setdefault(f["bpn"], {k: f.get(k) for k in ("bpn", "homeOspId", "homeOspName", "memberActive")}
                              | {"homeIsThisOsp": f.get("homeOspId") == settings.get("ospId"), "matchedBy": [],
                                 "expected": False})
        m["matchedBy"].append(f.get("matchedBy"))
    matches = list(merged.values())
    outcome = "NO_MATCH" if not matches else "REGISTERED_HERE" if any(m["homeIsThisOsp"] for m in matches) \
        else "REGISTERED_AT_OTHER_OSP"
    return _check("CSP_MATCH_EXPECTED", "PASSED" if outcome == "NO_MATCH" else "FAILED",
                  cspOutcome=outcome, cspMatches=matches)


def open_review(input: dict) -> dict:
    aid = input.get("applicationId")
    case = next((c for c in cases.values() if c["applicationId"] == aid), None)
    if case is None:
        app = neighbours.query(MODULE, "c1-m2", "get_application_internal", applicationId=aid)
        if app is None:
            raise Problem(MODULE, 404, "not-found", detail="application unknown", retryable=False)
        case = {"id": str(uuid.uuid4()), "applicationId": aid, "companyId": app["companyId"], "kind": app["kind"],
                "existingCompany": app["existingCompany"], "verification": app["verification"],
                "applicantSub": app["applicant"]["sub"], "status": "OPEN", "openedAt": _now(), "decidedAt": None,
                "cspBpn": None, "checks": [_no_duplicate(app), _csp_match(app)], "firstApproval": None,
                "decision": None, "closure": None}
        cases[case["id"]] = case
    return standin.step_reply(input, {"reviewCaseId": case["id"]})


def close_review(input: dict) -> dict:
    case = next((c for c in cases.values() if c["applicationId"] == input.get("applicationId")), None)
    if case and case["status"] == "APPROVED":
        raise Problem(MODULE, 409, "invalid-state", retryable=False)
    if case and case["status"] != "CLOSED":
        case["status"], case["closure"] = "CLOSED", {"closedAt": _now(), "reason": "WITHDRAWN"}
    return standin.step_reply(input)


def get_review_status_by_application(*, applicationId: str) -> dict:
    case = next((c for c in cases.values() if c["applicationId"] == applicationId), None)
    if case is None:
        raise Problem(MODULE, 404, "not-found")
    return {"reviewCaseId": case["id"], "applicationId": applicationId, "status": case["status"],
            "decidedAt": case["decidedAt"]}


def summary(case: dict) -> dict:
    """`ReviewCaseSummary` of the list (7.1)."""
    counts = {"passed": "PASSED", "failed": "FAILED", "notApplicable": "NOT_APPLICABLE", "error": "ERROR"}
    return {k: case[k] for k in ("id", "applicationId", "companyId", "kind", "verification", "status", "openedAt",
                                 "decidedAt")} \
        | {"checkCounts": {k: sum(c["result"] == v for c in case["checks"]) for k, v in counts.items()}}


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
