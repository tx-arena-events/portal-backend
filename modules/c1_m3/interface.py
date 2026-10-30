# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C1-M3 stand-in: the steps `open-review` and `close-review`."""
from __future__ import annotations

import datetime as dt
import uuid

from portal_common import neighbours, standin
from portal_common.problems import Problem

MODULE = "c1-m3"
cases: dict[str, dict] = {}


def reset() -> None:
    cases.clear()


def open_review(input: dict) -> dict:
    aid = input.get("applicationId")
    case = next((c for c in cases.values() if c["applicationId"] == aid), None)
    if case is None:
        app = neighbours.query(MODULE, "c1-m2", "get_application_internal", applicationId=aid)
        if app is None:
            raise Problem(MODULE, 404, "not-found", detail="application unknown", retryable=False)
        ids = {i["type"]: i["value"] for i in app["companyData"]["identifiers"]}
        found = neighbours.query(MODULE, "c3-m2", "find_companies", ebwoid=ids.get("EBWOID"), euid=ids.get("EUID")) or []
        found = found.get("items", []) if isinstance(found, dict) else found
        case = {"id": str(uuid.uuid4()), "applicationId": aid, "companyId": app["companyId"], "kind": app["kind"],
                "existingCompany": app["existingCompany"], "applicantSub": app["applicant"]["sub"], "status": "OPEN",
                "legalName": app["companyData"]["legalName"],
                "checks": [{"checkKey": "DUPLICATE_AT_CSP", "result": "FAILED" if found else "PASSED",
                            "details": [f["bpn"] for f in found]}],
                "openedAt": dt.datetime.now(dt.UTC).isoformat()}
        cases[case["id"]] = case
    return standin.step_reply(input, {"reviewCaseId": case["id"]})


def close_review(input: dict) -> dict:
    case = next((c for c in cases.values() if c["applicationId"] == input.get("applicationId")), None)
    if case and case["status"] == "APPROVED":
        raise Problem(MODULE, 409, "invalid-state", retryable=False)
    if case:
        case["status"] = "CLOSED"
    return standin.step_reply(input)


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
