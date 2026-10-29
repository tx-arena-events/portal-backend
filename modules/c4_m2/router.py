# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C4-M2 Dataspace Wallet Interface: `create-wallet` and the wallet queries.

Provider: the dataspace wallet of the CSP when `CSP_WALLET_URL` is set (C4-M2 11.3, e.g. the
shared CSP stand-in `/wallet/v1`), otherwise `STUB` (DID `did:web:wallet.example.org:<BPN>`).
The holder access (API key) stays in memory and is only returned by the query for C4-M1.
"""
from __future__ import annotations

import os

from fastapi import Body

from portal_common import csp, events, neighbours, standin
from portal_common.problems import Problem

STANDIN = True
MODULE = "c4-m2"
router = standin.router(MODULE)
wallets: dict[str, dict] = {}


def reset() -> None:
    wallets.clear()


@router.post("/steps/create-wallet")
def create_wallet(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/create-wallet")):
    cid = body.get("companyId")
    if cid not in wallets:
        company = neighbours.get_json(MODULE, "c3-m1", f"/internal/companies/{cid}")
        if not company or not company.get("bpn"):
            raise Problem(MODULE, 409, "invalid-state", detail="company without BPN", retryable=False)
        bpn, url = company["bpn"], os.environ.get("CSP_WALLET_URL", "")
        if url:
            try:
                status, answer = csp.request(url, "POST", "/wallets", {"bpn": bpn})
            except csp.CspUnavailable as e:
                raise Problem(MODULE, 502, "upstream-error", detail=str(e), retryable=True) from e
            if status != 201:
                raise Problem(MODULE, 409 if status in (403, 409) else 502,
                              "provider-rejected" if status in (403, 409) else "upstream-error",
                              detail=f"CSP wallet answered {status}", retryable=status >= 500)
            wallets[cid] = {"companyId": cid, "status": "ACTIVE", "provider": "CSP", "issuanceMode": "HOLDER_REQUESTS",
                            "did": answer["did"], "providerRef": answer["participantContextId"],
                            "_holder": {k: answer[k] for k in ("identityApiUrl", "participantContextId", "apiKey")}}
        else:
            wallets[cid] = {"companyId": cid, "status": "ACTIVE", "provider": "STUB", "issuanceMode": "STUB",
                            "did": f"did:web:wallet.example.org:{bpn}", "providerRef": None, "_holder": None}
        events.publish({"eventType": "DataspaceWalletCreated", "companyId": cid, "did": wallets[cid]["did"]})
    return standin.step_reply(body, state="RUNNING")   # long-running; completed by DataspaceWalletCreated


def _wallet(cid: str) -> dict:
    if cid not in wallets:
        raise Problem(MODULE, 404, "not-found")
    return wallets[cid]


@router.get("/internal/wallets/{companyId}")
def get_wallet(companyId: str, _: dict = standin.perm(MODULE, "GET", "/internal/wallets/{companyId}")):
    return {k: v for k, v in _wallet(companyId).items() if not k.startswith("_")}


@router.get("/internal/wallets/{companyId}/holder-access")
def holder_access(companyId: str, _: dict = standin.perm(MODULE, "GET", "/internal/wallets/{companyId}/holder-access")):
    w = _wallet(companyId)
    if not w["_holder"]:
        raise Problem(MODULE, 409, "invalid-state", detail="no holder access for provider STUB")
    return w["_holder"]


@router.get("/companies/{companyId}/wallet")
def own_wallet(companyId: str, claims: dict = standin.perm(MODULE, "GET", "/companies/{companyId}/wallet")):
    standin.company_scope(MODULE, claims, companyId)
    return get_wallet(companyId, claims)
