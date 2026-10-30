# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C10-M1 Subscriptions, app path of FRAMEWORK 5.8: provider configuration, subscribe,
`notify-provider`, start setup, activate (only when the setup process at C12-M1 is `WAITING`, 6.3),
`send-activation-callback` (client ID of the instance and a rotated secret of the technical user
to the provider's `callbackUrl`, C10-M1 11.2). Provider secrets stay in memory.
"""
from __future__ import annotations

import uuid

from fastapi import Body

from portal_common import events, neighbours, standin
from portal_common.problems import Problem

from . import interface
from .interface import MODULE, provider_config, subscriptions

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()


def _visible(claims: dict, sid: str, provider_only: bool = False) -> dict:
    s = subscriptions.get(sid)
    cid = claims.get("company_id")
    if not s or cid not in (s["providerCompanyId"], s["customerCompanyId"]):
        raise Problem(MODULE, 404, "not-found")
    if provider_only and cid != s["providerCompanyId"]:
        raise Problem(MODULE, 403, "forbidden")
    return s


def _event_fields(s: dict) -> dict:
    """Content of the subscription events (C10-M1 section 10)."""
    return {k: s[k] for k in ("offerSubscriptionId", "offerId", "offerType", "customerCompanyId", "providerCompanyId")}


@router.put("/provider-configuration")
def put_config(body: dict = Body(...), claims: dict = standin.perm(MODULE, "PUT", "/provider-configuration")):
    missing = [k for k in ("setupUrl", "callbackUrl", "tokenUrl", "clientId", "clientSecret") if not body.get(k)]
    if missing or not claims.get("company_id"):
        raise Problem(MODULE, 400, "validation", missing=missing)
    provider_config[claims["company_id"]] = dict(body)
    return {k: v for k, v in body.items() if k != "clientSecret"}


@router.post("/subscriptions", status_code=201)
def subscribe(body: dict = Body(...), claims: dict = standin.perm(MODULE, "POST", "/subscriptions")):
    cid = claims.get("company_id")
    offer = neighbours.query(MODULE, "c8-m1", "get_offer_internal", offerId=body.get("offerId"))
    if not offer or offer["status"] != "ACTIVE":
        raise Problem(MODULE, 409, "offer-not-active")
    if offer["providerCompanyId"] == cid:
        raise Problem(MODULE, 409, "own-offer")
    s = {"offerSubscriptionId": str(uuid.uuid4()), "offerId": offer["offerId"], "offerType": offer["offerType"],
         "instanceKind": offer.get("instanceKind"), "customerCompanyId": cid,
         "providerCompanyId": offer["providerCompanyId"], "status": "REQUESTED", "instanceUrl": None}
    subscriptions[s["offerSubscriptionId"]] = s
    events.publish({"eventType": "SubscriptionRequested", **_event_fields(s)})
    return s


@router.get("/subscriptions/{offerSubscriptionId}")
def get_subscription(offerSubscriptionId: str,
                     claims: dict = standin.perm(MODULE, "GET", "/subscriptions/{offerSubscriptionId}")):
    s = _visible(claims, offerSubscriptionId)
    return s | {"setupStatus": _setup_status(s["offerSubscriptionId"])}


@router.post("/subscriptions/{offerSubscriptionId}/start-setup", status_code=202)
def start_setup(offerSubscriptionId: str, body: dict = Body(default={}),
                claims: dict = standin.perm(MODULE, "POST", "/subscriptions/{offerSubscriptionId}/start-setup")):
    s = _visible(claims, offerSubscriptionId, provider_only=True)
    if s["status"] != "REQUESTED":
        raise Problem(MODULE, 409, "invalid-state")
    s["status"], s["instanceUrl"] = "PENDING", body.get("instanceUrl")
    events.publish({"eventType": "SubscriptionSetupStarted", **_event_fields(s), "instanceUrl": s["instanceUrl"]})
    return s


def _setup_status(sid: str) -> str | None:
    procs = neighbours.query(MODULE, "c12-m1", "find_processes_of_subject", subjectType="SUBSCRIPTION",
                             subjectId=sid) or []
    setup = next((p for p in procs if p["flowKey"] == "subscription-setup"), None)
    return setup["status"] if setup else None


@router.post("/subscriptions/{offerSubscriptionId}/activate")
def activate(offerSubscriptionId: str,
             claims: dict = standin.perm(MODULE, "POST", "/subscriptions/{offerSubscriptionId}/activate")):
    s = _visible(claims, offerSubscriptionId, provider_only=True)
    if s["status"] != "PENDING":
        raise Problem(MODULE, 409, "invalid-state")
    status = _setup_status(offerSubscriptionId)
    if status == "RUNNING":
        raise Problem(MODULE, 409, "setup-not-finished")
    if status == "FAILED":
        raise Problem(MODULE, 409, "setup-failed")
    s["status"] = "ACTIVE"
    events.publish({"eventType": "SubscriptionActivated", **_event_fields(s), "instanceKind": s["instanceKind"]})
    return s


@router.post("/steps/notify-provider")
def notify_provider(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/notify-provider")):
    return interface.notify_provider(body)


@router.post("/steps/send-activation-callback")
def send_activation_callback(body: dict = Body(...),
                             _: dict = standin.perm(MODULE, "POST", "/steps/send-activation-callback")):
    return interface.send_activation_callback(body)
