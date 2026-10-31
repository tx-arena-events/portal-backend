# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C2-M3 stand-in: `create-for-subscription` (technical user in the test IAM,
roles checked against C2-M5 6.4), the query of a technical user and the rotation of its secret
for Subscriptions. Secrets only in the answer of the rotation, never in step results or events."""
from __future__ import annotations

from portal_common import events, standin, tokens
from portal_common.problems import Problem
from tools import iam_backend

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
            iam_backend.backend(MODULE).create_technical_user(client_id, input.get("customerCompanyId"), None, roles)
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


def check_technical_user(*, id: str, companyId: str | None = None, offerSubscriptionId: str | None = None,
                         **_other) -> dict:
    """9.1: always 200; the stand-in only knows subscription accounts."""
    tu = technical_users.get(id)
    if tu is None:
        return {"exists": False, "usableForConnector": False}
    usable = tu["companyId"] == companyId and tu["status"] in ("ACTIVE", "PENDING") and (
        tu["origin"] == "OWN" or (offerSubscriptionId is not None and tu["offerSubscriptionId"] == offerSubscriptionId))
    return {"exists": True, "belongsToCompany": tu["companyId"] == companyId, "status": tu["status"],
            "origin": tu["origin"], "walletAccess": "NONE", "disabled": False, "usableForConnector": usable}


def rotate_subscription_credentials(*, id: str, offerSubscriptionId: str | None = None, **_other) -> dict:
    """9.2: a new secret of the subscription account."""
    tu = _get(id)
    if offerSubscriptionId is not None and tu["offerSubscriptionId"] != offerSubscriptionId:
        raise Problem(MODULE, 404, "not-found")
    return {"iamClientId": tu["clientId"], "secret": iam_backend.backend(MODULE).rotate_secret(tu["clientId"]),
            "tokenUrl": f"{tokens.iam_url()}/realms/{tokens.realm()}/protocol/openid-connect/token"}


def list_technical_roles(*, lang: str | None = None, **_other) -> list[dict]:
    """9.3: the roles of the test IAM (C2-M5 6.4) with the flags of the example values of README 4;
    the stand-in has no descriptions."""
    wallet = {"Identity Wallet Management"}
    provider_only = {"BPDM Pool Consumer", "BPDM Sharing Output Consumer", "BPDM Sharing Input Manager"}
    return [{"name": r, "description": None, "grantsWalletAccess": r in wallet, "providerOnly": r in provider_only}
            for r in sorted(iam_backend.backend(MODULE).technical_roles)]


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
