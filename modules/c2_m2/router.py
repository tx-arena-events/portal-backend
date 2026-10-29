# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C2-M2 Users and App Access: `assign-company-admin` (binds the applicant in the test
IAM), client template and instance clients of apps, and the queries on instance clients.
Writes to the in-memory test IAM through its Python API (the Keycloak Admin API is not simulated).
"""
from __future__ import annotations

from fastapi import Body

from portal_common import events, standin
from portal_common.problems import Problem
from tools import test_iam

STANDIN = True
MODULE = "c2-m2"
router = standin.router(MODULE)
templates: dict[str, dict] = {}          # appId -> template
instance_clients: dict[str, dict] = {}   # id -> instance client


def reset() -> None:
    templates.clear()
    instance_clients.clear()


@router.post("/steps/assign-company-admin")
def assign_company_admin(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/assign-company-admin")):
    iam, uid = test_iam.iam(), body.get("applicantSub")
    if uid not in iam.users:
        raise Problem(MODULE, 404, "not-found", detail="account unknown", retryable=False)
    person = iam.person(uid)
    if person["companyId"] not in (None, body.get("companyId")):
        raise Problem(MODULE, 409, "account-bound", retryable=False)
    if person["companyId"] != body.get("companyId") or "Company Admin" not in person["portalRoles"]:
        iam.bind(uid, body.get("companyId"), body.get("bpn"))
        iam.add_portal_roles(uid, ["Company Admin"])
        events.publish({"eventType": "UserAssignedToCompany", "userId": uid, "companyId": body.get("companyId"),
                        "via": "ONBOARDING", "portalRoles": ["Company Admin"]})
    return standin.step_reply(body, {"userId": uid})


@router.post("/steps/create-client-template")
def create_client_template(body: dict = Body(...),
                           _: dict = standin.perm(MODULE, "POST", "/steps/create-client-template")):
    app_id = body.get("appId")
    if not body.get("roles"):
        raise Problem(MODULE, 400, "validation", detail="roles", retryable=False)
    if app_id not in templates:
        templates[app_id] = {"appId": app_id, "clientId": f"app-{standin.stable_id('app', app_id)[:8]}",
                             "roles": [r["name"] for r in body["roles"]], "appName": body.get("appName")}
        events.publish({"eventType": "AppClientTemplateCreated", "appId": app_id})
    return standin.step_reply(body, {"clientId": templates[app_id]["clientId"]})


@router.post("/steps/create-instance-client")
def create_instance_client(body: dict = Body(...),
                           _: dict = standin.perm(MODULE, "POST", "/steps/create-instance-client")):
    key = (body.get("appId"), body.get("offerSubscriptionId"))
    ic = next((c for c in instance_clients.values() if (c["appId"], c["offerSubscriptionId"]) == key), None)
    if ic is None:
        iid = standin.stable_id("instance-client", *key)
        ic = {"id": iid, "appId": key[0], "offerSubscriptionId": key[1], "customerCompanyId": body.get("customerCompanyId"),
              "instanceKind": body.get("instanceKind"), "instanceUrl": body.get("instanceUrl"),
              "iamClientId": f"app-{iid[:8]}", "status": "INACTIVE"}
        instance_clients[iid] = ic
        events.publish({"eventType": "AppInstanceClientCreated", "appInstanceClientId": iid, "appId": key[0]})
    return standin.step_reply(body, {"id": ic["id"], "iamClientId": ic["iamClientId"]})


def _set_status(body: dict, status: str, event: str) -> dict:
    ic = instance_clients.get(body.get("appInstanceClientId"))
    if ic is None:
        raise Problem(MODULE, 404, "not-found", retryable=False)
    if ic["status"] != status:
        ic["status"] = status
        events.publish({"eventType": event, "appInstanceClientId": ic["id"], "appId": ic["appId"]})
    return standin.step_reply(body, {"id": ic["id"], "status": status})


@router.post("/steps/enable-instance-client")
def enable(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/enable-instance-client")):
    return _set_status(body, "ACTIVE", "AppInstanceClientEnabled")


@router.post("/steps/disable-instance-client")
def disable(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/disable-instance-client")):
    return _set_status(body, "INACTIVE", "AppInstanceClientDisabled")


@router.get("/internal/app-instance-clients")
def list_instance_clients(_: dict = standin.perm(MODULE, "GET", "/internal/app-instance-clients"),
                          appId: str | None = None, offerSubscriptionId: str | None = None):
    return [c for c in instance_clients.values()
            if (not appId or c["appId"] == appId) and (not offerSubscriptionId or c["offerSubscriptionId"] == offerSubscriptionId)]


@router.get("/internal/app-instance-clients/{id}")
def get_instance_client(id: str, _: dict = standin.perm(MODULE, "GET", "/internal/app-instance-clients/{id}")):
    if id not in instance_clients:
        raise Problem(MODULE, 404, "not-found")
    return instance_clients[id]
