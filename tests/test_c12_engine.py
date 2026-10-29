# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""C12-M1 Process Orchestration against the acceptance cases of its README (section 13)."""
import datetime as dt
import uuid

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from conftest import BASE, bearer, person_token
from modules.c12_m1 import engine as eng
from modules.c12_m1 import router as c12_router
from portal_common import events, problems, spec, standin, tokens
from portal_common import http as phttp
from tools import test_iam

COMPANY = "5b0c7a52-3f7e-4b1e-9d0a-1c2d3e4f5a6b"
APPLICATION = "0f8e2d4c-6b1a-4c3e-8f7d-2a9b1c0d3e4f"
FAKE = "http://fake.test"


class Clock:
    def __init__(self):
        self.t = dt.datetime(2026, 11, 6, 9, 0, tzinfo=dt.UTC)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += dt.timedelta(seconds=seconds)


@pytest.fixture
def fake():
    """A neighbour whose step and lookup answers the test sets: fake.answers[path] = callable or (status, body)."""
    app = FastAPI()
    app.state.answers, app.state.calls = {}, []

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def any_path(path: str, request: Request):
        body = await request.json() if request.method == "POST" else dict(request.query_params)
        app.state.calls.append(("/" + path, body))
        ans = app.state.answers.get("/" + path)
        if callable(ans):
            ans = ans(body)
        status, payload = ans or (200, {"stepRunId": body.get("stepRunId"), "state": "DONE", "result": {}})
        from fastapi.responses import JSONResponse
        return JSONResponse(payload, status_code=status)

    phttp.register_local(FAKE, app)
    return app.state


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setenv("MODULE_URLS_DEFAULT", "")
    iam_app = FastAPI()
    iam_app.include_router(test_iam.router)
    phttp.register_local(BASE, iam_app)
    e = eng.reset()
    e.clock = Clock()
    return e


def ev(etype, **fields):
    return {"eventId": str(uuid.uuid4()), "eventType": etype, "occurredAt": "2026-11-06T09:30:00Z", **fields}


def submitted(kind="ONBOARDING", existing=False, **extra):
    return ev("ApplicationSubmitted", applicationId=APPLICATION, companyId=COMPANY, kind=kind,
              existingCompany=existing, applicantSub="ebw.test-erika", **extra)


def proc_of(e, flow):
    return next(p for p in e.processes.values() if p["flowKey"] == flow)


def states(p):
    return {s["stepKey"]: s["state"] for s in p["steps"]}


def step(p, key):
    return next(s for s in p["steps"] if s["stepKey"] == key)


def published(etype):
    return [x for x in events.RELAY.log if x["eventType"] == etype]


# ---------------------------------------------------------------- start-up check (6.2, case 1)
def test_flows_of_the_spec_pass_the_check(engine):
    assert len(engine.flows) >= 23 and engine.warnings == []


def test_unknown_step_stops_the_start(tmp_path):
    doc = yaml.safe_load(spec.flows_file().read_text())
    doc["flows"][0]["steps"][0]["call"] = "c3-m1.no-such-step"
    f = tmp_path / "flows.yaml"
    f.write_text(yaml.safe_dump(doc))
    with pytest.raises(eng.FlowError, match="flow onboarding, step create-company: unknown step"):
        eng.Engine(f)


def test_required_input_without_source_stops_the_start(tmp_path):
    doc = yaml.safe_load(spec.flows_file().read_text())
    doc["flows"][0]["steps"][0]["input"] = {}
    f = tmp_path / "flows.yaml"
    f.write_text(yaml.safe_dump(doc))
    with pytest.raises(eng.FlowError, match="required input"):
        eng.Engine(f)


