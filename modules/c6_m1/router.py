# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C6-M1 Connectors: create, list and read connectors of the own company
(`ConnectorRegistered` leads to C3-M2 `register-connector`, FRAMEWORK 5.9) and the query of
connectors by technical user. The company must be `ACTIVE` at C3-M1.
"""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import Body

from portal_common import events, neighbours, standin
from portal_common.problems import Problem

from . import interface
from .interface import MODULE, connectors

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()


@router.post("/connectors", status_code=201)
def create(body: dict = Body(...), claims: dict = standin.perm(MODULE, "POST", "/connectors")):
    cid = claims.get("company_id")
    if not cid:
        raise Problem(MODULE, 403, "forbidden", detail="no company")
    if not str(body.get("endpoint", "")).startswith("https://") or not body.get("name"):
        raise Problem(MODULE, 400, "validation", detail="name and https endpoint required")
    company = neighbours.query(MODULE, "c3-m1", "query_company", companyId=cid)
    if not company or company.get("status") != "ACTIVE":
        raise Problem(MODULE, 409, "company-not-active")
    con = {"connectorId": str(uuid.uuid4()), "companyId": cid, "name": body["name"], "endpoint": body["endpoint"],
           "location": body.get("location"), "status": "ACTIVE", "technicalUserId": body.get("technicalUserId"),
           "createdAt": dt.datetime.now(dt.UTC).isoformat()}
    connectors[con["connectorId"]] = con
    events.publish({"eventType": "ConnectorRegistered", "connectorId": con["connectorId"], "companyId": cid,
                    "endpoint": con["endpoint"]})
    return con


@router.get("/connectors")
def list_own(claims: dict = standin.perm(MODULE, "GET", "/connectors")):
    items = [c for c in connectors.values() if c["companyId"] == claims.get("company_id")]
    return {"items": items, "page": 0, "size": len(items), "total": len(items)}


@router.get("/connectors/{connectorId}")
def get_own(connectorId: str, claims: dict = standin.perm(MODULE, "GET", "/connectors/{connectorId}")):
    con = connectors.get(connectorId)
    standin.company_scope(MODULE, claims, con and con["companyId"])
    return con


@router.get("/internal/connectors")
def by_technical_user(_: dict = standin.perm(MODULE, "GET", "/internal/connectors"), technicalUserId: str | None = None):
    return interface.list_connectors_internal(technicalUserId=technicalUserId)
