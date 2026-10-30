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


def get_application_internal(*, applicationId: str) -> dict:
    a = applications.get(applicationId)
    if not a:
        raise Problem(MODULE, 404, "not-found")
    required = [{"agreementKey": x["agreementKey"], "version": x["version"]}
                for x in testdata.agreements_for_roles(a["companyRoles"])] if a["status"] != "CREATED" else []
    return public(a) | {"attestation": a["_attestation"], "requiredAgreements": required}


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
