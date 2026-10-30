# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C6-M1 stand-in: the query of connectors by technical user."""
from __future__ import annotations

from portal_common import standin

MODULE = "c6-m1"
connectors: dict[str, dict] = {}


def reset() -> None:
    connectors.clear()


def list_connectors_internal(*, technicalUserId: str | None = None) -> list:
    return [c for c in connectors.values() if technicalUserId and c.get("technicalUserId") == technicalUserId]


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
