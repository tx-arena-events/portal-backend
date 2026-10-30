# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""portal_common: spec access, settings, tokens, service tokens, Problem Details, neighbours, event relay."""
import json
import threading
import time

import httpx
import jwt
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from conftest import BASE, bearer, person_token
from portal_common import events, neighbours, problems, settings, spec, standin, tokens
from portal_common import http as phttp
from portal_common.problems import Problem
from tools import test_iam


def _iam_app() -> FastAPI:
    app = FastAPI()
    app.include_router(test_iam.router)
    phttp.register_local(BASE, app)
    return app


# ---------------------------------------------------------------- spec
def test_spec_lists_every_module_with_api_base():
    ids = set(spec.modules())
    assert {"c1-m2", "c1-m3", "c2-m2", "c2-m3", "c3-m1", "c3-m2", "c4-m1", "c4-m2",
            "c6-m1", "c8-m1", "c10-m1", "c11-m1", "c12-m1"} <= ids
    assert "c5-m1" not in ids  # standards-based paths, no /api/<module>


def test_service_permissions_come_from_x_permission():
    perms = spec.service_permissions()
    assert "execute_steps_c3_m1" in perms and "query_c1_m2" in perms
    lookup = spec.module("c8-m1").find("GET", "/internal/offers/123")
    assert lookup is not None and lookup.permission in perms


def test_find_prefers_literal_segments():
    op = spec.module("c2-m3").find("GET", "/companies/abc/technical-users/assignable-roles")
    assert op.path.endswith("/assignable-roles")


# ---------------------------------------------------------------- settings
def test_settings_from_settings_file(tmp_path, monkeypatch):
    assert settings.get("ospName") == "OSP A"
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"ospId": "x", "ospName": "B"}))
    monkeypatch.setenv("SETTINGS_FILE", str(f))
    settings.reset()
    with pytest.raises(ValueError, match="ospCompanyId"):
        settings.load()


# ---------------------------------------------------------------- tokens
def _protected_app() -> TestClient:
    app = _iam_app()
    r = APIRouter(prefix="/api/x", route_class=problems.route_class("x"))

    @r.get("/thing")
    def thing(claims: dict = Depends(tokens.require("x", "register_company"))):
        return {"sub": claims["sub"]}

    app.include_router(r)
    return TestClient(app, base_url=BASE)


def test_person_token_is_accepted_and_permission_checked():
    c = _protected_app()
    r = c.get("/api/x/thing", headers=bearer(person_token("erika")))
    assert r.status_code == 200 and r.json()["sub"] == "5a1c0001-0000-4000-8000-000000000001"
    otto_claims = tokens.verify(person_token("otto"))
    assert "view_processes" in tokens.permissions(otto_claims) and "company_id" not in otto_claims
    pia = tokens.verify(person_token("pia"))
    assert pia["company_id"] == "11111111-1111-4111-8111-111111111111" and "manage_offers" in tokens.permissions(pia)


def test_missing_token_401_missing_permission_403():
    c = _protected_app()
    assert c.get("/api/x/thing").status_code == 401
    iam = test_iam.iam()
    iam.users[iam.find("lieschen")]["roles"] = set()
    r = c.get("/api/x/thing", headers=bearer(person_token("lieschen")))
    assert r.status_code == 403 and r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["type"] == "urn:x:problem:forbidden"


def test_token_of_another_installation_is_rejected():
    c = _protected_app()
    iam = test_iam.iam()
    foreign = jwt.encode({"iss": "http://osp-b.test/auth/realms/CX-Central", "aud": "portal-core", "sub": "x",
                          "exp": int(time.time()) + 60, "typ": "Bearer",
                          "resource_access": {"portal-core": {"roles": ["register_company"]}}},
                         iam.key, algorithm="RS256", headers={"kid": iam.public_jwk["kid"]})
    assert c.get("/api/x/thing", headers=bearer(foreign)).status_code == 401


def test_technical_user_token_has_no_portal_core_audience():
    _iam_app()
    iam = test_iam.iam()
    secret = iam.create_technical_user("sa-test-u1", "c1", "BPNL1", ["Dataspace Discovery"])
    status, body = iam.token_request({"grant_type": "client_credentials", "client_id": "sa-test-u1",
                                      "client_secret": secret})
    assert status == 200
    with pytest.raises(jwt.InvalidAudienceError):
        tokens.verify(body["access_token"])


