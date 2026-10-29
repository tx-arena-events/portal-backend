# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C3-M1 Company Profile: seed of the OSP company, the onboarding steps, the query
for other modules and Process Orchestration, and reading a company. In memory.

`create-company` reads the application from C1-M2 (`ApplicationInternal` as C1-M2 specifies it)
and maps it as C3-M1 11.1 describes. Consents are mapped from `agreementKey` + `version` to
`agreementId` through the shared catalogue `testdata/agreements.json` (provisional, OP-46).
"""
from __future__ import annotations

import datetime as dt

from fastapi import Body

from portal_common import events, neighbours, settings, standin
from portal_common.problems import Problem
from tools import testdata

STANDIN = True
MODULE = "c3-m1"
router = standin.router(MODULE)
companies: dict[str, dict] = {}


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def reset() -> None:
    companies.clear()
    seed = testdata.osp_company()
    if seed:
        if seed["companyId"] != settings.get("ospCompanyId"):
            raise ValueError("OSP company file: companyId differs from ospCompanyId (C3-M1 4)")
        companies[seed["companyId"]] = {
            "id": seed["companyId"], "name": seed["name"], "shortName": None, "address": seed["address"],
            "identifiers": seed["identifiers"], "coreDataSource": "MANUAL", "status": "ACTIVE", "bpn": seed["bpn"],
            "companyRoles": seed["companyRoles"], "requestedRoles": [], "consents": [], "createdAt": _now()}


def _get(cid: str | None) -> dict:
    if cid not in companies:
        raise Problem(MODULE, 404, "not-found", retryable=False)
    return companies[cid]


def _view(c: dict) -> dict:
    return {k: v for k, v in c.items() if k not in ("requestedRoles", "consents")}


@router.post("/steps/create-company")
def create_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/create-company")):
    cid = body.get("companyId")
    if cid not in companies:
        app = neighbours.get_json(MODULE, "c1-m2", f"/internal/applications/{body.get('applicationId')}")
        if app is None:
            raise Problem(MODULE, 404, "not-found", detail="application unknown", retryable=False)
        data, addr = app["companyData"], app["companyData"]["address"]
        companies[cid] = {
            "id": cid, "name": data["legalName"], "shortName": data.get("shortName"),
            "address": {"street": addr.get("streetName"), "houseNumber": addr.get("streetNumber"),
                        "postalCode": addr.get("postalCode"), "city": addr.get("city"), "region": addr.get("region"),
                        "countryCode": addr.get("countryCode")},
            "identifiers": [{"type": i["type"], "value": i["value"],
                             "source": "BUSINESS_WALLET" if i.get("source") == "ATTESTATION" else "DECLARED"}
                            for i in data["identifiers"]],
            "coreDataSource": "BUSINESS_WALLET" if app["verification"] == "EBW" else "MANUAL",
            "status": "REQUESTED", "bpn": None, "companyRoles": [], "requestedRoles": app["companyRoles"],
            "consents": [{"agreementId": testdata.agreement_id(c["agreementKey"], c["version"]), "status": "ACTIVE",
                          "consentedAt": c.get("givenAt"), "consentedBy": app["applicant"]["sub"]}
                         for c in app.get("consents", [])],
            "createdAt": _now()}
        events.publish({"eventType": "CompanyCreated", "companyId": cid, "applicationId": body.get("applicationId"),
                        "coreDataSource": companies[cid]["coreDataSource"]})
    return standin.step_reply(body, {"companyId": cid, "status": companies[cid]["status"]})


@router.post("/steps/assign-identifier")
def assign_identifier(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/assign-identifier")):
    c = _get(body.get("companyId"))
    if not body.get("bpn"):
        raise Problem(MODULE, 400, "validation", detail="bpn", retryable=False)
    if c["bpn"] != body["bpn"]:
        c["bpn"] = body["bpn"]
        events.publish({"eventType": "CompanyIdentifierAssigned", "companyId": c["id"], "bpn": c["bpn"]})
    return standin.step_reply(body, {"companyId": c["id"], "bpn": c["bpn"]})


@router.post("/steps/activate-company")
def activate_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/activate-company")):
    c = _get(body.get("companyId"))
    if c["status"] != "ACTIVE":
        consented = {x["agreementId"] for x in c["consents"] if x["status"] == "ACTIVE"}
        missing = [a["agreementId"] for a in testdata.agreements_for_roles(c["requestedRoles"])
                   if a["agreementId"] not in consented]
        if missing:
            raise Problem(MODULE, 409, "roles-without-consent", missing=missing, retryable=False)
        if not c["bpn"]:
            raise Problem(MODULE, 409, "invalid-state", detail="no BPN", retryable=False)
        c["status"], c["companyRoles"] = "ACTIVE", sorted(set(c["requestedRoles"]))
        events.publish({"eventType": "CompanyActivated", "companyId": c["id"], "bpn": c["bpn"],
                        "companyRoles": c["companyRoles"]})
    return standin.step_reply(body, {"companyId": c["id"], "status": c["status"]})


@router.post("/steps/reject-company")
def reject_company(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/reject-company")):
    c = companies.get(body.get("companyId"))
    if c and c["status"] == "REQUESTED":
        c["status"] = "REJECTED"
        events.publish({"eventType": "CompanyRejected", "companyId": c["id"]})
    return standin.step_reply(body)


@router.get("/internal/companies/{companyId}")
def query_company(companyId: str, _: dict = standin.perm(MODULE, "GET", "/internal/companies/{companyId}")):
    c = companies.get(companyId)
    if not c:
        raise Problem(MODULE, 404, "not-found")
    return _view(c)


@router.get("/osp/companies/{companyId}")
def osp_company(companyId: str, _: dict = standin.perm(MODULE, "GET", "/osp/companies/{companyId}")):
    return _view(_get(companyId)) | {"requestedRoles": companies[companyId]["requestedRoles"]}


@router.get("/companies/{companyId}")
def own_company(companyId: str, claims: dict = standin.perm(MODULE, "GET", "/companies/{companyId}")):
    standin.company_scope(MODULE, claims, companyId)
    return _view(_get(companyId))
