# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C2-M2 stand-in: `assign-company-admin` (binds the applicant in the test
IAM), client template and instance clients of apps, and the queries on instance clients."""
from __future__ import annotations

from portal_common import events, standin
from portal_common.problems import Problem
from tools import test_iam

MODULE = "c2-m2"
templates: dict[str, dict] = {}          # appId -> template
instance_clients: dict[str, dict] = {}   # id -> instance client


def reset() -> None:
    templates.clear()
    instance_clients.clear()


def assign_company_admin(input: dict) -> dict:
    iam, uid = test_iam.iam(), input.get("applicantSub")
    if uid not in iam.users:
        raise Problem(MODULE, 404, "not-found", detail="account unknown", retryable=False)
    person = iam.person(uid)
    if person["companyId"] not in (None, input.get("companyId")):
        raise Problem(MODULE, 409, "account-bound", retryable=False)
    if person["companyId"] != input.get("companyId") or "Company Admin" not in person["portalRoles"]:
        iam.bind(uid, input.get("companyId"), input.get("bpn"))
        iam.add_portal_roles(uid, ["Company Admin"])
        events.publish({"eventType": "UserAssignedToCompany", "userId": uid, "companyId": input.get("companyId"),
                        "via": "ONBOARDING", "portalRoles": ["Company Admin"]})
    return standin.step_reply(input, {"userId": uid})


def create_client_template(input: dict) -> dict:
    app_id = input.get("appId")
    if not input.get("roles"):
        raise Problem(MODULE, 400, "validation", detail="roles", retryable=False)
    if app_id not in templates:
        templates[app_id] = {"appId": app_id, "clientId": f"app-{standin.stable_id('app', app_id)[:8]}",
                             "roles": [r["name"] for r in input["roles"]], "appName": input.get("appName")}
        events.publish({"eventType": "AppClientTemplateCreated", "appId": app_id})
    return standin.step_reply(input, {"clientId": templates[app_id]["clientId"]})


def create_instance_client(input: dict) -> dict:
    key = (input.get("appId"), input.get("offerSubscriptionId"))
    ic = next((c for c in instance_clients.values() if (c["appId"], c["offerSubscriptionId"]) == key), None)
    if ic is None:
        iid = standin.stable_id("instance-client", *key)
        ic = {"id": iid, "appId": key[0], "offerSubscriptionId": key[1], "customerCompanyId": input.get("customerCompanyId"),
              "instanceKind": input.get("instanceKind"), "instanceUrl": input.get("instanceUrl"),
              "iamClientId": f"app-{iid[:8]}", "status": "INACTIVE"}
        instance_clients[iid] = ic
        events.publish({"eventType": "AppInstanceClientCreated", "appInstanceClientId": iid, "appId": key[0]})
    return standin.step_reply(input, {"id": ic["id"], "iamClientId": ic["iamClientId"]})


def _set_status(input: dict, status: str, event: str) -> dict:
    ic = instance_clients.get(input.get("appInstanceClientId"))
    if ic is None:
        raise Problem(MODULE, 404, "not-found", retryable=False)
    if ic["status"] != status:
        ic["status"] = status
        events.publish({"eventType": event, "appInstanceClientId": ic["id"], "appId": ic["appId"]})
    return standin.step_reply(input, {"id": ic["id"], "status": status})


def enable_instance_client(input: dict) -> dict:
    return _set_status(input, "ACTIVE", "AppInstanceClientEnabled")


def disable_instance_client(input: dict) -> dict:
    return _set_status(input, "INACTIVE", "AppInstanceClientDisabled")


def list_app_instance_clients(*, appId: str | None = None, offerSubscriptionId: str | None = None, **_other) -> list:
    """The stand-in filters by `appId` and `offerSubscriptionId` only."""
    return [c for c in instance_clients.values()
            if (not appId or c["appId"] == appId) and (not offerSubscriptionId or c["offerSubscriptionId"] == offerSubscriptionId)]


def get_app_instance_client(*, id: str) -> dict:
    if id not in instance_clients:
        raise Problem(MODULE, 404, "not-found")
    return instance_clients[id]


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
