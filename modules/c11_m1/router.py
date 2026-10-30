# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C11-M1 Notifications: a small subset of the rules, enough to see the inbox work.

- `ApplicationSubmitted` -> `APPLICATION_SUBMITTED` for the applicant;
- `ApplicationCompleted` -> `APPLICATION_COMPLETED` for the applicant;
- `ProcessStepFailed` -> `PROCESS_STEP_FAILED` for every person with the portal role `OSP Admin`;
  `ProcessStepRetriggered` (same step), `ProcessCompleted`, `ProcessEnded` close it (`done`).
Recipients by portal role are read from the test IAM (the real module asks C2-M2).

`GET /notifications/events` (README 7.6): Server-Sent Events `notification-created`,
`notification-done` and `resync` for the own inbox, through `portal_common.sse`.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import Header, Request

from portal_common import sse, standin
from tools import test_iam

STANDIN = True
MODULE = "c11-m1"
router = standin.router(MODULE)
notifications: list[dict] = []
applicants: dict[str, str] = {}   # applicationId -> sub
BROKER = sse.Broker()
TOPICS = {"APPLICATION_SUBMITTED": "INFO", "APPLICATION_COMPLETED": "INFO", "PROCESS_STEP_FAILED": "ACTION"}
STAFF = {"PROCESS_STEP_FAILED"}   # R17 is a staff notification; the stand-in stores it per OSP Admin


def reset() -> None:
    notifications.clear()
    applicants.clear()
    BROKER.reset()


def _add(user: str, ntype: str, event: dict, **content) -> None:
    n = {"id": str(uuid.uuid4()), "userId": user, "type": ntype, "topic": TOPICS[ntype], "read": False,
         "done": False, "createdAt": dt.datetime.now(dt.UTC).isoformat(), "sourceEventId": event["eventId"],
         "content": content}
    notifications.append(n)
    BROKER.publish("notification-created", {   # README 7.6: no content, only IDs, types and states
        "notificationId": n["id"], "type": ntype, "topic": n["topic"], "forStaff": ntype in STAFF,
        "done": False if n["topic"] == "ACTION" else None, "createdAt": n["createdAt"]}, {"sub": user})


def _close(match) -> None:
    for n in notifications:
        if n["topic"] == "ACTION" and not n["done"] and match(n):
            n["done"], n["doneAt"] = True, dt.datetime.now(dt.UTC).isoformat()
            BROKER.publish("notification-done", {"notificationId": n["id"], "doneAt": n["doneAt"]}, {"sub": n["userId"]})


def on_event(event: dict) -> None:
    t = event["eventType"]
    if t == "ApplicationSubmitted":
        applicants[event["applicationId"]] = event["applicantSub"]
        _add(event["applicantSub"], "APPLICATION_SUBMITTED", event, applicationId=event["applicationId"])
    elif t == "ApplicationCompleted" and event.get("applicationId") in applicants:
        _add(applicants[event["applicationId"]], "APPLICATION_COMPLETED", event, applicationId=event["applicationId"])
    elif t == "ProcessStepFailed":
        iam = test_iam.iam()
        for uid, u in list(iam.users.items()):
            if "OSP Admin" in u["roles"]:
                _add(uid, "PROCESS_STEP_FAILED", event, processId=event["processId"], stepKey=event.get("stepKey"))
    elif t == "ProcessStepRetriggered":
        _close(lambda n: n["type"] == "PROCESS_STEP_FAILED" and n["content"] == {
            "processId": event["processId"], "stepKey": event.get("stepKey")})
    elif t in ("ProcessCompleted", "ProcessEnded"):
        _close(lambda n: n["type"] == "PROCESS_STEP_FAILED" and n["content"]["processId"] == event["processId"])


@router.get("/notifications/events")
def inbox_events(request: Request, claims: dict = standin.perm(MODULE, "GET", "/notifications/events"),
                 last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
    """README 7.6: without Last-Event-ID only new events; an id no longer kept gives `resync`."""
    sub = claims["sub"]
    return sse.response(BROKER, request, claims, last_event_id, visible=lambda meta: meta.get("sub") == sub,
                        on_gap=lambda: [("resync", {})])


@router.get("/notifications")
def inbox(claims: dict = standin.perm(MODULE, "GET", "/notifications")):
    items = [n for n in notifications if n["userId"] == claims["sub"]]
    return {"items": items, "page": 0, "size": len(items), "total": len(items)}
