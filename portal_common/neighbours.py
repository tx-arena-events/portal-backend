# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Queries to neighbour modules with the error rule of FRAMEWORK 4.5.

Address of a neighbour (FRAMEWORK 4.7), one notation for all modules:
1. `<MODULE>_URL`, e.g. `C3_M1_URL`;
2. otherwise the entry in `MODULE_URLS` (JSON, e.g. `{"c3-m1": "in-process"}`, C12-M1 section 4);
3. otherwise `MODULE_URLS_DEFAULT` (the scaffold sets `in-process`).
Values: empty = built-in stand-in of the caller, `in-process` = direct call of the interface
function `modules.<module>.interface.<function>` (no token, FRAMEWORK 4.2), otherwise the HTTP
address of the split-out module (with the caller's service token).

    company = neighbours.query("c4-m2", "c3-m1", "query_company", companyId=cid)
"""
from __future__ import annotations

import json
import logging
import os
from urllib.parse import quote

import httpx
from fastapi.encoders import jsonable_encoder

from . import http, settings, spec, standin, tokens
from .problems import Problem, from_neighbour, to_problem

IN_PROCESS = "in-process"
log = logging.getLogger("portal.neighbours")


def _configured(target: str) -> str:
    env = os.environ.get(target.upper().replace("-", "_") + "_URL")
    if env is None:
        env = json.loads(os.environ.get("MODULE_URLS") or "{}").get(target)
    if env is None:
        env = os.environ.get("MODULE_URLS_DEFAULT", "")
    return env.strip()


def in_process(target: str) -> bool:
    return _configured(target) == IN_PROCESS


def is_standin(target: str) -> bool:
    """True if the address of `target` is empty: the caller answers with its built-in stand-in
    (README section 11 of the caller) instead of calling `target`."""
    return not _configured(target)


def address(target: str) -> str:
    """HTTP address of `target` (`BASE_URL` for `in-process`), empty for the built-in stand-in."""
    env = _configured(target)
    return settings.base_url() if env == IN_PROCESS else env.rstrip("/")


def operation(target: str, function: str) -> spec.Operation:
    """The query of `target` whose operationId in snake_case is `function`."""
    op = next((o for o in spec.module(target).operations if o.is_query and o.function_name == function), None)
    if op is None:
        raise KeyError(f"{target} has no query {function!r} under /internal in its openapi.yaml")
    return op


def query(caller: str, target: str, function: str, /, **params):
    """Calls the query `function` of `target` with the path and query parameters of its openapi.yaml
    (for a query with a JSON body: its fields). Returns the JSON body, or None for 404. Every other
    error raises a `Problem` of the caller: 4xx -> 502 neighbour-rejected, otherwise 502 upstream-error."""
    op = operation(target, function)
    mode = _configured(target)
    if not mode:
        raise Problem(caller, 502, "neighbour-not-configured", detail=f"no address for {target}", retryable=False)
    if mode == IN_PROCESS:
        try:
            return jsonable_encoder(standin.interface_function(target, function)(**params))
        except Exception as e:  # noqa: BLE001 - the same error rule as over HTTP
            p = to_problem(target, e, False)
            if p.status == 404:
                return None
            raise from_neighbour(caller, target, p.status, p.body()) from e
    path, rest = op.path, {}
    for k, v in params.items():
        if "{" + k + "}" in path:
            path = path.replace("{" + k + "}", quote(str(v), safe=""))
        elif v is not None:
            rest[k] = v
    # MINIMAL: an operation with a JSON body takes every non-path parameter from the body
    # (true for every query of the spec today); mixed query + body parameters would need the spec.
    body = rest if op.request_schema else None
    try:
        r = http.request(op.method, mode.rstrip("/") + spec.module(target).base_path + path,
                         json=body or None, params=None if op.request_schema else rest,
                         headers={"Authorization": "Bearer " + tokens.service_token(caller)})
    except httpx.HTTPError as e:
        raise Problem(caller, 502, "upstream-error", detail=f"{target}: {type(e).__name__}", retryable=True) from e
    if r.status_code == 404:
        return None
    try:
        answer = r.json()
    except json.JSONDecodeError:
        answer = None
    if r.is_success:
        return answer
    raise from_neighbour(caller, target, r.status_code, answer)
