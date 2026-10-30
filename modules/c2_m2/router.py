# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C2-M2 Users and App Access: `assign-company-admin` (binds the applicant in the test
IAM), client template and instance clients of apps, and the queries on instance clients.
Writes to the in-memory test IAM through its Python API (the Keycloak Admin API is not simulated).
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



@router.post("/steps/assign-company-admin")
def assign_company_admin(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/assign-company-admin")):
    return interface.assign_company_admin(body)


@router.post("/steps/create-client-template")
def create_client_template(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/create-client-template")):
    return interface.create_client_template(body)


@router.post("/steps/create-instance-client")
def create_instance_client(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/create-instance-client")):
    return interface.create_instance_client(body)


@router.post("/steps/enable-instance-client")
def enable_instance_client(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/enable-instance-client")):
    return interface.enable_instance_client(body)


@router.post("/steps/disable-instance-client")
def disable_instance_client(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/disable-instance-client")):
    return interface.disable_instance_client(body)


@router.get("/internal/app-instance-clients")
def list_instance_clients(_: dict = standin.perm(MODULE, "GET", "/internal/app-instance-clients"),
                          appId: str | None = None, offerSubscriptionId: str | None = None):
    return interface.list_app_instance_clients(appId=appId, offerSubscriptionId=offerSubscriptionId)


@router.get("/internal/app-instance-clients/{id}")
def get_instance_client(id: str, _: dict = standin.perm(MODULE, "GET", "/internal/app-instance-clients/{id}")):
    return interface.get_app_instance_client(id=id)
