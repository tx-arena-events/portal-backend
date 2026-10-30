# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C4-M2 Dataspace Wallet Interface: `create-wallet` and the wallet queries.

Provider: the dataspace wallet of the CSP when `CSP_WALLET_URL` is set (C4-M2 11.3, e.g. the
shared CSP stand-in `/wallet/v1`), otherwise `STUB` (DID `did:web:wallet.example.org:<BPN>`).
The holder access (API key) stays in memory and is only returned by the query for C4-M1.
"""
from __future__ import annotations

from fastapi import Body

from portal_common import standin

from . import interface
from .interface import MODULE

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()


@router.post("/steps/create-wallet")
def create_wallet(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/create-wallet")):
    return interface.create_wallet(body)


@router.get("/internal/wallets/{companyId}")
def get_wallet(companyId: str, _: dict = standin.perm(MODULE, "GET", "/internal/wallets/{companyId}")):
    return interface.get_wallet_info(companyId=companyId)


@router.get("/internal/wallets/{companyId}/holder-access")
def holder_access(companyId: str, _: dict = standin.perm(MODULE, "GET", "/internal/wallets/{companyId}/holder-access")):
    return interface.get_holder_access(companyId=companyId)


@router.get("/companies/{companyId}/wallet")
def own_wallet(companyId: str, claims: dict = standin.perm(MODULE, "GET", "/companies/{companyId}/wallet")):
    standin.company_scope(MODULE, claims, companyId)
    return interface.get_wallet_info(companyId=companyId)
