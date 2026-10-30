# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C8-M1 stand-in: `getOfferInternal` serves the lookup `offer` of Process
Orchestration and Subscriptions."""
from __future__ import annotations

from portal_common import standin
from portal_common.problems import Problem

MODULE = "c8-m1"
offers: dict[str, dict] = {}


def reset() -> None:
    offers.clear()


def get_offer_internal(*, offerId: str) -> dict:
    o = offers.get(offerId)
    if not o:
        raise Problem(MODULE, 404, "not-found")
    return o | {"hasTechnicalUserProfile": o["technicalUserProfile"] is not None}


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
