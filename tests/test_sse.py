# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Server-Sent Events of C12-M1 (README 7.6 to 7.8) and C11-M1 (README 7.6) over a real HTTP server.

The test client of Starlette buffers a response until it ends, so the streams run on uvicorn
(port chosen by the system) and are read with httpx in a thread.
"""
import json
import queue
import threading
import time
import uuid

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from conftest import BASE, person_token
from modules.c11_m1 import router as c11
from modules.c12_m1 import engine as eng
from modules.c12_m1 import router as c12
from portal_common import http as phttp
from portal_common import problems
from tools import test_iam

APPLICATION = "0f8e2d4c-6b1a-4c3e-8f7d-2a9b1c0d3e4f"   # FRAMEWORK 7a
COMPANY = "c0000001-0000-4000-8000-000000000001"


def ev(etype, **fields):
    return {"eventId": str(uuid.uuid4()), "eventType": etype, "occurredAt": "2026-11-06T09:30:00Z", **fields}


def submitted():
    return ev("ApplicationSubmitted", applicationId=APPLICATION, companyId=COMPANY, kind="ONBOARDING",
              existingCompany=False, applicantSub=test_iam.iam().find("erika"))


def approved():
    return ev("ApplicationApproved", applicationId=APPLICATION, companyId=COMPANY, kind="ONBOARDING",
              existingCompany=False)


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv("MODULE_URLS_DEFAULT", "")    # C12 built-in stand-ins (README 11.3)
    monkeypatch.setenv("SSE_HEARTBEAT_SECONDS", "0.2")
    eng.reset()
    c11.reset()
    app = FastAPI()
    problems.install(app)
    for r in (test_iam.router, c12.router, c11.router):
        app.include_router(r)
    phttp.register_local(BASE, app)   # JWKS of the test IAM for the token check
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="off",
                                        timeout_graceful_shutdown=1))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.01)
    port = srv.servers[0].sockets[0].getsockname()[1]
    readers = []

    def open_stream(path, person=None, last_id=None, token=None):
        r = Reader(f"http://127.0.0.1:{port}{path}", token or (person_token(person) if person else None), last_id)
        readers.append(r)
        return r

    yield open_stream
    for r in readers:
        r.stop.set()
    srv.should_exit = True
    thread.join(5)


class Reader:
    """Reads one event stream in a thread; events go to a queue, comments to a list."""

    def __init__(self, url, token, last_id):
        self.events: queue.Queue = queue.Queue()
        self.comments: list[str] = []
        self.retry = None
        self.status = None
        self.body = None
        self.stop = threading.Event()
        self.ready = threading.Event()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if last_id:
            headers["Last-Event-ID"] = last_id
        threading.Thread(target=self._run, args=(url, headers), daemon=True).start()
        assert self.ready.wait(10)

    def _run(self, url, headers):
        try:
            with httpx.Client(timeout=10) as client, client.stream("GET", url, headers=headers) as r:
                self.status, self.content_type = r.status_code, r.headers.get("content-type", "")
                if r.status_code != 200:
                    self.body = json.loads(r.read())
                    return
                self.ready.set()
                msg: dict = {}
                for line in r.iter_lines():
                    if self.stop.is_set():
                        return
                    if line.startswith(":"):
                        self.comments.append(line)
                    elif line:
                        field, _, value = line.partition(":")
                        msg[field] = value[1:] if value.startswith(" ") else value
                    elif "event" in msg:
                        self.events.put({"id": msg.get("id"), "event": msg["event"], "data": json.loads(msg["data"])})
                        msg = {}
                    elif "retry" in msg:
                        self.retry, msg = msg["retry"], {}
        except httpx.HTTPError:
            pass
        finally:
            self.ready.set()

    def next(self, timeout=5.0) -> dict:
        return self.events.get(timeout=timeout)

    def until(self, check, timeout=5.0) -> list[dict]:
        """Events up to and including the first one that meets `check`."""
        seen, end = [], time.time() + timeout
        while True:
            seen.append(self.next(max(0.01, end - time.time())))
            if check(seen[-1]):
                return seen

    def quiet(self, seconds=0.5) -> bool:
        try:
            self.events.get(timeout=seconds)
            return False
        except queue.Empty:
            return True


# ------------------------------------------------------------------ C12-M1 README 7.7, 7.8


def test_staff_follows_a_process_until_completed(server):
    e = eng.get_engine()
    e.on_event(submitted())
    pid = next(iter(e.processes))
    s = server(f"/api/c12-m1/processes/{pid}/events", "otto")
    assert s.status == 200 and s.content_type.startswith("text/event-stream")
    first = s.next()   # README 7.6: without Last-Event-ID the current state first
    assert s.retry == "3000" and first["event"] == "process-changed" and first["data"]["status"] == "WAITING"
    steps = {x["stepKey"]: x["state"] for x in first["data"]["steps"]}
    assert steps["create-company"] == "DONE" and steps["open-review"] == "DONE"
    e.on_event(approved())
    seen = s.until(lambda m: m["data"]["status"] == "COMPLETED")
    assert len(seen) > 1   # step changes before the end, not only the final state
    assert all(set(m["data"]) == {"processId", "flowKey", "subjectType", "subjectId", "status", "currentStep",
                                  "endReason", "steps", "updatedAt"} for m in seen)   # no variables, results
    assert s.quiet(0.5) and s.comments and s.comments[0] == ": keep-alive"   # heartbeat (0.2 s here)


def test_applicant_follows_own_application_only(server):
    erika = server(f"/api/c12-m1/applications/{APPLICATION}/events", "erika")
    lieschen = server(f"/api/c12-m1/applications/{APPLICATION}/events", "lieschen")
    assert erika.status == 200 and lieschen.status == 200 and erika.quiet(0.3)   # no process yet: open, empty
    eng.get_engine().on_event(submitted())
    msg = erika.until(lambda m: m["data"]["status"] == "WAITING")[-1]
    assert msg["data"]["flowKey"] == "onboarding" and msg["data"]["subjectId"] == APPLICATION
    assert lieschen.quiet(0.5)   # another person's application: nothing, and no 404
    pid = msg["data"]["processId"]
    staff_only = server(f"/api/c12-m1/processes/{pid}/events", "erika")
    assert staff_only.status == 403 and staff_only.body["type"] == "urn:c12-m1:problem:forbidden"
    bad = server("/api/c12-m1/applications/not-a-uuid/events", "erika")
    assert bad.status == 400


def test_reconnect_replays_missed_process_events(server):
    e = eng.get_engine()
    e.on_event(submitted())
    pid = next(iter(e.processes))
    s = server(f"/api/c12-m1/processes/{pid}/events", "otto")
    last = s.next()["id"]
    s.stop.set()                     # connection lost ...
    e.on_event(approved())           # ... while the process runs to the end
    again = server(f"/api/c12-m1/processes/{pid}/events", "otto", last_id=last)
    replayed = again.until(lambda m: m["data"]["status"] == "COMPLETED")
    assert len(replayed) > 1 and all(m["id"] != last for m in replayed)
    assert [m["data"]["status"] for m in replayed][0] == "RUNNING"   # the first missed change, not a snapshot
    unknown = server(f"/api/c12-m1/processes/{pid}/events", "otto", last_id="gone-7")
    assert unknown.next()["data"]["status"] == "COMPLETED"           # not kept: current state


def test_stream_needs_a_token_in_the_header(server):
    pid = str(uuid.uuid4())
    for path in (f"/api/c12-m1/processes/{pid}/events", f"/api/c12-m1/applications/{APPLICATION}/events",
                 "/api/c11-m1/notifications/events"):
        assert server(path).status == 401
        assert server(path, token="not-a-token").status == 401
        token_in_url = server(f"{path}?access_token={person_token('otto')}")
        assert token_in_url.status == 401 and token_in_url.body["type"].endswith(":problem:unauthorized")
    assert server(f"/api/c12-m1/processes/{pid}/events", "otto").status == 404


# ------------------------------------------------------------------ C11-M1 README 7.6


def test_inbox_stream_created_and_done(server):
    erika = server("/api/c11-m1/notifications/events", "erika")
    otto = server("/api/c11-m1/notifications/events", "otto")
    c11.on_event(submitted())
    created = erika.next()
    assert created["event"] == "notification-created" and created["data"]["type"] == "APPLICATION_SUBMITTED"
    assert "content" not in created["data"] and otto.quiet(0.3)
    pid = str(uuid.uuid4())
    c11.on_event(ev("ProcessStepFailed", processId=pid, flowKey="onboarding", subjectType="APPLICATION",
                    subjectId=APPLICATION, stepKey="register-company", errorType="x", retryable=False))
    task = otto.next()["data"]
    assert task["type"] == "PROCESS_STEP_FAILED" and task["forStaff"] is True and task["done"] is False
    c11.on_event(ev("ProcessStepRetriggered", processId=pid, stepKey="register-company", retriggeredBy="x"))
    done = otto.next()
    assert done["event"] == "notification-done" and done["data"]["notificationId"] == task["notificationId"]
    assert erika.quiet(0.3)


def test_inbox_reconnect_replays_or_resyncs(server):
    s = server("/api/c11-m1/notifications/events", "erika")
    c11.on_event(submitted())
    last = s.next()["id"]
    s.stop.set()
    c11.on_event(ev("ApplicationCompleted", applicationId=APPLICATION))
    again = server("/api/c11-m1/notifications/events", "erika", last_id=last)
    assert again.next()["data"]["type"] == "APPLICATION_COMPLETED"
    unknown = server("/api/c11-m1/notifications/events", "erika", last_id="gone-1")
    assert unknown.next()["event"] == "resync"
