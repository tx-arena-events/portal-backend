# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C3-M1 Company Profile: seed of the OSP company, the onboarding steps, the query
for other modules and Process Orchestration, and reading a company. In memory.

`create-company` reads the application from C1-M2 (`ApplicationInternal` as C1-M2 specifies it)
and maps it as C3-M1 11.1 describes. Consents are mapped from `agreementKey` + `version` to
`agreementId` through the shared catalogue `testdata/agreements.json` (provisional, OP-46).
"""
from __future__ import annotations

from fastapi import Body

from portal_common import standin

from . import interface
from .interface import MODULE, get_company, role_view

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()



@router.post("/steps/create-company")
def create_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/create-company")):
    return interface.create_company(body)


@router.post("/steps/assign-identifier")
def assign_identifier(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/assign-identifier")):
    return interface.assign_identifier(body)


@router.post("/steps/activate-company")
def activate_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/activate-company")):
    return interface.activate_company(body)


@router.post("/steps/reject-company")
def reject_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/reject-company")):
    return interface.reject_company(body)


@router.get("/internal/companies/{companyId}")
def query_company(companyId: str, _: dict = standin.perm(MODULE, "GET", "/internal/companies/{companyId}")):
    return interface.query_company(companyId=companyId)


@router.get("/osp/companies/{companyId}")
def osp_company(companyId: str, _: dict = standin.perm(MODULE, "GET", "/osp/companies/{companyId}")):
    return role_view(get_company(companyId))


@router.get("/companies/{companyId}")
def own_company(companyId: str, claims: dict = standin.perm(MODULE, "GET", "/companies/{companyId}")):
    standin.company_scope(MODULE, claims, companyId)
    return role_view(get_company(companyId))