# ---------------------------------------------------------------- onboarding with stand-ins (2, 3, 4)
def test_onboarding_end_to_end_with_builtin_standins(engine):
    engine.on_event(submitted())
    p = proc_of(engine, "onboarding")
    assert p["status"] == "WAITING"
    assert states(p)["create-company"] == "DONE" and states(p)["open-review"] == "DONE"
    engine.on_event(ev("ApplicationApproved", applicationId=APPLICATION, companyId=COMPANY, kind="ONBOARDING",
                       existingCompany=False))
    assert p["status"] == "COMPLETED", states(p)
    assert step(p, "register-company")["result"]["bpn"] == "BPNLIUK2REMLK3F4"   # C12-M1 14
    calls = dict(engine.call_log)
    assert calls["c3-m1.assign-identifier"]["bpn"] == "BPNLIUK2REMLK3F4"
    assert calls["c3-m2.register-did"]["did"] == f"did:web:wallet.example.org:{COMPANY}"
    # address mapped by the lookup grammar (6.8): C3-M1 address -> C3-M2 Address
    assert calls["c3-m2.register-company"]["address"] == {"streetAddress": "Musterstraße 1", "postalCode": "70569",
                                                          "locality": "Stuttgart", "country": "DE"}
    assert calls["c3-m2.register-company"]["ebwoid"] == "EBWOID-DE-0000000001"
    assert len(published("ProcessCompleted")) == 1
    assert published("CompanyRegisteredAtCsp") == []   # the stand-in never publishes events (11.3)


def test_same_event_twice_and_second_event_start_no_second_process(engine):
    e1 = submitted()
    engine.on_event(e1)
    engine.on_event(e1)
    engine.on_event(submitted())
    assert len([p for p in engine.processes.values() if p["flowKey"] == "onboarding"]) == 1


# ---------------------------------------------------------------- decline, reactivation (5, 6)
def test_declined_ends_onboarding_and_runs_declined_flow(engine):
    engine.on_event(submitted())
    engine.on_event(ev("ApplicationDeclined", applicationId=APPLICATION, companyId=COMPANY, kind="ONBOARDING",
                       existingCompany=False, reason="incomplete"))
    p = proc_of(engine, "onboarding")
    assert p["status"] == "ENDED" and states(p)["register-company"] == "SKIPPED"
    d = proc_of(engine, "application-declined")
    assert d["status"] == "COMPLETED" and states(d) == {"close-application": "DONE", "reject-company": "DONE"}


def test_reactivation_has_no_create_company(engine):
    engine.on_event(submitted(kind="REACTIVATION", existing=True))
    p = proc_of(engine, "reactivation")
    assert "create-company" not in states(p) and p["status"] == "WAITING"


# ---------------------------------------------------------------- long-running steps (7, 8), endOn rule (6.5)
def _approve(engine):
    engine.on_event(submitted())
    engine.on_event(ev("ApplicationApproved", applicationId=APPLICATION, companyId=COMPANY, kind="ONBOARDING",
                       existingCompany=False))


def test_running_step_completed_by_event_and_withdrawal_after_approval_does_not_end(engine, fake, monkeypatch):
    monkeypatch.setenv("C4_M2_URL", FAKE)
    fake.answers["/api/c4-m2/steps/create-wallet"] = lambda b: (200, {"stepRunId": b["stepRunId"], "state": "RUNNING"})
    _approve(engine)
    p = proc_of(engine, "onboarding")
    assert step(p, "create-wallet")["state"] == "AWAITING_EVENT" and p["status"] == "RUNNING"
    engine.on_event(ev("ApplicationWithdrawn", applicationId=APPLICATION, companyId=COMPANY, kind="ONBOARDING",
                       existingCompany=False))
    assert p["status"] == "RUNNING"   # decision already taken: only recorded (6.5 point 5)
    engine.on_event(ev("DataspaceWalletCreated", companyId=COMPANY, did="did:web:x:1"))
    assert p["status"] == "COMPLETED"
    assert step(p, "create-wallet")["result"]["did"] == "did:web:x:1"
    assert dict(engine.call_log)["c3-m2.register-did"]["did"] == "did:web:x:1"


