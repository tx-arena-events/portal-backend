# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""C12-M1 Process Orchestration API (openapi.yaml, README 7 and 9). Provided module, not a stand-in."""
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse

from portal_common.problems import Problem, route_class
from portal_common.tokens import require_spec

from . import engine as eng

PROVIDED = True   # provided module (C12-M1 kind "Provided"), not a stand-in
MODULE = eng.MODULE
router = APIRouter(prefix="/api/c12-m1", route_class=route_class(MODULE), tags=["C12-M1 Process Orchestration"])
SUBJECT_TYPES = {"APPLICATION", "COMPANY", "TECHNICAL_USER", "OFFER", "SUBSCRIPTION", "CONNECTOR"}
STATUSES = {"RUNNING", "WAITING", "FAILED", "COMPLETED", "ENDED"}


def _perm(method: str, path: str):
    return Depends(require_spec(MODULE, method, path))


def _iso(t):
    return t.isoformat() if t else None


def summary(p: dict) -> dict:
    current = next((s["stepKey"] for s in p["steps"] if s["state"] not in ("DONE", "SKIPPED")), None)
    return {"id": p["id"], "flowKey": p["flowKey"], "flowVersion": p["flowVersion"], "subjectType": p["subjectType"],
            "subjectId": p["subjectId"], "status": p["status"], "currentStep": current, "endReason": p["endReason"],
            "createdAt": _iso(p["createdAt"]), "updatedAt": _iso(p["updatedAt"]), "finishedAt": _iso(p["finishedAt"])}


def step_run(s: dict, detail: bool = False) -> dict:
    out = {k: s[k] for k in ("stepRunId", "stepKey", "call", "state", "attempts", "maxAttempts", "lastError")}
    out |= {"nextAttemptAt": _iso(s["nextAttemptAt"]), "awaitUntil": _iso(s["awaitUntil"]),
            "startedAt": _iso(s["startedAt"]), "finishedAt": _iso(s["finishedAt"])}
    if detail:
        out |= {"result": s["result"], "attempts_": s["attemptHistory"]}
    return out


def reset() -> None:
    eng.reset()


on_event = eng.on_event


def on_startup() -> None:
    eng.start_worker()


def _process(pid: str) -> dict:
    proc = eng.get_engine().processes.get(pid)
    if not proc:
        raise Problem(MODULE, 404, "not-found", detail="process unknown")
    return proc


@router.get("/processes", dependencies=[_perm("GET", "/processes")])
def list_processes(flowKey: str | None = None, status: str | None = None, subjectType: str | None = None,
                   subjectId: str | None = None, updatedSince: str | None = None, page: int = 0, size: int = 20):
    if page < 0 or not 1 <= size <= 100 or (status and status not in STATUSES) or \
            (subjectType and subjectType not in SUBJECT_TYPES):
        raise Problem(MODULE, 400, "validation")
    items = [summary(p) for p in list(eng.get_engine().processes.values())
             if (not flowKey or p["flowKey"] == flowKey) and (not status or p["status"] == status)
             and (not subjectType or p["subjectType"] == subjectType) and (not subjectId or p["subjectId"] == subjectId)
             and (not updatedSince or _iso(p["updatedAt"]) >= updatedSince)]
    items.sort(key=lambda x: x["updatedAt"], reverse=True)
    return {"items": items[page * size:(page + 1) * size], "page": page, "size": size, "total": len(items)}


@router.get("/processes/{processId}", dependencies=[_perm("GET", "/processes/{processId}")])
def get_process(processId: str):
    proc = _process(processId)
    return summary(proc) | {"variables": proc["variables"], "steps": [step_run(s) for s in proc["steps"]]}


@router.get("/processes/{processId}/steps/{stepKey}", dependencies=[_perm("GET", "/processes/{processId}/steps/{stepKey}")])
def get_step(processId: str, stepKey: str):
    sr = next((s for s in _process(processId)["steps"] if s["stepKey"] == stepKey), None)
    if not sr:
        raise Problem(MODULE, 404, "not-found", detail="step unknown")
    return step_run(sr, detail=True)


@router.post("/processes/{processId}/steps/{stepKey}/retrigger", status_code=202)
def retrigger(processId: str, stepKey: str,
              claims: Annotated[dict, _perm("POST", "/processes/{processId}/steps/{stepKey}/retrigger")],
              body: Annotated[dict | None, Body()] = None):
    comment = (body or {}).get("comment")
    if comment is not None and (not isinstance(comment, str) or len(comment) > 500):
        raise Problem(MODULE, 400, "validation", detail="comment up to 500 characters")
    proc = _process(processId)
    if not any(s["stepKey"] == stepKey for s in proc["steps"]):
        raise Problem(MODULE, 404, "not-found", detail="step unknown")
    sr = eng.get_engine().retrigger(proc, stepKey, claims["sub"], comment)
    if sr is None:
        raise Problem(MODULE, 409, "invalid-state", detail="step is not FAILED")
    return JSONResponse(step_run(sr), status_code=202)


@router.get("/flows", dependencies=[_perm("GET", "/flows")])
def list_flows():
    return {"items": [f | {"version": eng.get_engine().version} for f in eng.get_engine().flows.values()]}


@router.get("/flows/{flowKey}", dependencies=[_perm("GET", "/flows/{flowKey}")])
def get_flow(flowKey: str):
    fl = eng.get_engine().flows.get(flowKey)
    if not fl:
        raise Problem(MODULE, 404, "not-found", detail="flow unknown")
    return fl | {"version": eng.get_engine().version}


@router.get("/internal/processes", dependencies=[_perm("GET", "/internal/processes")])
def internal_processes(subjectType: Annotated[str, Query()], subjectId: Annotated[str, Query()]):
    if subjectType not in SUBJECT_TYPES:
        raise Problem(MODULE, 400, "validation", detail="subjectType")
    items = [summary(p) for p in list(eng.get_engine().processes.values())
             if p["subjectType"] == subjectType and p["subjectId"] == subjectId]
    return sorted(items, key=lambda x: x["createdAt"], reverse=True)