def test_service_token_has_step_and_query_permissions_of_the_spec():
    _iam_app()
    claims = tokens.verify(tokens.service_token("c12-m1"))
    assert claims["azp"] == "svc-c12-m1"
    assert spec.service_permissions() <= tokens.permissions(claims)
    assert tokens.service_token("c12-m1") == tokens.service_token("c12-m1")  # cached


# ---------------------------------------------------------------- Problem Details
class Body(BaseModel):
    name: str


def _problem_app() -> TestClient:
    app = FastAPI()
    problems.install(app)
    r = APIRouter(prefix="/api/y", route_class=problems.route_class("y"))

    @r.post("/things")
    def create(body: Body):
        return body

    @r.post("/steps/boom")
    def boom():
        raise RuntimeError("bug")

    @r.post("/steps/conflict")
    def conflict():
        raise Problem("y", 409, "invalid-state")

    app.include_router(r)
    return TestClient(app, raise_server_exceptions=False)


def test_validation_is_400_not_422():
    r = _problem_app().post("/api/y/things", json={})
    assert r.status_code == 400 and r.json()["type"] == "urn:y:problem:validation" and r.json()["errors"]


def test_unhandled_error_is_500_problem_and_steps_carry_retryable():
    c = _problem_app()
    r = c.post("/api/y/steps/boom")
    assert r.status_code == 500 and r.json()["type"] == "urn:y:problem:internal-error" and r.json()["retryable"] is True
    r = c.post("/api/y/steps/conflict")
    assert r.status_code == 409 and r.json()["retryable"] is False
    assert c.get("/nowhere").headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("status,name,retryable", [(400, "neighbour-rejected", False), (401, "neighbour-rejected", False),
                                                   (403, "neighbour-rejected", False), (503, "upstream-error", True),
                                                   (None, "upstream-error", True)])
def test_neighbour_error_rule(status, name, retryable):
    p = problems.from_neighbour("c3-m1", "c1-m2", status)
    assert (p.status, p.name, p.ext["retryable"]) == (502, name, retryable)


# ---------------------------------------------------------------- neighbours
def test_address_resolution(monkeypatch):
    assert neighbours.address("c3-m1") == BASE
    monkeypatch.setenv("MODULE_URLS", json.dumps({"c3-m1": "", "c8-m1": "http://offers:9000/"}))
    assert neighbours.address("c3-m1") == "" and neighbours.address("c8-m1") == "http://offers:9000"
    assert neighbours.is_standin("c3-m1") and not neighbours.is_standin("c8-m1") and not neighbours.is_standin("c1-m2")
    monkeypatch.setenv("C3_M1_URL", "in-process")
    assert neighbours.address("c3-m1") == BASE and not neighbours.is_standin("c3-m1")


def test_neighbour_query_over_http_sends_service_token_and_maps_errors(monkeypatch):
    monkeypatch.setenv("C1_M2_URL", BASE)   # an HTTP address (here served in the same process)
    app = _iam_app()
    seen = {}

    @app.get("/api/c1-m2/internal/applications/{aid}")
    def get_app(aid: str, claims: dict = Depends(tokens.require("c1-m2", "query_c1_m2"))):
        seen["azp"] = claims["azp"]
        if aid == "teapot":
            raise Problem("c1-m2", 403)
        return {"id": aid}

    problems.install(app, "c1-m2")
    assert neighbours.query("c3-m1", "c1-m2", "get_application_internal", applicationId="a1") == {"id": "a1"}
    assert seen["azp"] == "svc-c3-m1"
    with pytest.raises(Problem) as e:
        neighbours.query("c3-m1", "c1-m2", "get_application_internal", applicationId="teapot")
    assert e.value.name == "neighbour-rejected" and e.value.ext["retryable"] is False


