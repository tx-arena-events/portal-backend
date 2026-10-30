# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C4-M2 stand-in: `create-wallet` and the wallet queries.

Provider: the dataspace wallet of the CSP when `CSP_WALLET_URL` is set (C4-M2 11.3, e.g. the
shared CSP stand-in `/wallet/v1`), otherwise `STUB` (DID `did:web:wallet.example.org:<BPN>`).
The holder access (API key) stays in memory and is only returned by the query for C4-M1.
"""
from __future__ import annotations

import os

from portal_common import csp, events, neighbours, standin
from portal_common.problems import Problem

MODULE = "c4-m2"
wallets: dict[str, dict] = {}


def reset() -> None:
    wallets.clear()


def create_wallet(input: dict) -> dict:
    cid = input.get("companyId")
    if cid not in wallets:
        company = neighbours.query(MODULE, "c3-m1", "query_company", companyId=cid)
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
    return standin.step_reply(input, state="RUNNING")   # long-running; completed by DataspaceWalletCreated


def _wallet(cid: str) -> dict:
    if cid not in wallets:
        raise Problem(MODULE, 404, "not-found")
    return wallets[cid]


def get_wallet_info(*, companyId: str) -> dict:
    return {k: v for k, v in _wallet(companyId).items() if not k.startswith("_")}


def get_holder_access(*, companyId: str) -> dict:
    w = _wallet(companyId)
    if not w["_holder"]:
        raise Problem(MODULE, 409, "invalid-state", detail="no holder access for provider STUB")
    return w["_holder"]


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
