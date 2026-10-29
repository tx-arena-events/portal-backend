# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C11-M1 Notifications: a small subset of the rules, enough to see the inbox work.

- `ApplicationSubmitted` -> `APPLICATION_SUBMITTED` for the applicant;
- `ApplicationCompleted` -> `APPLICATION_COMPLETED` for the applicant;
- `ProcessStepFailed` -> `PROCESS_STEP_FAILED` for every person with the portal role `OSP Admin`.
Recipients by portal role are read from the test IAM (the real module asks C2-M2).
"""
from __future__ import annotations

import datetime as dt
import uuid

from portal_common import standin
from tools import test_iam

STANDIN = True
MODULE = "c11-m1"
router = standin.router(MODULE)
notifications: list[dict] = []
applicants: dict[str, str] = {}   # applicationId -> sub


def reset() -> None:
    notifications.clear()
    applicants.clear()


def _add(user: str, ntype: str, event: dict, **content) -> None:
    notifications.append({"id": str(uuid.uuid4()), "userId": user, "type": ntype, "read": False, "done": False,
                          "createdAt": dt.datetime.now(dt.UTC).isoformat(), "sourceEventId": event["eventId"],
                          "content": content})


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


@router.get("/notifications")
def inbox(claims: dict = standin.perm(MODULE, "GET", "/notifications")):
    items = [n for n in notifications if n["userId"] == claims["sub"]]
    return {"items": items, "page": 0, "size": len(items), "total": len(items)}
