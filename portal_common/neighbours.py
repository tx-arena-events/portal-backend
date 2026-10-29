# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Calls to neighbour modules (queries) with a service token and the error rule of FRAMEWORK 4.5.

Address of a neighbour (FRAMEWORK 4.7), one notation for all modules:
1. `<MODULE>_URL`, e.g. `C3_M1_URL`;
2. otherwise the entry in `MODULE_URLS` (JSON, e.g. `{"c3-m1": "in-process"}`, C12-M1 section 4);
3. otherwise `MODULE_URLS_DEFAULT` (the scaffold sets `in-process`).
Values: empty = built-in stand-in of the caller, `in-process` = this installation (`BASE_URL`),
otherwise the HTTP address of the split-out module.
"""
from __future__ import annotations

import json
import os

import httpx

from . import http, settings, spec, tokens
from .problems import Problem, from_neighbour

IN_PROCESS = "in-process"


def address(target: str) -> str:
    env = os.environ.get(target.upper().replace("-", "_") + "_URL")
    if env is None:
        env = json.loads(os.environ.get("MODULE_URLS") or "{}").get(target)
    if env is None:
        env = os.environ.get("MODULE_URLS_DEFAULT", "")
    env = env.strip()
    return settings.base_url() if env == IN_PROCESS else env.rstrip("/")


def call(caller: str, target: str, method: str, path: str, *, json_body: object = None, params: dict | None = None,
         allow: tuple[int, ...] = (404,)) -> httpx.Response:
    """Calls `{address}{base path of target}{path}`. 2xx and the statuses in `allow` are returned;
    every other answer raises a `Problem` of the caller (502 neighbour-rejected / upstream-error)."""
    base = address(target)
    if not base:
        raise Problem(caller, 502, "neighbour-not-configured", detail=f"no address for {target}", retryable=False)
    url = base + spec.module(target).base_path + path
    try:
        r = http.request(method, url, json=json_body, params=params,
                         headers={"Authorization": "Bearer " + tokens.service_token(caller)})
    except httpx.HTTPError as e:
        raise Problem(caller, 502, "upstream-error", detail=f"{target}: {type(e).__name__}", retryable=True) from e
    if r.is_success or r.status_code in allow:
        return r
    try:
        body = r.json()
    except json.JSONDecodeError:
        body = None
    raise from_neighbour(caller, target, r.status_code, body)


def get_json(caller: str, target: str, path: str, params: dict | None = None):
    """GET a query; returns the JSON body or None for 404."""
    r = call(caller, target, "GET", path, params=params)
    return None if r.status_code == 404 else r.json()
