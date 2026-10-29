# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Spec-driven stand-ins.

`fallback_router(module)` mounts every operation of the module's `openapi.yaml` that the module
does not implement itself: the permission from `x-permission` is checked as in the real module;
a step answers `DONE` with a stand-in result, every other operation 501 `not-implemented`.
A team that replaces a stand-in therefore starts from a module where every path already exists.

`standin_result()` gives the stand-in results of steps that later steps need (`bpn`, `did`, the
ID of an instance client, ...) as C12-M1 README 11.3 lists them; Process Orchestration uses
the same table for its built-in stand-in. Results are derived from the input (`stepRunId` stays
the same across attempts), so a repeated call returns the same result (FRAMEWORK 4.4).
"""
from __future__ import annotations

import base64
import hashlib
import re
import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute

from . import spec, tokens
from .problems import Problem, route_class

NAMESPACE = uuid.UUID("6f1c3f0e-6a8e-4c7e-9a43-6f1d2b7c9a10")


def derive_bpn(key: str) -> str:
    """C3-M2 11.3: `BPNL` + first 12 characters of the Base32 SHA-256 hash of the key, upper case."""
    return "BPNL" + base64.b32encode(hashlib.sha256(key.encode()).digest()).decode()[:12].upper()


def stable_id(*parts: object) -> str:
    """Deterministic UUID for idempotent stand-in objects."""
    return str(uuid.uuid5(NAMESPACE, "|".join(str(p) for p in parts)))


def did_for(bpn_or_id: str) -> str:
    return f"did:web:wallet.example.org:{bpn_or_id}"


def standin_result(call: str, data: dict) -> dict:
    """Result of a step answered by a stand-in (C12-M1 README 11.3). `call` = `<module>.<step>`."""
    cid, run = data.get("companyId"), data.get("stepRunId")
    if call == "c3-m2.register-company":
        key = data.get("ebwoid") or data.get("euid") or next(
            (f"{i['type']}:{i['value']}" for i in data.get("identifiers") or []), cid or "")
        return {"bpn": derive_bpn(key)}
    if call == "c3-m2.transfer-company":
        return {"bpn": data.get("bpn")}
    if call == "c4-m2.create-wallet":
        return {"did": did_for(cid or "")}
    if call == "c4-m2.create-wallet-account":
        return {"walletAccountRef": run}
    if call == "c2-m2.create-instance-client":
        return {"id": run, "iamClientId": f"standin-{run}"}
    if call == "c2-m3.create-for-subscription":
        return {"accounts": []}
    return {}


def _norm(path: str) -> str:
    return re.sub(r"\{[^/}]+\}", "{}", path)


def _make_handler(op: spec.Operation):
    step = op.path.removeprefix("/steps/") if op.is_step else None

    async def handler(request: Request):
        tokens.check(request.headers.get("authorization"), op.module, op.permission)
        if step is None:
            raise Problem(op.module, 501, "not-implemented",
                          detail=f"{op.method} {op.path} is not implemented by the stand-in of {op.module}")
        try:
            data = await request.json()
        except ValueError:
            data = {}
        return {"stepRunId": data.get("stepRunId"), "state": "DONE",
                "result": standin_result(f"{op.module}.{step}", data)}

    handler.__name__ = f"standin_{op.operation_id or step or 'op'}"
    return handler


def fallback_router(module: str, implemented: APIRouter | None = None) -> APIRouter:
    """Router with every spec operation of `module` that `implemented` does not cover."""
    ms = spec.module(module)
    have = set()
    for r in (implemented.routes if implemented else []):
        if isinstance(r, APIRoute):
            have |= {(m, _norm(r.path)) for m in r.methods}
    out = APIRouter(prefix=ms.base_path, route_class=route_class(module), tags=[f"{module} (stand-in)"])
    for op in ms.operations:
        if (op.method, _norm(ms.base_path + op.path)) in have:
            continue
        out.add_api_route(op.path, _make_handler(op), methods=[op.method], summary=f"stand-in: {op.operation_id}")
    return out


# ---------------------------------------------------------------- helpers for hand-written stand-ins
def perm(module: str, method: str, path: str):
    """Dependency with the permission of the spec operation (`x-permission`); returns the claims."""
    return Depends(tokens.require_spec(module, method, path))


def step_reply(body: dict, result: dict | None = None, state: str = "DONE") -> dict:
    """Step answer of FRAMEWORK 4.4."""
    return {"stepRunId": body.get("stepRunId"), "state": state, "result": result or {}}


def company_scope(module: str, claims: dict, company_id: str | None) -> None:
    """Scope `company` (FRAMEWORK 4.3): another company's object stays invisible (404)."""
    if not company_id or claims.get("company_id") != company_id:
        raise Problem(module, 404, "not-found")


def router(module: str) -> APIRouter:
    """Router of a module with the base path of the spec and Problem Details."""
    return APIRouter(prefix=spec.module(module).base_path, route_class=route_class(module),
                     tags=[f"{module} (stand-in)"])
