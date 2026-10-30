# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C10-M1 stand-in: the steps `notify-provider` and `send-activation-callback`
(client ID of the instance and a rotated secret of the technical user to the provider's
`callbackUrl`, C10-M1 11.2). Provider secrets stay in memory."""
from __future__ import annotations

import httpx

from portal_common import http, neighbours, standin
from portal_common.problems import Problem

MODULE = "c10-m1"
subscriptions: dict[str, dict] = {}
provider_config: dict[str, dict] = {}   # providerCompanyId -> configuration (with secret)


def reset() -> None:
    subscriptions.clear()
    provider_config.clear()


def _provider_call(provider: str, kind: str, body: dict) -> None:
    cfg = provider_config.get(provider)
    if not cfg:
        raise Problem(MODULE, 409, "invalid-state", detail="provider has no configuration", retryable=False)
    try:
        t = http.request("POST", cfg["tokenUrl"], data={"grant_type": "client_credentials", "client_id": cfg["clientId"],
                                                        "client_secret": cfg["clientSecret"]})
        if t.status_code != 200:
            raise Problem(MODULE, 502, "provider-rejected", detail=f"token {t.status_code}", retryable=t.status_code >= 500)
        r = http.request("POST", cfg[f"{kind}Url"], json=body, headers={"Authorization": "Bearer " + t.json()["access_token"]})
    except httpx.HTTPError as e:
        raise Problem(MODULE, 502, "upstream-error", detail=type(e).__name__, retryable=True) from e
    if not r.is_success:
        raise Problem(MODULE, 502, "provider-rejected" if r.status_code < 500 else "upstream-error",
                      detail=f"{kind} {r.status_code}", retryable=r.status_code >= 500)


def notify_provider(input: dict) -> dict:
    s = subscriptions.get(input.get("offerSubscriptionId"))
    if not s:
        raise Problem(MODULE, 404, "not-found", retryable=False)
    customer = neighbours.query(MODULE, "c3-m1", "query_company", companyId=s["customerCompanyId"]) or {}
    _provider_call(s["providerCompanyId"], "setup", {
        "offerSubscriptionId": s["offerSubscriptionId"], "offerId": s["offerId"], "customerCompanyId": s["customerCompanyId"],
        "customerName": customer.get("name"), "customerCountry": (customer.get("address") or {}).get("countryCode"),
        "setupExpected": s["offerType"] == "APP"})
    return standin.step_reply(input)


def send_activation_callback(input: dict) -> dict:
    s = subscriptions.get(input.get("offerSubscriptionId"))
    if not s:
        raise Problem(MODULE, 404, "not-found", retryable=False)
    client_id = input.get("clientId")   # iamClientId of the instance client (C10-M1 8.2)
    if s["providerCompanyId"] not in provider_config or not provider_config[s["providerCompanyId"]].get("callbackUrl"):
        return standin.step_reply(input)    # without a callbackUrl: nothing to do
    technical_user = None
    for acc in input.get("technicalUsers") or []:
        r = neighbours.query(MODULE, "c2-m3", "rotate_subscription_credentials", id=acc["technicalUserId"])
        technical_user = {k: r[k] for k in ("clientId", "clientSecret")}
    _provider_call(s["providerCompanyId"], "callback", {"offerSubscriptionId": s["offerSubscriptionId"],
                                                        "instanceUrl": s["instanceUrl"], "clientId": client_id,
                                                        "technicalUser": technical_user})
    return standin.step_reply(input)


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
