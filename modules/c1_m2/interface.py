# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C1-M2 stand-in: the steps and queries other modules and Process
Orchestration call in the same process. The router calls the same functions over HTTP."""
from __future__ import annotations

from portal_common import events, standin
from portal_common.problems import Problem
from tools import testdata

MODULE = "c1-m2"
applications: dict[str, dict] = {}


def reset() -> None:
    applications.clear()


def public(a: dict) -> dict:
    return {k: v for k, v in a.items() if not k.startswith("_")}


def required_agreements(a: dict) -> list[dict]:
    """Required terms of the application (6.8): scope ALL plus the terms of the company roles."""
    return [{"agreementKey": x["agreementKey"], "version": x["version"]}
            for x in testdata.required_agreements(a["companyRoles"])]


def get_application_internal(*, applicationId: str) -> dict:
    a = applications.get(applicationId)
    if not a:
        raise Problem(MODULE, 404, "not-found")
    required = required_agreements(a) if a["status"] != "CREATED" else []
    return public(a) | {"attestation": a["_attestation"], "requiredAgreements": required}


def list_applications_internal(*, applicationId: list[str] | str | None = None, **_other) -> list[dict]:
    """`ApplicationSummary` (9.2) of the given applications; unknown IDs are left out."""
    ids = [applicationId] if isinstance(applicationId, str) else applicationId or []
    return [{"id": a["id"], "companyId": a["companyId"], "kind": a["kind"], "existingCompany": a["existingCompany"],
             "verification": a["verification"], "status": a["status"], "legalName": a["companyData"]["legalName"],
             "countryCode": a["companyData"]["address"].get("countryCode"), "companyRoles": a["companyRoles"],
             "createdAt": a["createdAt"], "submittedAt": a.get("submittedAt")}
            for i in ids if (a := applications.get(i))]


def list_agreements_internal(*, status: str | None = None, **_other) -> list[dict]:
    """`AgreementVersion` (9.3) from the shared catalogue `testdata/agreements.json`; all versions are ACTIVE."""
    if status not in (None, "ACTIVE"):
        return []
    return [{k: x[k] for k in ("agreementKey", "version", "party", "scope", "companyRoles", "title", "documentUrls")}
            | {"status": "ACTIVE", "reconsentRequired": False, "reconsentDueAt": None,
               "createdAt": "2026-01-01T00:00:00Z", "createdBy": None}
            for x in testdata.agreements()]


def _step_application(input: dict) -> dict:
    a = applications.get(input.get("applicationId"))
    if not a:
        raise Problem(MODULE, 404, "not-found", retryable=False)
    return a


def complete_application(input: dict) -> dict:
    a = _step_application(input)
    if a["status"] != "COMPLETED":
        a["status"] = "COMPLETED"
        events.publish({"eventType": "ApplicationCompleted", "applicationId": a["id"], "companyId": a["companyId"],
                        "kind": a["kind"]})
    return standin.step_reply(input, {"applicationId": a["id"], "status": a["status"]})


def close_application(input: dict) -> dict:
    a = _step_application(input)
    a["status"] = "DECLINED"
    return standin.step_reply(input, {"applicationId": a["id"], "status": a["status"]})


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
