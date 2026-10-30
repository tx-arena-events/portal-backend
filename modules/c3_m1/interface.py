# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C3-M1 stand-in: seed of the OSP company, the onboarding steps and the
queries `queryCompany`, `queryCompanies` and `findIdentifierMatches` for other modules and Process
Orchestration. In memory.

`create-company` reads the application from C1-M2 (`ApplicationInternal` as C1-M2 specifies it)
and maps it as C3-M1 11.1 describes. Consents are mapped from `agreementKey` + `version` to
`agreementId` through the shared catalogue `testdata/agreements.json` (provisional, OP-46).
"""
from __future__ import annotations

import datetime as dt
import re

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
            "coreDataSource": "EBW" if app["verification"] == "EBW" else "MANUAL",
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


def query_companies(*, id: list[str] | str | None = None, bpn: list[str] | str | None = None, **_other) -> dict:
    """9.2: companies by ID or BPN, `notFound` lists the values without a company."""
    ids = [id] if isinstance(id, str) else id or []
    bpns = [bpn] if isinstance(bpn, str) else bpn or []
    items = [c for c in companies.values() if c["id"] in ids or (c["bpn"] and c["bpn"] in bpns)]
    found = {c["id"] for c in items} | {c["bpn"] for c in items}
    return {"items": [view(c) for c in items], "notFound": [v for v in ids + bpns if v not in found]}


def _ident_key(value: str) -> str:
    """Comparison per type in upper case without spaces, `.`, `-`, `/` (C1-M3 11.2)."""
    return re.sub(r"[\s.\-/]", "", str(value)).upper()


def find_identifier_matches(*, identifiers: list[dict], excludeCompanyId: str | None = None,
                            statuses: list[str] | None = None, **_other) -> dict:
    """9.3 without the name comparison: companies with one of the identifiers (BPN as an identifier too)."""
    wanted = {(i["type"], _ident_key(i["value"])) for i in identifiers}
    matches = []
    for c in companies.values():
        if c["id"] == excludeCompanyId or (statuses and c["status"] not in statuses):
            continue
        own = {(i["type"], _ident_key(i["value"])) for i in c["identifiers"]} | ({("BPN", _ident_key(c["bpn"]))} if c["bpn"] else set())
        on = sorted({t for t, _ in wanted & own})
        if on:
            matches.append({"companyId": c["id"], "name": c["name"], "status": c["status"], "bpn": c["bpn"],
                            "matchedOn": on})
    return {"matches": matches}


def role_view(c: dict) -> dict:
    """`Company` of the paths for people (7.1, 7.9): company roles with their status."""
    roles = [{"companyRole": r, "status": "ACTIVE"} for r in c["companyRoles"]] + \
        [{"companyRole": r, "status": "REQUESTED"} for r in c["requestedRoles"] if r not in c["companyRoles"]]
    return view(c) | {"companyRoles": roles, "dataspaceName": (settings.get("dataspace") or {}).get("name")}


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