def test_neighbour_query_in_process_calls_the_interface_function(monkeypatch):
    from modules.c1_m2 import interface as c1_m2
    seen = {}

    def get_application_internal(*, applicationId):
        seen["applicationId"] = applicationId
        if applicationId == "teapot":
            raise Problem("c1-m2", 403)
        if applicationId == "gone":
            raise Problem("c1-m2", 404)
        return {"id": applicationId}

    monkeypatch.setattr(c1_m2, "get_application_internal", get_application_internal)
    assert neighbours.in_process("c1-m2")
    assert neighbours.query("c3-m1", "c1-m2", "get_application_internal", applicationId="a1") == {"id": "a1"}
    assert seen == {"applicationId": "a1"}   # no HTTP, no token
    assert neighbours.query("c3-m1", "c1-m2", "get_application_internal", applicationId="gone") is None
    with pytest.raises(Problem) as e:   # the same error rule as over HTTP
        neighbours.query("c3-m1", "c1-m2", "get_application_internal", applicationId="teapot")
    assert (e.value.name, e.value.ext["neighbourType"]) == ("neighbour-rejected", "urn:c1-m2:problem:forbidden")
    with pytest.raises(KeyError):
        neighbours.query("c3-m1", "c1-m2", "get_application")


def test_neighbour_unreachable_is_retryable(monkeypatch):
    _iam_app()
    monkeypatch.setenv("C1_M2_URL", "http://127.0.0.1:9")
    with pytest.raises(Problem) as e:
        neighbours.query("c3-m1", "c1-m2", "get_application_internal", applicationId="a1")
    assert e.value.name == "upstream-error" and e.value.ext["retryable"] is True


# ---------------------------------------------------------------- event relay
def test_publish_returns_before_subscribers_run_and_failures_are_isolated():
    relay = events.Relay()
    got, gate = [], threading.Event()

    def slow(ev):
        gate.wait(2)
        got.append(("slow", ev["eventType"]))

    def broken(ev):
        raise RuntimeError("subscriber bug")

    relay.subscribe("slow", slow)
    relay.subscribe("broken", broken)
    relay.subscribe("fine", lambda ev: got.append(("fine", ev["eventType"])))
    relay.start()
    t0 = time.time()
    ev = relay.publish({"eventType": "CompanyCreated", "companyId": "c1"})
    assert time.time() - t0 < 0.5 and ev["eventId"] and ev["occurredAt"]
    gate.set()
    assert relay.wait_idle(5)
    assert got == [("slow", "CompanyCreated"), ("fine", "CompanyCreated")]
    assert relay.failures[0]["subscriber"] == "broken"
    relay.stop()


def test_events_never_carry_secrets():
    with pytest.raises(ValueError):
        events.Relay().publish({"eventType": "X", "clientSecret": "s"})


def test_drain_without_thread():
    relay = events.Relay()
    got = []
    relay.subscribe("a", got.append)
    relay.publish({"eventType": "A"})
    assert got == []
    relay.drain()
    assert got[0]["eventType"] == "A"


# ---------------------------------------------------------------- spec-driven stand-in
def test_fallback_router_mounts_every_spec_operation():
    app = _iam_app()
    implemented = APIRouter(prefix="/api/c3-m2")

    @implemented.post("/steps/register-did")
    def own():
        return {"own": True}

    app.include_router(implemented)
    app.include_router(standin.fallback_router("c3-m2", implemented))
    c = TestClient(app, base_url=BASE)
    tok = bearer(tokens.service_token("c12-m1"))
    r = c.post("/api/c3-m2/steps/transfer-company", headers=tok,   # stand-in interface function of C3-M2
               json={"processId": "p", "stepRunId": "s", "companyId": "c", "bpn": "BPNL000000000001"})
    assert r.status_code == 200 and r.json() == {"stepRunId": "s", "state": "DONE", "result": {"bpn": "BPNL000000000001"}}
    assert c.post("/api/c3-m2/steps/register-did", headers=tok, json={}).json() == {"own": True}
    r = c.get("/api/c3-m2/companies/c", headers=bearer(person_token("otto")))
    assert r.status_code == 501 and r.json()["type"] == "urn:c3-m2:problem:not-implemented"
    assert c.post("/api/c3-m2/steps/register-company", json={}).status_code == 401
    paths = c.get("/openapi.json").json()["paths"]
    n = sum(len(v) for k, v in paths.items() if k.startswith("/api/c3-m2"))
    assert n == len(spec.module("c3-m2").operations)


def test_bpn_derivation_matches_c3_m2_11_3():
    assert standin.derive_bpn("EBWOID-DE-0000000001").startswith("BPNL") and len(standin.derive_bpn("x")) == 16
