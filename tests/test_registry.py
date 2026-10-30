# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Module registry and installation app: every module and every operation of the spec is mounted."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import BASE, bearer, person_token
from portal_common import problems, spec, standin
from scaffold import app as scaffold_app
from scaffold import registry


def test_every_spec_module_has_a_folder_and_a_kind():
    kinds = {e.id: e.kind for e in registry.load()}
    assert set(kinds) == set(spec.modules())
    assert kinds["c12-m1"] == "provided"
    # every other module is a stand-in or the module of a team (no STANDIN, no PROVIDED)
    assert {k for k, v in kinds.items() if v not in ("stand-in", "team")} == {"c12-m1"}
    assert [e.id for e in registry.load()][-2:] == ["c11-m1", "c12-m1"]   # subscribers last


def test_every_spec_operation_is_mounted_once():
    app = scaffold_app.create_app()
    paths = TestClient(app, base_url=BASE).get("/openapi.json").json()["paths"]
    for mid, ms in spec.modules().items():
        mounted = {(m.upper(), p) for p, ops in paths.items() for m in ops if p.startswith(ms.base_path + "/")}
        wanted = {(op.method, ms.base_path + op.path) for op in ms.operations}
        assert wanted <= {(m, p) for m, p in mounted}, f"{mid}: {sorted(wanted - mounted)[:3]}"


def test_unimplemented_operation_answers_501_with_permission_check():
    installation = TestClient(scaffold_app.create_app(), base_url=BASE)   # also serves the test IAM
    # the fallback of a module without own routes, independent of whether a team replaced C3-M1
    app = FastAPI()
    problems.install(app)
    app.include_router(standin.fallback_router("c3-m1"))
    c = TestClient(app, base_url=BASE)
    r = c.get("/api/c3-m1/osp/change-requests", headers=bearer(person_token("rolf")))
    assert r.status_code == 501 and r.headers["content-type"].startswith("application/problem+json")
    r = c.get("/api/c3-m1/osp/change-requests", headers=bearer(person_token("lieschen")))
    assert r.status_code == 403
    assert installation.get("/scaffold/modules").json()[-1] == {"module": "c12-m1", "kind": "provided"}
