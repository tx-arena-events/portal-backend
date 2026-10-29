# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Shared test data package (`testdata/`): one catalogue of terms, the OSP company, test persons."""
from __future__ import annotations

import functools
import json

from portal_common import settings
from portal_common.spec import ROOT

DIR = ROOT / "testdata"


@functools.lru_cache(maxsize=None)
def agreements() -> list[dict]:
    """Terms with `agreementId` (C3-M1) and `agreementKey` + `version` (C1-M2), one entry per version."""
    return json.loads((DIR / "agreements.json").read_text(encoding="utf-8"))["agreements"]


def agreements_for_roles(roles: list[str]) -> list[dict]:
    return [a for a in agreements() if set(a["companyRoles"]) & set(roles)]


def agreement_id(key: str, version: str) -> str | None:
    return next((a["agreementId"] for a in agreements() if a["agreementKey"] == key and a["version"] == version), None)


def osp_company() -> dict | None:
    """Seed data of the OSP company of this installation (`ospCompanyFile` in the settings)."""
    path = settings.resolve_path(settings.get("ospCompanyFile"))
    return json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else None
