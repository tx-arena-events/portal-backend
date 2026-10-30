# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C2-M3 Technical Users: `create-for-subscription` (technical user in the test IAM,
roles checked against C2-M5 6.4), the query of a technical user and the rotation of its secret
for Subscriptions. Secrets only in the answer of the rotation, never in step results or events.
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


@router.post("/steps/create-for-subscription")
def create_for_subscription(body: dict = Body(...),
                            _: dict = standin.perm(MODULE, "POST", "/steps/create-for-subscription")):
    return interface.create_for_subscription(body)


@router.get("/internal/technical-users/{id}")
def get_technical_user(id: str, _: dict = standin.perm(MODULE, "GET", "/internal/technical-users/{id}")):
    return interface.check_technical_user(id=id)


@router.post("/internal/technical-users/{id}/credentials/rotate")
def rotate(id: str, _: dict = standin.perm(MODULE, "POST", "/internal/technical-users/{id}/credentials/rotate")):
    return interface.rotate_subscription_credentials(id=id)