def test_event_published_before_running_answer_still_completes(engine, fake, monkeypatch):
    monkeypatch.setenv("C4_M2_URL", FAKE)

    def early(b):
        events.publish({"eventType": "DataspaceWalletCreated", "companyId": COMPANY, "did": "did:web:early"})
        return 200, {"stepRunId": b["stepRunId"], "state": "RUNNING"}

    fake.answers["/api/c4-m2/steps/create-wallet"] = early
    events.RELAY.subscribe("c12", eng.on_event)
    _approve(engine)
    events.RELAY.drain()
    p = proc_of(engine, "onboarding")
    assert p["status"] == "COMPLETED" and step(p, "create-wallet")["result"]["did"] == "did:web:early"


def test_fails_on_event_fails_the_step(engine, fake, monkeypatch):
    monkeypatch.setenv("C3_M2_URL", FAKE)
    fake.answers["/api/c3-m2/steps/register-company"] = lambda b: (200, {"stepRunId": b["stepRunId"], "state": "RUNNING"})
    _approve(engine)
    engine.on_event(ev("CompanyRegistrationAtCspFailed", companyId=COMPANY, reason="registered-at-other-osp"))
    p = proc_of(engine, "onboarding")
    assert p["status"] == "FAILED" and step(p, "register-company")["state"] == "FAILED"
    assert published("ProcessStepFailed")[-1]["stepKey"] == "register-company"


# ---------------------------------------------------------------- retries (9, 10)
def test_retryable_error_three_times_then_success(engine, fake, monkeypatch):
    monkeypatch.setenv("C3_M1_URL", FAKE)
    answers = iter([(503, {"type": "urn:c3-m1:problem:upstream-error", "retryable": True})] * 3)
    fake.answers["/api/c3-m1/steps/create-company"] = lambda b: next(
        answers, (200, {"stepRunId": b["stepRunId"], "state": "DONE", "result": {}}))
    fake.answers["/api/c3-m1/internal/companies/" + COMPANY] = (404, {})
    engine.on_event(submitted())
    p = proc_of(engine, "onboarding")
    waits = []
    for _ in range(3):
        sr = step(p, "create-company")
        waits.append((sr["nextAttemptAt"] - engine.clock()).total_seconds())
        engine.clock.advance(waits[-1])
        engine.tick()
    sr = step(p, "create-company")
    assert waits == [5, 10, 20] and sr["attempts"] == 4 and sr["state"] == "DONE" and p["status"] == "WAITING"


def test_final_error_fails_after_one_attempt(engine, fake, monkeypatch):
    monkeypatch.setenv("C3_M1_URL", FAKE)
    fake.answers["/api/c3-m1/steps/create-company"] = (409, {"type": "urn:c3-m1:problem:invalid-state",
                                                             "retryable": False})
    engine.on_event(submitted())
    p = proc_of(engine, "onboarding")
    assert step(p, "create-company")["attempts"] == 1 and p["status"] == "FAILED"
    f = published("ProcessStepFailed")[-1]
    assert f["stepKey"] == "create-company" and f["errorType"] == "urn:c3-m1:problem:invalid-state"


# ---------------------------------------------------------------- lookups (6.8)
def _offer_approved(engine):
    engine.on_event(ev("OfferApproved", offerId="7c1d9e3a-2b4f-4a6c-8d0e-5f1a2b3c4d5e", offerType="APP"))
    return proc_of(engine, "app-approval")


def test_when_not_met_skips_the_step(engine):
    p = _offer_approved(engine)   # stand-in offer is MULTI
    assert p["status"] == "COMPLETED" and states(p)["create-instance-client"] == "SKIPPED"
    assert dict(engine.call_log)["c2-m2.create-client-template"]["roles"] == [{"name": "Viewer"}, {"name": "Editor"}]


