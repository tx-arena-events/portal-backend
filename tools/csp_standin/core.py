# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Core services of the CSP in memory: the logic behind the core services API (C3-M2 README 11.1).

Used by the shared CSP stand-in (app.py, several OSPs) and by the built-in stand-in of C3-M2
(README 11.3, one OSP). Every method takes the calling `ospId` and returns
(HTTP status, JSON body or None), the same contract as over HTTP.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable

from portal_common.standin import derive_bpn

MATCH_RANK = {"EBWOID": 0, "EUID": 1, "IDENTIFIER": 2, "NAME": 3}


def norm(value: str) -> str:
    # MINIMAL: normalisation as C3-M1 6.4 (upper case, without spaces . - /); the CSP's own rule is open
    return "".join(ch for ch in value.upper() if ch not in " .-/")


def problem(status: int, name: str, **ext) -> tuple[int, dict]:
    return status, {"type": f"urn:csp:problem:{name}", "title": name.replace("-", " "), "status": status, **ext}


class CoreServices:
    def __init__(self, osps: dict[str, dict], stub_companies: list[dict] | None = None,
                 notify: Callable[[str, dict], None] | None = None):
        self.osps = osps                      # ospId -> {"name", "installationUrl", "jwk", "iamTokenUrl"}
        self.notify = notify                  # (ospId, notification) -> None; None = no callbacks
        self.async_operations = False
        self.companies: dict[str, dict] = {}  # bpn -> record
        self.operations: dict[str, dict] = {}
        for c in stub_companies or []:
            self.companies[c["bpn"]] = {"ebwoid": None, "euid": None, "identifiers": [], "address": None,
                                        "memberActive": False, "did": None, **c, "connectors": {}}

    # ---------- helpers
    def _find_by_ids(self, ebwoid=None, euid=None, identifiers=()) -> list[tuple[dict, str]]:
        found: dict[str, str] = {}
        for c in self.companies.values():
            kinds = []
            if ebwoid and c.get("ebwoid") == ebwoid:
                kinds.append("EBWOID")
            if euid and c.get("euid") and norm(c["euid"]) == norm(euid):
                kinds.append("EUID")
            own = {(i["type"], norm(i["value"])) for i in c.get("identifiers") or []}
            if any((t, norm(v)) in own for t, v in identifiers):
                kinds.append("IDENTIFIER")
            if kinds:
                found[c["bpn"]] = min(kinds, key=MATCH_RANK.get)
        return [(self.companies[b], k) for b, k in found.items()]

    def home_check(self, caller: str, bpn: str) -> tuple[int, dict] | None:
        c = self.companies.get(bpn)
        if c is None:
            return problem(404, "unknown-bpn")
        if c["homeOspId"] != caller:
            return problem(403, "not-home-osp")
        return None

    def _send(self, osp_id: str, notification: dict) -> None:
        if self.notify:
            self.notify(osp_id, {"notificationId": str(uuid.uuid4()), **notification})

    # ---------- GET /companies (duplicate check)
    def find_companies(self, caller: str, ebwoid=None, euid=None, identifier=(), legalName=None, country=None):
        idents = [tuple(x.split(":", 1)) for x in identifier or () if ":" in x]
        if not (ebwoid or euid or idents or (legalName and country)):
            return problem(400, "validation", detail="ebwoid, euid, identifier or legalName with country")
        matches = {c["bpn"]: (c, k) for c, k in self._find_by_ids(ebwoid, euid, idents)}
        if legalName and country:
            for c in self.companies.values():
                if c["bpn"] not in matches and c["country"] == country and \
                        " ".join(c["legalName"].split()).casefold() == " ".join(legalName.split()).casefold():
                    matches[c["bpn"]] = (c, "NAME")
        return 200, [{"bpn": c["bpn"], "legalName": c["legalName"], "country": c["country"], "matchedBy": k,
                      "homeOspId": c["homeOspId"], "homeOspName": (self.osps.get(c["homeOspId"]) or {}).get("name"),
                      "memberActive": c["memberActive"]} for c, k in matches.values()]

    # ---------- POST /companies
    def register_company(self, caller: str, body: dict):
        ebwoid, euid, idents = body.get("ebwoid"), body.get("euid"), body.get("identifiers") or []
        if not body.get("legalName") or not body.get("country") or not (ebwoid or euid or idents):
            return problem(400, "validation", detail="legalName, country and at least one identifier")
        existing = self._find_by_ids(ebwoid, euid, [(i["type"], i["value"]) for i in idents])
        if existing:
            c = existing[0][0]
            if c["homeOspId"] != caller:
                return 409, {**problem(409, "registered-at-other-osp")[1], "bpn": c["bpn"], "homeOspId": c["homeOspId"]}
            return 201, {"bpn": c["bpn"]}   # C3-M2 open points: the CSP confirms the existing BPN
        key = ebwoid or euid or f'{idents[0]["type"]}:{idents[0]["value"]}'
        record = {"bpn": derive_bpn(key), "legalName": body["legalName"], "country": body["country"],
                  "ebwoid": ebwoid, "euid": euid, "identifiers": idents, "address": body.get("address"),
                  "homeOspId": caller, "memberActive": False, "did": None, "connectors": {}}
        if self.async_operations:
            return self._start_operation(caller, "register", record)
        self.companies[record["bpn"]] = record
        return 201, {"bpn": record["bpn"]}

    # ---------- POST /companies/{bpn}/transfer
    def transfer_company(self, caller: str, bpn: str, body: dict):
        c = self.companies.get(bpn)
        if c is None:
            return problem(404, "unknown-bpn")
        idents = [(i["type"], i["value"]) for i in body.get("identifiers") or []]
        if (body.get("ebwoid") or body.get("euid") or idents) and \
                not self._find_by_ids(body.get("ebwoid"), body.get("euid"), idents):
            return problem(403, "identifier-mismatch")   # MINIMAL: answer not specified
        if c["homeOspId"] == caller:
            return 201, {"bpn": bpn}
        if self.async_operations:
            return self._start_operation(caller, "transfer", {"bpn": bpn})
        self._apply_transfer(caller, bpn)
        return 201, {"bpn": bpn}

    def _apply_transfer(self, caller: str, bpn: str) -> None:
        old = self.companies[bpn]["homeOspId"]
        self.companies[bpn]["homeOspId"] = caller
        self._send(old, {"type": "CompanyTransferredAway", "bpn": bpn})

    # ---------- long-running operations (202 + callback)
    def _start_operation(self, caller: str, kind: str, payload: dict):
        op_id = str(uuid.uuid4())
        self.operations[op_id] = {"operationId": op_id, "kind": kind, "caller": caller, "payload": payload,
                                  "state": "RUNNING"}
        return 202, {"operationId": op_id}

    def complete_operation(self, op_id: str, outcome: str = "SUCCEEDED", reason: str | None = None):
        op = self.operations.get(op_id)
        if op is None or op["state"] != "RUNNING":
            return problem(404, "unknown-operation")
        op["state"] = outcome
        bpn = op["payload"]["bpn"]
        if outcome == "SUCCEEDED":
            if op["kind"] == "register":
                self.companies[bpn] = op["payload"]
            else:
                self._apply_transfer(op["caller"], bpn)
        note = {"type": "OperationCompleted", "operationId": op_id, "outcome": outcome}
        note |= {"bpn": bpn} if outcome == "SUCCEEDED" else {"reason": reason or "REJECTED_BY_CSP"}
        self._send(op["caller"], note)
        return 200, {"operationId": op_id, "state": outcome}

    # ---------- changing calls per BPN (home OSP only)
    def update_company(self, caller: str, bpn: str, body: dict):
        if err := self.home_check(caller, bpn):
            return err
        for f in ("legalName", "euid", "address"):
            if body.get(f) is not None:
                self.companies[bpn][f] = body[f]
        return 204, None

    def put_did(self, caller: str, bpn: str, body: dict):
        if err := self.home_check(caller, bpn):
            return err
        if not body.get("did"):
            return problem(400, "validation")
        self.companies[bpn]["did"] = body["did"]
        return 204, None

    def put_membership(self, caller: str, bpn: str, body: dict):
        if err := self.home_check(caller, bpn):
            return err
        if not isinstance(body.get("member"), bool):
            return problem(400, "validation")
        self.companies[bpn]["memberActive"] = body["member"]
        return 204, None

    def put_connector(self, caller: str, bpn: str, connector_id: str, body: dict):
        if err := self.home_check(caller, bpn):
            return err
        if not str(body.get("endpointUrl", "")).startswith("https://"):
            return problem(400, "validation", detail="endpointUrl must be https")
        self.companies[bpn]["connectors"][connector_id] = body["endpointUrl"]
        return 204, None

    def delete_connector(self, caller: str, bpn: str, connector_id: str):
        if err := self.home_check(caller, bpn):
            return err
        self.companies[bpn]["connectors"].pop(connector_id, None)
        return 204, None

    # ---------- read side (not in the proposal 11.1)
    def directory(self, bpn: str):
        c = self.companies.get(bpn)
        if c is None or not c.get("did"):
            return problem(404, "unknown-bpn")
        return 200, {"bpn": bpn, "did": c["did"]}

    def discovery(self, bpn: str):
        c = self.companies.get(bpn)
        if c is None:
            return problem(404, "unknown-bpn")
        items = [{"connectorId": k, "endpointUrl": v} for k, v in c["connectors"].items()] if c["memberActive"] else []
        return 200, {"bpn": bpn, "connectors": items}

    # ---------- dispatcher with the paths of 11.1 (built-in stand-in of C3-M2)
    def dispatch(self, caller: str, method: str, path: str, body: dict | None = None, params: dict | None = None):
        parts = [p for p in path.split("/") if p]
        body, params = body or {}, params or {}
        if parts == ["companies"] and method == "GET":
            return self.find_companies(caller, **params)
        if parts == ["companies"] and method == "POST":
            return self.register_company(caller, body)
        if len(parts) == 2 and method == "PATCH":
            return self.update_company(caller, parts[1], body)
        if len(parts) == 3 and method == "POST" and parts[2] == "transfer":
            return self.transfer_company(caller, parts[1], body)
        if len(parts) == 3 and method == "PUT" and parts[2] == "did":
            return self.put_did(caller, parts[1], body)
        if len(parts) == 3 and method == "PUT" and parts[2] == "membership":
            return self.put_membership(caller, parts[1], body)
        if len(parts) == 4 and parts[2] == "connectors":
            if method == "PUT":
                return self.put_connector(caller, parts[1], parts[3], body)
            if method == "DELETE":
                return self.delete_connector(caller, parts[1], parts[3])
        return problem(404, "not-found")
