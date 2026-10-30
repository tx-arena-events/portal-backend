# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C2-M3 stand-in: `create-for-subscription` (technical user in the test IAM,
roles checked against C2-M5 6.4), the query of a technical user and the rotation of its secret
for Subscriptions. Secrets only in the answer of the rotation, never in step results or events."""
from __future__ import annotations

from portal_common import events, standin
from portal_common.problems import Problem
from tools import test_iam

MODULE = "c2-m3"
technical_users: dict[str, dict] = {}


def reset() -> None:
    technical_users.clear()


def create_for_subscription(input: dict) -> dict:
    sub_id = input.get("offerSubscriptionId")
    tid = standin.stable_id("technical-user", sub_id)
    if tid not in technical_users:
        roles = [r["name"] if isinstance(r, dict) else r for r in input.get("roles") or []]
        client_id = f"sa-{tid[:8]}"
        try:
            test_iam.iam().create_technical_user(client_id, input.get("customerCompanyId"), None, roles)
        except ValueError as e:
            raise Problem(MODULE, 400, "role-not-assignable", detail=str(e), retryable=False) from e
        technical_users[tid] = {"id": tid, "clientId": client_id, "name": input.get("name"), "roles": roles,
                                "companyId": input.get("customerCompanyId"),
                                "providerCompanyId": input.get("providerCompanyId"), "offerSubscriptionId": sub_id,
                                "origin": "SUBSCRIPTION", "status": "ACTIVE"}
        events.publish({"eventType": "TechnicalUserCreated", "technicalUserId": tid,
                        "companyId": input.get("customerCompanyId")})
    tu = technical_users[tid]
    return standin.step_reply(input, {"accounts": [{"technicalUserId": tid, "clientId": tu["clientId"]}]})


def _get(id: str) -> dict:
    if id not in technical_users:
        raise Problem(MODULE, 404, "not-found")
    return technical_users[id]


def check_technical_user(*, id: str, **_filters) -> dict:
    """The stand-in ignores the filters `companyId` and `offerSubscriptionId`."""
    return _get(id)


def rotate_subscription_credentials(*, id: str, **_body) -> dict:
    """The stand-in ignores `offerSubscriptionId` of the body."""
    tu = _get(id)
    return {"technicalUserId": id, "clientId": tu["clientId"], "clientSecret": test_iam.iam().rotate_secret(tu["clientId"])}


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
