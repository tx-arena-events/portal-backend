# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Read-only access to the module specs (`openapi.yaml` per module folder, `flows.yaml`).

Module identifiers, base paths and permissions come from the spec, not from code: a module is
every folder with an `openapi.yaml` whose server URL is `/api/<module>` (FRAMEWORK 4.1). When the
spec renames a permission, `service_token()`, the stand-ins and Process Orchestration follow it.
"""
from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
METHODS = ("get", "post", "put", "patch", "delete")


def spec_dir() -> Path:
    return Path(os.environ.get("SPEC_DIR") or ROOT / "spec")


def snake_case(name: str) -> str:
    """`create-company` -> `create_company`, `getApplicationInternal` -> `get_application_internal`."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return name.replace("-", "_").lower()


@dataclass(frozen=True)
class Operation:
    module: str
    method: str          # upper case
    path: str            # relative to the module base, e.g. /steps/create-company
    permission: str | None
    scope: str | None
    tags: tuple[str, ...]
    operation_id: str | None
    request_schema: dict | None = field(default=None, compare=False, hash=False)

    @property
    def is_step(self) -> bool:
        return self.path.startswith("/steps/")

    @property
    def is_query(self) -> bool:
        return self.path.startswith("/internal/")

    def matches(self, method: str, path: str) -> bool:
        if method.upper() != self.method:
            return False
        a, b = self.path.strip("/").split("/"), path.strip("/").split("/")
        return len(a) == len(b) and all(x == y or (x[:1] == "{" and x[-1:] == "}") for x, y in zip(a, b))

    @property
    def params(self) -> int:
        return self.path.count("{")

    @property
    def function_name(self) -> str | None:
        """Name of the function in `modules/<module>/interface.py`: a step by its step name, a query
        by its operationId, both in snake_case (`create-company` -> `create_company`,
        `getApplicationInternal` -> `get_application_internal`). None for other operations."""
        if self.is_step:
            return snake_case(self.path.removeprefix("/steps/"))
        if self.is_query and self.operation_id:
            return snake_case(self.operation_id)
        return None


@dataclass(frozen=True)
class ModuleSpec:
    id: str              # e.g. c3-m1
    folder: str          # e.g. C3-M1-CompanyProfile
    base_path: str       # e.g. /api/c3-m1
    operations: tuple[Operation, ...]
    document: dict = field(compare=False, hash=False, repr=False)

    @property
    def package(self) -> str:
        return self.id.replace("-", "_")

    def steps(self) -> dict[str, Operation]:
        return {op.path.removeprefix("/steps/"): op for op in self.operations if op.is_step}

    def find(self, method: str, path: str) -> Operation | None:
        found = [op for op in self.operations if op.matches(method, path)]
        return min(found, key=lambda op: op.params) if found else None  # literal segments win

    def resolve(self, schema: dict | None) -> dict:
        """Resolves local `$ref` and merges `allOf` into one object schema (properties, required)."""
        if not schema:
            return {}
        if "$ref" in schema:
            name = schema["$ref"].rsplit("/", 1)[-1]
            return self.resolve(self.document["components"]["schemas"][name])
        if "allOf" in schema:
            out: dict = {"type": "object", "properties": {}, "required": []}
            for part in schema["allOf"]:
                r = self.resolve(part)
                out["properties"].update(r.get("properties", {}))
                out["required"] += [x for x in r.get("required", []) if x not in out["required"]]
            return out
        return schema


@functools.lru_cache(maxsize=None)
def _load(directory: str) -> dict[str, ModuleSpec]:
    modules: dict[str, ModuleSpec] = {}
    for f in sorted(Path(directory).glob("*/openapi.yaml")):
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        url = ((doc.get("servers") or [{}])[0]).get("url", "")
        m = re.fullmatch(r"/api/([a-z0-9-]+)", url)
        if not m:
            continue  # C2-M5 (Keycloak) and C5-M1 (standards) have no /api/<module> base
        mid = m.group(1)
        ops = []
        for path, item in (doc.get("paths") or {}).items():
            for method in METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema")
                ops.append(Operation(mid, method.upper(), path, op.get("x-permission"), op.get("x-permission-scope"),
                                     tuple(op.get("tags") or ()), op.get("operationId"), body))
        modules[mid] = ModuleSpec(mid, f.parent.name, url, tuple(ops), doc)
    return modules


def modules() -> dict[str, ModuleSpec]:
    """All modules of the spec with an `/api/<module>` base, keyed by module id."""
    return _load(str(spec_dir()))


def module(mid: str) -> ModuleSpec:
    try:
        return modules()[mid]
    except KeyError:
        raise KeyError(f"module {mid!r} is not in the spec at {spec_dir()}") from None


def service_permissions(target: str | None = None) -> set[str]:
    """Permissions of all steps and queries (paths /steps/, /internal/) of one module or of all."""
    mods = [module(target)] if target else modules().values()
    return {op.permission for m in mods for op in m.operations if (op.is_step or op.is_query) and op.permission}


def readme(folder: str) -> str:
    return (spec_dir() / folder / "README.md").read_text(encoding="utf-8")


def flows_file() -> Path:
    return Path(os.environ.get("FLOWS_FILE") or spec_dir() / "C12-M1-ProcessOrchestration" / "flows.yaml")
