# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Spec-driven stand-ins and the interface functions of the modules.

`interface_function(module, name)` is `modules.<module>.interface.<name>`, the in-process entry
point of a step or query (FRAMEWORK 1). `missing_interface(module)` is the `__getattr__` of an
`interface.py`: every step and query of the spec that the module does not define yet exists as a
stand-in function (a step answers `DONE` with a stand-in result, a query raises 501).

`fallback_router(module)` mounts every operation of the module's `openapi.yaml` that the module's
router does not implement itself: the permission from `x-permission` is checked as in the real
module; steps and queries call the module's interface function, every other operation answers
501 `not-implemented`. A team that replaces a stand-in therefore starts from a module where every
path and every interface function already exists.

`standin_result()` gives the stand-in results of steps that later steps need (`bpn`, `did`, the
ID of an instance client, ...) as C12-M1 README 11.3 lists them; Process Orchestration uses
the same table for its built-in stand-in. Results are derived from the input (`stepRunId` stays
the same across attempts), so a repeated call returns the same result (FRAMEWORK 4.4).
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import re
import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

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


def _interface_operations(module: str) -> dict[str, spec.Operation]:
    return {op.function_name: op for op in spec.module(module).operations if op.function_name}


def _standin_function(op: spec.Operation) -> Callable:
    """Stand-in of one interface function: a step answers DONE with the results of C12-M1 11.3,
    a query raises 501 (the same answers as the fallback routes over HTTP)."""
    if op.is_step:
        call = f"{op.module}.{op.path.removeprefix('/steps/')}"

        def step(input: dict) -> dict:
            return {"stepRunId": input.get("stepRunId"), "state": "DONE", "result": standin_result(call, input)}
        fn = step
    else:
        def query(**params):
            raise Problem(op.module, 501, "not-implemented",
                          detail=f"{op.method} {op.path} is not implemented by the stand-in of {op.module}")
        fn = query
    fn.__name__ = fn.__qualname__ = op.function_name
    return fn


def missing_interface(module: str) -> Callable[[str], Callable]:
    """`__getattr__` for `modules/<module>/interface.py`: every step and query of the spec that the
    module does not define yet, as a stand-in function. Delete the line once all are implemented."""
    package = f"modules.{spec.module(module).package}.interface"

    def __getattr__(name: str) -> Callable:
        op = _interface_operations(module).get(name)
        if op is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        return _standin_function(op)

    return __getattr__


def interface_function(module: str, name: str) -> Callable:
    """`modules.<module>.interface.<name>`; for a module without a folder, the stand-in function."""
    package = f"modules.{spec.module(module).package}.interface"
    try:
        iface = importlib.import_module(package)
    except ModuleNotFoundError as e:
        if e.name not in (package, package.rsplit(".", 1)[0]):
            raise
        op = _interface_operations(module).get(name)
        if op is None:
            raise AttributeError(f"{module} has no step or query {name!r}") from None
        return _standin_function(op)
    return getattr(iface, name)


def _make_handler(op: spec.Operation):
    name = op.function_name

    async def handler(request: Request):
        tokens.check(request.headers.get("authorization"), op.module, op.permission)
        if name is None:
            raise Problem(op.module, 501, "not-implemented",
                          detail=f"{op.method} {op.path} is not implemented by the stand-in of {op.module}")
        fn = interface_function(op.module, name)
        try:
            body = await request.json()
        except ValueError:
            body = {}
        if op.is_step:
            return await run_in_threadpool(fn, body if isinstance(body, dict) else {})
        # MINIMAL: one value per query parameter; a list-valued parameter needs a route of the module.
        params = dict(request.path_params) | dict(request.query_params)
        if op.request_schema and isinstance(body, dict):
            params |= body
        return await run_in_threadpool(fn, **params)

    handler.__name__ = f"standin_{op.operation_id or name or 'op'}"
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
