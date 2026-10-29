# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C2-M3 Technical Users: `create-for-subscription` (technical user in the test IAM,
roles checked against C2-M5 6.4), the query of a technical user and the rotation of its secret
for Subscriptions. Secrets only in the answer of the rotation, never in step results or events.
"""
from __future__ import annotations

from fastapi import Body

from portal_common import events, standin
from portal_common.problems import Problem
from tools import test_iam

STANDIN = True
MODULE = "c2-m3"
router = standin.router(MODULE)
technical_users: dict[str, dict] = {}


def reset() -> None:
    technical_users.clear()


@router.post("/steps/create-for-subscription")
def create_for_subscription(body: dict = Body(...),
                            _: dict = standin.perm(MODULE, "POST", "/steps/create-for-subscription")):
    sub_id = body.get("offerSubscriptionId")
    tid = standin.stable_id("technical-user", sub_id)
    if tid not in technical_users:
        roles = [r["name"] if isinstance(r, dict) else r for r in body.get("roles") or []]
        client_id = f"sa-{tid[:8]}"
        try:
            test_iam.iam().create_technical_user(client_id, body.get("customerCompanyId"), None, roles)
        except ValueError as e:
            raise Problem(MODULE, 400, "role-not-assignable", detail=str(e), retryable=False) from e
        technical_users[tid] = {"id": tid, "clientId": client_id, "name": body.get("name"), "roles": roles,
                                "companyId": body.get("customerCompanyId"),
                                "providerCompanyId": body.get("providerCompanyId"), "offerSubscriptionId": sub_id,
                                "origin": "SUBSCRIPTION", "status": "ACTIVE"}
        events.publish({"eventType": "TechnicalUserCreated", "technicalUserId": tid,
                        "companyId": body.get("customerCompanyId")})
    tu = technical_users[tid]
    return standin.step_reply(body, {"accounts": [{"technicalUserId": tid, "clientId": tu["clientId"]}]})


def _get(id: str) -> dict:
    if id not in technical_users:
        raise Problem(MODULE, 404, "not-found")
    return technical_users[id]


@router.get("/internal/technical-users/{id}")
def get_technical_user(id: str, _: dict = standin.perm(MODULE, "GET", "/internal/technical-users/{id}")):
    return _get(id)


@router.post("/internal/technical-users/{id}/credentials/rotate")
def rotate(id: str, _: dict = standin.perm(MODULE, "POST", "/internal/technical-users/{id}/credentials/rotate")):
    tu = _get(id)
    return {"technicalUserId": id, "clientId": tu["clientId"], "clientSecret": test_iam.iam().rotate_secret(tu["clientId"])}