def test_failed_lookup_is_an_error_not_found_false(engine, fake, monkeypatch):
    monkeypatch.setenv("C8_M1_URL", FAKE)
    fake.answers["/api/c8-m1/internal/offers/7c1d9e3a-2b4f-4a6c-8d0e-5f1a2b3c4d5e"] = (
        403, {"type": "urn:c8-m1:problem:forbidden", "status": 403})
    p = _offer_approved(engine)
    sr = step(p, "create-client-template")
    assert p["status"] == "FAILED" and sr["state"] == "FAILED"
    assert sr["lastError"]["type"] == "urn:c8-m1:problem:forbidden" and sr["lastError"]["retryable"] is False
    assert not any(c == "c2-m2.create-client-template" for c, _ in engine.call_log)


def test_lookup_5xx_is_retried_and_404_is_found_false(engine, fake, monkeypatch):
    monkeypatch.setenv("C2_M2_URL", FAKE)
    answers = iter([(503, {"type": "x"}), (200, [])])
    fake.answers["/api/c2-m2/internal/app-instance-clients"] = lambda b: next(answers)
    engine.on_event(ev("SubscriptionDeactivated", offerSubscriptionId="s1"))
    p = proc_of(engine, "subscription-ended")
    sr = step(p, "disable-instance-client")
    assert sr["state"] == "READY" and sr["lastError"]["type"] == "x" and sr["lastError"]["retryable"] is True
    engine.clock.advance(5)
    engine.tick()
    assert states(p)["disable-instance-client"] == "SKIPPED" and p["status"] == "COMPLETED"
    assert fake.calls[0][1] == {"offerSubscriptionId": "s1"}   # query parameter per the operation


# ---------------------------------------------------------------- API (11, 13)
def _api(engine):
    app = FastAPI()
    problems.install(app)
    app.include_router(test_iam.router)
    app.include_router(c12_router.router)
    phttp.register_local(BASE, app)
    return TestClient(app, base_url=BASE)


def test_retrigger_permissions_and_state(engine, fake, monkeypatch):
    monkeypatch.setenv("C3_M1_URL", FAKE)
    fake.answers["/api/c3-m1/steps/create-company"] = (409, {"type": "urn:c3-m1:problem:invalid-state"})
    engine.on_event(submitted())
    p = proc_of(engine, "onboarding")
    c = _api(engine)
    url = f"/api/c12-m1/processes/{p['id']}/steps/create-company/retrigger"
    assert c.post(url, headers=bearer(person_token("rolf")), json={}).status_code == 403
    fake.answers.pop("/api/c3-m1/steps/create-company")
    r = c.post(url, headers=bearer(person_token("otto")), json={"comment": "fixed"})
    assert r.status_code == 202 and r.json()["state"] == "READY" and r.json()["attempts"] == 0
    assert step(p, "create-company")["state"] == "DONE" and published("ProcessStepRetriggered")
    assert c.post(url, headers=bearer(person_token("otto")), json={}).status_code == 409


def test_processes_of_a_subject(engine):
    engine.on_event(ev("SubscriptionSetupStarted", offerSubscriptionId="s9", offerId="o1", customerCompanyId="c1"))
    c = _api(engine)
    r = c.get("/api/c12-m1/internal/processes", params={"subjectType": "SUBSCRIPTION", "subjectId": "s9"},
              headers=bearer(tokens.service_token("c10-m1")))
    assert r.status_code == 200 and r.json()[0]["flowKey"] == "subscription-setup"
    r = c.get("/api/c12-m1/processes", headers=bearer(person_token("otto")), params={"flowKey": "subscription-setup"})
    assert r.json()["total"] == 1


def test_standin_results_follow_readme_11_3():
    assert standin.standin_result("c2-m2.create-instance-client", {"stepRunId": "r1"}) == {"id": "r1",
                                                                                          "iamClientId": "standin-r1"}
    assert standin.standin_result("c2-m3.create-for-subscription", {}) == {"accounts": []}
    assert standin.standin_result("c3-m2.transfer-company", {"bpn": "BPNL1"}) == {"bpn": "BPNL1"}
