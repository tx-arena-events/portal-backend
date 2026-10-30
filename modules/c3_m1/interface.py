# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C3-M1 stand-in: seed of the OSP company, the onboarding steps and the
query `queryCompany` for other modules and Process Orchestration. In memory.

`create-company` reads the application from C1-M2 (`ApplicationInternal` as C1-M2 specifies it)
and maps it as C3-M1 11.1 describes. Consents are mapped from `agreementKey` + `version` to
`agreementId` through the shared catalogue `testdata/agreements.json` (provisional, OP-46).
"""
from __future__ import annotations

import datetime as dt

from portal_common import events, neighbours, settings, standin
from portal_common.problems import Problem
from tools import testdata

MODULE = "c3-m1"
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


def get_company(cid: str | None) -> dict:
    if cid not in companies:
        raise Problem(MODULE, 404, "not-found", retryable=False)
    return companies[cid]


def view(c: dict) -> dict:
    return {k: v for k, v in c.items() if k not in ("requestedRoles", "consents")}


def create_company(input: dict) -> dict:
    cid = input.get("companyId")
    if cid not in companies:
        app = neighbours.query(MODULE, "c1-m2", "get_application_internal", applicationId=input.get("applicationId"))
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
        events.publish({"eventType": "CompanyCreated", "companyId": cid, "applicationId": input.get("applicationId"),
                        "coreDataSource": companies[cid]["coreDataSource"]})
    return standin.step_reply(input, {"companyId": cid, "status": companies[cid]["status"]})


def assign_identifier(input: dict) -> dict:
    c = get_company(input.get("companyId"))
    if not input.get("bpn"):
        raise Problem(MODULE, 400, "validation", detail="bpn", retryable=False)
    if c["bpn"] != input["bpn"]:
        c["bpn"] = input["bpn"]
        events.publish({"eventType": "CompanyIdentifierAssigned", "companyId": c["id"], "bpn": c["bpn"]})
    return standin.step_reply(input, {"companyId": c["id"], "bpn": c["bpn"]})


def activate_company(input: dict) -> dict:
    c = get_company(input.get("companyId"))
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
    return standin.step_reply(input, {"companyId": c["id"], "status": c["status"]})


def reject_company(input: dict) -> dict:
    c = companies.get(input.get("companyId"))
    if c and c["status"] == "REQUESTED":
        c["status"] = "REJECTED"
        events.publish({"eventType": "CompanyRejected", "companyId": c["id"]})
    return standin.step_reply(input)


def query_company(*, companyId: str) -> dict:
    c = companies.get(companyId)
    if not c:
        raise Problem(MODULE, 404, "not-found")
    return view(c)


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
