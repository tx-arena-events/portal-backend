# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""C12-M1 Process Orchestration: runs the flows of `flows.yaml` (README sections 4 to 6, 11).

In memory (no database yet). Events arrive from the event relay of the installation; steps are
called over HTTP or in-process (`portal_common.neighbours.address`), with the service token of
`svc-c12-m1`. With an empty module address the built-in stand-in answers (README 11.3):
steps `DONE` at once with the stand-in results, lookups with the answers of section 14
(`STANDIN_FILE` replaces both). Lookups follow README 6.8: `query` = `<module>.<operationId>`,
field paths, object mappings and `join`; a failed lookup is an error of the step, never
`found: false`. In memory: a restart loses the processes (upgrade: tables of README 5).
"""
from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import re
import threading
import uuid
from pathlib import Path

import httpx
import yaml

from portal_common import events, http, neighbours, spec, standin, tokens

MODULE = "c12-m1"
log = logging.getLogger("c12-m1")
ENVELOPE = {"eventId", "eventType", "occurredAt"}
OPEN = {"RUNNING", "WAITING", "FAILED"}
FINAL_STEP = {"DONE", "SKIPPED"}
LOOKUP_FAILED = "urn:c12-m1:problem:lookup-failed"


class FlowError(Exception):
    pass


class LookupFailed(Exception):
    def __init__(self, error: dict):
        super().__init__(error.get("title"))
        self.error = error


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime | None) -> str | None:
    return t.isoformat() if t else None


def _payload(event: dict) -> dict:
    return {k: v for k, v in event.items() if k not in ENVELOPE}


def extract(obj, path: str):
    """Field path (README 6.8): a.b, a[0].b, [0].id, a[TYPE].value (first entry whose `type` is TYPE)."""
    for part in re.findall(r"[^.\[\]]+|\[[^\]]*\]", path):
        if obj is None:
            return None
        if part.startswith("["):
            key = part[1:-1]
            if not isinstance(obj, list):
                return None
            if key.isdigit():
                obj = obj[int(key)] if int(key) < len(obj) else None
            else:
                obj = next((x for x in obj if isinstance(x, dict) and x.get("type") == key), None)
        else:
            obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def map_fields(raw, spec_: object):
    """README 6.8: a path, an object mapping, or `{join: [paths]}`. Returns None for no value."""
    if isinstance(spec_, str):
        return extract(raw, spec_)
    if isinstance(spec_, dict) and set(spec_) == {"join"}:
        parts = [str(v) for v in (extract(raw, p) for p in spec_["join"]) if v not in (None, "")]
        return " ".join(parts) or None
    if isinstance(spec_, dict):
        out = {k: v for k, v in ((k, map_fields(raw, sub)) for k, sub in spec_.items()) if v is not None}
        return out or None
    return None


def _completion_types(st: dict) -> set[str]:
    return {c["event"] if isinstance(c, dict) else c for c in st.get("completesOn") or []}


def lookup_operation(query: str) -> spec.Operation:
    module, _, op_id = (query or "").partition(".")
    ms = spec.modules().get(module)
    op = next((o for o in (ms.operations if ms else ()) if o.operation_id == op_id), None)
    if op is None or op.method != "GET" or not op.is_query:
        raise FlowError(f"lookup query {query}: no GET operation under /internal with this operationId")
    return op


def known_events() -> set[str]:
    """Event names of the spec: `eventType` constants of the event schemas in every openapi.yaml,
    plus the names in the event tables (README section 10 of every module, FRAMEWORK section 6)."""
    out = set()
    texts = [(spec.spec_dir() / "FRAMEWORK.md").read_text(encoding="utf-8")]
    texts += [f.read_text(encoding="utf-8") for f in spec.spec_dir().glob("*/README.md")]
    for text in texts:
        m = re.search(r"^## (?:10|6)\. (?:Events|Event catalogue)\n(.*?)(?=^## )", text, re.S | re.M)
        if m:
            out |= set(re.findall(r"^\| `([A-Z][A-Za-z]+)`", m.group(1), re.M))
            out |= {n for row in re.findall(r"^\|[^|]*\| ([^|]+)\|", m.group(1), re.M)
                    for n in re.findall(r"`([A-Z][A-Za-z]+)`", row)}
    for ms in spec.modules().values():
        for name, sch in (ms.document.get("components", {}).get("schemas") or {}).items():
            if name.startswith("Event"):
                const = (ms.resolve(sch).get("properties", {}).get("eventType") or {}).get("const")
                if const:
                    out.add(const)
    return out


def check(doc: dict) -> list[str]:
    """README 6.2 against the spec: FlowError for unknown steps, lookups and events, sources that
    are not earlier, and required inputs without a source. Returns the list of warnings (empty
    today); with `FLOWS_CHECK=warn` errors of the last two kinds become warnings."""
    mods = spec.modules()
    lookups = doc.get("lookups") or {}
    for name, lk in lookups.items():
        try:
            lookup_operation(lk.get("query"))
        except FlowError as e:
            raise FlowError(f"lookup {name}: {e}") from None
    warnings = []
    events_known = known_events() | {"ProcessStarted", "ProcessStepFailed", "ProcessCompleted", "ProcessEnded"}
    for fl in doc.get("flows") or []:
        seen: list[str] = []
        names = {fl["trigger"]["event"], *(fl.get("endOn") or [])}
        for st in fl["steps"]:
            where = f"flow {fl['key']}, step {st.get('key')}"
            names |= {st["waitFor"]} if st.get("waitFor") else set()
            names |= _completion_types(st) | set(st.get("failsOn") or [])
            module, _, step = st.get("call", "").partition(".")
            if module not in mods or step not in mods[module].steps():
                raise FlowError(f"{where}: unknown step {st.get('call')}")
            if st.get("lookup") and st["lookup"] not in lookups:
                raise FlowError(f"{where}: unknown lookup {st['lookup']}")
            sources = [s for s in (st.get("input") or {}).values() if isinstance(s, str)] + list((st.get("when") or {}))
            for src in sources:
                if src.startswith("steps.") and src.split(".")[1] not in seen:
                    raise FlowError(f"{where}: source {src} is not earlier in the flow")
                if src.startswith("lookup.") and src.split(".")[1] not in lookups:
                    raise FlowError(f"{where}: unknown lookup in {src}")
            op = mods[module].steps()[step]
            required = set(mods[module].resolve(op.request_schema).get("required", [])) - {"processId", "stepRunId"}
            missing = sorted(required - set(st.get("input") or {}))
            if missing:
                warnings.append(f"{where}: required input {missing} of {st['call']} has no source")
            seen.append(st["key"])
        unknown = sorted(n for n in names if n not in events_known)
        if unknown:
            warnings.append(f"flow {fl['key']}: events {unknown} are not in any event table or schema of the spec")
    return warnings


DEFAULT_STANDIN = {
    "lookups": {
        "c3-m1.queryCompany": {"name": "Beispiel Maschinenbau GmbH", "status": "REQUESTED", "coreDataSource": "BUSINESS_WALLET",
                               "identifiers": [{"type": "EBWOID", "value": "EBWOID-DE-0000000001"},
                                               {"type": "EUID", "value": "DEXX001T.HRB100001"}],
                               "address": {"street": "Musterstraße", "houseNumber": "1", "postalCode": "70569",
                                           "city": "Stuttgart", "region": "BW", "countryCode": "DE"},
                               "companyRoles": []},
        "c8-m1.getOfferInternal": {"offerType": "APP", "name": "Demo App", "instanceKind": "MULTI",
                                   "providerCompanyId": "11111111-1111-4111-8111-111111111111",
                                   "roles": [{"name": "Viewer"}, {"name": "Editor"}], "hasTechnicalUserProfile": True,
                                   "technicalUserProfile": {"name": "connector-sync",
                                                            "roles": [{"name": "Dataspace Discovery"}]}},
        "c2-m2.listAppInstanceClients": [],
    },
    "steps": {},
}


class Engine:
    def __init__(self, flows_file: str | Path | None = None):
        path = Path(flows_file or spec.flows_file())
        self.doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.warnings = check(self.doc)
        if self.warnings and os.environ.get("FLOWS_CHECK", "strict") != "warn":
            raise FlowError("; ".join(self.warnings))
        for w in self.warnings:
            log.warning("flows.yaml: %s", w)
        self.flows = {fl["key"]: fl for fl in self.doc["flows"]}
        self.version = self.doc.get("version", 1)
        self.defaults = self.doc.get("defaults") or {}
        self.lookups = self.doc.get("lookups") or {}
        self.clock = _now
        self.lock = threading.RLock()
        self.retry_base = float(os.environ.get("RETRY_BASE_SECONDS", 5))
        self.retry_factor = float(os.environ.get("RETRY_FACTOR", 2))
        self.retry_max = float(os.environ.get("RETRY_MAX_SECONDS", 600))
        self.max_attempts = int(os.environ.get("MAX_ATTEMPTS", self.defaults.get("maxAttempts", 5)))
        self.timeout = float(os.environ.get("STEP_TIMEOUT_SECONDS", 30))
        self.standin = copy.deepcopy(DEFAULT_STANDIN)
        if os.environ.get("STANDIN_FILE"):
            extra = yaml.safe_load(Path(os.environ["STANDIN_FILE"]).read_text(encoding="utf-8")) or {}
            for part in ("steps", "lookups"):
                self.standin[part].update(extra.get(part) or {})
        self.processes: dict[str, dict] = {}
        self.processed_events: set[str] = set()
        self.audit: list[dict] = []
        self.call_log: list[tuple[str, dict]] = []

    # ---------------------------------------------------------------- events (README 6.5)
    def on_event(self, event: dict) -> None:
        with self.lock:
            eid, etype = event.get("eventId"), event.get("eventType")
            if eid in self.processed_events:
                return
            self.processed_events.add(eid)
            touched = []
            for fl in self.flows.values():
                corr = fl["correlateBy"]
                proc = self._open_process(fl["key"], event.get(corr)) if corr in event else None
                if proc and etype in self._named_events(fl):
                    proc["variables"].update(_payload(event))
                    proc["inbox"].append({"type": etype, "payload": _payload(event), "used": False})
                    if etype in (fl.get("endOn") or []) and self._decision_open(proc):
                        self._end(proc, etype)   # README 6.5 point 5: only before every waitFor event arrived
                    touched.append(proc)
                elif etype == fl["trigger"]["event"] and not proc and self._trigger_matches(fl, event):
                    touched.append(self._start(fl, event))
            for proc in touched:
                self.advance(proc)

    @staticmethod
    def _decision_open(proc: dict) -> bool:
        waits = {st["waitFor"] for st in proc["flow"]["steps"] if st.get("waitFor")}
        arrived = {e["type"] for e in proc["inbox"]}
        return not waits or not waits <= arrived

    @staticmethod
    def _named_events(fl: dict) -> set[str]:
        names = set(fl.get("endOn") or [])
        for st in fl["steps"]:
            if st.get("waitFor"):
                names.add(st["waitFor"])
            names |= _completion_types(st) | set(st.get("failsOn") or [])
        return names

    @staticmethod
    def _trigger_matches(fl: dict, event: dict) -> bool:
        return all(event.get(k) == v for k, v in (fl["trigger"].get("when") or {}).items())

    def _open_process(self, flow_key: str, value) -> dict | None:
        return next((p for p in self.processes.values() if p["flowKey"] == flow_key
                     and p["correlationValue"] == value and p["status"] in OPEN), None)

    def _start(self, fl: dict, event: dict) -> dict:
        now = self.clock()
        proc = {
            "id": str(uuid.uuid4()), "flowKey": fl["key"], "flowVersion": self.version,
            "flow": copy.deepcopy(fl),  # README 4: a process keeps the flow version it started with
            "subjectType": fl["subject"]["type"], "subjectId": event.get(fl["subject"]["key"]),
            "correlationKey": fl["correlateBy"], "correlationValue": event.get(fl["correlateBy"]),
            "status": "RUNNING", "variables": _payload(event), "lookups": {}, "inbox": [],
            "triggerEventId": event.get("eventId"), "endReason": None,
            "createdAt": now, "updatedAt": now, "finishedAt": None,
            "steps": [{"stepRunId": str(uuid.uuid4()), "stepKey": st["key"], "call": st["call"], "state": "WAITING",
                       "attempts": 0, "maxAttempts": st.get("maxAttempts", self.max_attempts), "nextAttemptAt": None,
                       "awaitUntil": None, "result": None, "lastError": None, "startedAt": None, "finishedAt": None,
                       "attemptHistory": []} for st in fl["steps"]],
        }
        self.processes[proc["id"]] = proc
        self._publish("ProcessStarted", proc)
        return proc

    def _publish(self, etype: str, proc: dict, **extra) -> None:
        ev = {"eventType": etype, "processId": proc["id"]}
        if etype != "ProcessStepRetriggered":
            ev |= {"flowKey": proc["flowKey"], "subjectType": proc["subjectType"], "subjectId": proc["subjectId"]}
        events.publish(ev | extra)

    def _end(self, proc: dict, reason: str) -> None:
        for sr in proc["steps"]:
            if sr["state"] in ("WAITING", "READY", "AWAITING_EVENT"):
                sr["state"] = "SKIPPED"
        proc["status"], proc["endReason"] = "ENDED", reason
        proc["finishedAt"] = proc["updatedAt"] = self.clock()
        self._publish("ProcessEnded", proc)

    # ---------------------------------------------------------------- progress (README 6.3, 6.4)
    def advance(self, proc: dict) -> None:
        with self.lock:
            if proc["status"] in ("ENDED", "COMPLETED"):
                return
            for st, sr in zip(proc["flow"]["steps"], proc["steps"]):
                if sr["state"] in FINAL_STEP:
                    continue
                if sr["state"] == "FAILED":
                    return self._set_status(proc, "FAILED")
                if sr["nextAttemptAt"] and sr["nextAttemptAt"] > self.clock():
                    return self._set_status(proc, "RUNNING")      # due later; the worker picks it up
                if sr["state"] == "WAITING":
                    if st.get("waitFor") and not any(e["type"] == st["waitFor"] for e in proc["inbox"]):
                        return self._set_status(proc, "WAITING")
                    sr["state"] = "READY"
                if sr["state"] == "READY":
                    try:   # README 6.8: the lookup runs when the step is READY, before its `when`
                        when_ok = self._prepare(proc, st)
                    except LookupFailed as e:
                        self._error(proc, sr, e.error)
                        return self._set_status(proc, "FAILED" if sr["state"] == "FAILED" else "RUNNING")
                    if not when_ok:
                        sr["state"], sr["finishedAt"] = "SKIPPED", self.clock()
                        continue
                    self._call(proc, st, sr)
                    if proc["status"] == "ENDED":   # endOn during the call (README 6.3)
                        if sr["state"] not in FINAL_STEP:
                            sr["state"] = "SKIPPED"
                        return
                if sr["state"] == "AWAITING_EVENT":
                    self._check_completion(proc, st, sr)
                if sr["state"] in FINAL_STEP:
                    continue
                if sr["state"] == "FAILED":
                    return self._set_status(proc, "FAILED")
                return self._set_status(proc, "RUNNING")
            proc["status"] = "COMPLETED"
            proc["finishedAt"] = proc["updatedAt"] = self.clock()
            self._publish("ProcessCompleted", proc)

    def _set_status(self, proc: dict, status: str) -> None:
        proc["status"], proc["updatedAt"] = status, self.clock()

    def _take_event(self, proc: dict, types: set[str]) -> dict | None:
        for e in proc["inbox"]:
            if e["type"] in types and not e["used"]:
                e["used"] = True
                return e
        return None

    # ---------------------------------------------------------------- sources and lookups
    def resolve(self, proc: dict, src):
        if not isinstance(src, str):
            return src
        if src.startswith("="):
            return src[1:]
        if src.startswith("steps."):
            _, key, field = src.split(".", 2)
            sr = next(s for s in proc["steps"] if s["stepKey"] == key)
            return extract(sr["result"] or {}, field)
        if src.startswith("lookup."):
            _, name, field = src.split(".", 2)
            return (proc["lookups"].get(name) or {}).get(field)
        return extract(proc["variables"], src)

    def _prepare(self, proc: dict, st: dict) -> bool:
        """Runs the lookups the step needs; returns whether `when` is met."""
        names = [st["lookup"]] if st.get("lookup") else []
        names += [src.split(".")[1] for src in (st.get("when") or {}) if src.startswith("lookup.")]
        for name in names:
            if name not in proc["lookups"]:
                proc["lookups"][name] = self._run_lookup(proc, name)
        return all(self.resolve(proc, src) == want for src, want in (st.get("when") or {}).items())

    def build_input(self, proc: dict, st: dict, sr: dict) -> dict:
        data = {"processId": proc["id"], "stepRunId": sr["stepRunId"]}
        for field, src in (st.get("input") or {}).items():
            val = self.resolve(proc, src)
            if val is not None:  # README 6.1: a missing source value is left out
                data[field] = val
        return data

    def _run_lookup(self, proc: dict, name: str) -> dict:
        lk = self.lookups[name]
        op = lookup_operation(lk["query"])
        params = {p: self.resolve(proc, src) for p, src in (lk.get("params") or {}).items()}
        path = op.path
        for p in list(params):
            if "{" + p + "}" in path:
                path = path.replace("{" + p + "}", str(params.pop(p)))
        base = neighbours.address(op.module)
        if not base:   # built-in stand-in (README 11.3)
            raw = copy.deepcopy(self.standin["lookups"].get(lk["query"]))
            if isinstance(raw, dict):
                raw.update({p: self.resolve(proc, src) for p, src in (lk.get("params") or {}).items()})
            return self._lookup_result(lk, raw)
        try:
            r = http.request("GET", f"{base}{spec.module(op.module).base_path}{path}", timeout=self.timeout,
                             params={k: v for k, v in params.items() if v is not None},
                             headers={"Authorization": "Bearer " + tokens.service_token(MODULE)})
        except httpx.HTTPError as e:
            raise LookupFailed({"type": LOOKUP_FAILED, "status": None, "title": f"lookup {name}: {type(e).__name__}",
                                "retryable": True}) from e
        if r.status_code == 404:
            return {"found": False}
        if r.status_code != 200:
            try:
                body = r.json() if isinstance(r.json(), dict) else {}
            except ValueError:
                body = {}
            retryable = body["retryable"] if isinstance(body.get("retryable"), bool) else r.status_code >= 500
            raise LookupFailed({"type": body.get("type") or LOOKUP_FAILED, "status": r.status_code,
                                "title": f"lookup {name} answered {r.status_code}", "retryable": retryable})
        return self._lookup_result(lk, r.json())

    @staticmethod
    def _lookup_result(lk: dict, raw) -> dict:
        if not raw:   # empty list or nothing: not found (README 6.8)
            return {"found": False}
        res = {"found": True}
        for target, fspec in (lk.get("fields") or {}).items():
            val = map_fields(raw, fspec)
            if val is not None:
                res[target] = val
        return res

    # ---------------------------------------------------------------- calling a step
    def _call(self, proc: dict, st: dict, sr: dict, trigger: str = "WORKER", by: str | None = None) -> None:
        now = self.clock()
        sr["state"], sr["attempts"] = "IN_PROGRESS", sr["attempts"] + 1
        sr["startedAt"] = sr["startedAt"] or now
        attempt = {"number": sr["attempts"], "trigger": trigger, "triggeredBy": by, "startedAt": _iso(now),
                   "finishedAt": None, "outcome": None, "errorType": None}
        sr["attemptHistory"].append(attempt)
        data = self.build_input(proc, st, sr)
        self.call_log.append((st["call"], data))
        outcome = self._invoke(proc, st, data)
        attempt["finishedAt"] = _iso(self.clock())
        if "error" in outcome:
            attempt["outcome"], attempt["errorType"] = "ERROR", outcome["error"].get("type")
            sr["attempts"] -= 1   # _error counts the attempt
            return self._error(proc, sr, outcome["error"])
        state = outcome.get("state")
        attempt["outcome"], sr["nextAttemptAt"] = state, None
        if state == "RUNNING":
            sr["state"], sr["awaitUntil"] = "AWAITING_EVENT", self.clock() + self._await_timeout(st)
        else:
            sr["state"], sr["finishedAt"], sr["result"] = "DONE", self.clock(), outcome.get("result") or {}
            self._take_event(proc, _completion_types(st))   # a module may also publish the event

    def _error(self, proc: dict, sr: dict, err: dict) -> None:
        sr["attempts"] += 1
        sr["lastError"] = err
        if err.get("retryable") is True and sr["attempts"] < sr["maxAttempts"]:
            wait = min(self.retry_base * self.retry_factor ** (sr["attempts"] - 1), self.retry_max)
            sr["state"] = "READY" if sr["state"] == "IN_PROGRESS" else sr["state"]
            sr["nextAttemptAt"] = self.clock() + dt.timedelta(seconds=wait)
        else:
            self._fail(proc, sr, err.get("type"), bool(err.get("retryable")))

    def _await_timeout(self, st: dict) -> dt.timedelta:
        # step setting > AWAIT_TIMEOUT > defaults of the flow file
        iso = st.get("awaitTimeout") or os.environ.get("AWAIT_TIMEOUT") or self.defaults.get("awaitTimeout") or "PT24H"
        m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso)
        d, h, mi, s = (int(x or 0) for x in m.groups())
        return dt.timedelta(days=d, hours=h, minutes=mi, seconds=s)

    def _invoke(self, proc: dict, st: dict, data: dict) -> dict:
        module, _, step = st["call"].partition(".")
        base = neighbours.address(module)
        if not base:
            return self._standin(proc, st, data)
        try:
            r = http.request("POST", f"{base}{spec.module(module).base_path}/steps/{step}", json=data,
                             timeout=self.timeout, headers={"Authorization": "Bearer " + tokens.service_token(MODULE)})
        except httpx.HTTPError as e:   # README 6.3: timeouts and connection errors are retryable
            return {"error": {"type": "connection", "status": None, "title": type(e).__name__, "retryable": True}}
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200 and isinstance(body, dict) and body.get("state") in ("DONE", "RUNNING"):
            return body
        body = body if isinstance(body, dict) else {}
        return {"error": {"type": body.get("type", f"http-{r.status_code}"), "status": r.status_code,
                          "title": body.get("title"), "detail": body.get("detail"),
                          "retryable": body.get("retryable") is True}}

    def _standin(self, proc: dict, st: dict, data: dict) -> dict:
        """README 11.3: DONE at once, no events; `set` of the first completesOn entry applies."""
        result = self.standin["steps"].get(st["call"])
        if result is None:
            result = standin.standin_result(st["call"], data)
        comp = st.get("completesOn") or []
        if comp and isinstance(comp[0], dict):
            proc["variables"].update(comp[0].get("set") or {})
        return {"stepRunId": data["stepRunId"], "state": "DONE", "result": copy.deepcopy(result)}

    def _check_completion(self, proc: dict, st: dict, sr: dict) -> None:
        ev = self._take_event(proc, _completion_types(st) | set(st.get("failsOn") or []))
        if ev and ev["type"] in (st.get("failsOn") or []):
            sr["lastError"] = {"type": f"event:{ev['type']}", "status": None, "title": ev["payload"].get("reason"),
                               "retryable": False}
            return self._fail(proc, sr, f"event:{ev['type']}", False)
        if ev:
            sr["state"], sr["finishedAt"], sr["result"] = "DONE", self.clock(), ev["payload"]
            for c in st.get("completesOn") or []:
                if isinstance(c, dict) and c["event"] == ev["type"]:
                    proc["variables"].update(c.get("set") or {})
            return
        if sr["awaitUntil"] and self.clock() > sr["awaitUntil"]:
            sr["lastError"] = {"type": "await-timeout", "status": None, "title": "no completing event", "retryable": False}
            self._fail(proc, sr, "await-timeout", False)

    def _fail(self, proc: dict, sr: dict, error_type: str | None, retryable: bool) -> None:
        sr["state"], sr["finishedAt"], sr["nextAttemptAt"] = "FAILED", self.clock(), None
        self._set_status(proc, "FAILED")
        self._publish("ProcessStepFailed", proc, stepKey=sr["stepKey"], errorType=error_type, retryable=retryable)

    # ---------------------------------------------------------------- worker and retrigger
    def tick(self) -> None:
        """Worker run (README 4): due repetitions and passed await deadlines."""
        with self.lock:
            for proc in list(self.processes.values()):
                if proc["status"] == "RUNNING":
                    self.advance(proc)

    def retrigger(self, proc: dict, step_key: str, sub: str, comment: str | None = None) -> dict | None:
        with self.lock:
            sr = next(r for r in proc["steps"] if r["stepKey"] == step_key)
            if sr["state"] != "FAILED":
                return None
            sr.update(state="READY", attempts=0, nextAttemptAt=None,
                      awaitUntil=None, finishedAt=None)
            proc["status"] = "RUNNING"
            self.audit.append({"at": _iso(self.clock()), "sub": sub, "processId": proc["id"], "stepKey": step_key,
                               "comment": comment})
            self._publish("ProcessStepRetriggered", proc, stepKey=step_key, retriggeredBy=sub)
            result = dict(sr)
            self.advance(proc)
            return result


_engine: Engine | None = None
_worker: threading.Thread | None = None
_stop = threading.Event()


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        _engine = Engine()
    return _engine


def reset() -> Engine:
    global _engine
    _engine = Engine()
    return _engine


def on_event(event: dict) -> None:
    get_engine().on_event(event)


def start_worker() -> None:
    """Background worker for due repetitions and await deadlines (WORKER_POLL_SECONDS)."""
    global _worker
    if _worker and _worker.is_alive():
        return
    _stop.clear()
    interval = float(os.environ.get("WORKER_POLL_SECONDS", 2))

    def loop() -> None:
        while not _stop.wait(interval):
            try:
                get_engine().tick()
            except Exception:  # noqa: BLE001 - the worker must keep running
                log.exception("worker tick failed")

    _worker = threading.Thread(target=loop, name="c12-worker", daemon=True)
    _worker.start()


def stop_worker() -> None:
    _stop.set()
