# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Acceptance demo: the scenarios of the hackathon against running installations, over HTTP only.

    uv run python demo.py                 # starts CSP stand-in, OSP A (and OSP B for scenario 4)
    uv run python demo.py --scenarios 1,2,3
    uv run python demo.py --serve         # only start the processes and keep them running
    uv run python demo.py --no-start      # against already running processes
    uv run python demo.py --csp-issuer    # OSP B with the issuer of the CSP (Community Days, not the hackathon)

Every check looks at content, not only at status codes: the address that arrived at the CSP, the
DID in the directory, the credentials, the client ID and technical user in the provider callback.
A failed check prints the step trace of the process with `lastError`. Exit code 0 = all green.

Processes: CSP stand-in `http://localhost:8100`, OSP A `http://localhost:8000`, OSP B
`http://localhost:8001` (ports via `DEMO_CSP_PORT`, `DEMO_OSP_A_PORT`, `DEMO_OSP_B_PORT`).
Keys and the trust list are generated under `runtime/` at each start.

Credentials: OSP A and OSP B run on the route `STUB` of C4-M1 (hackathon, OP-53). `--csp-issuer`
points `CSP_ISSUER_URL` of OSP B to the issuer API of the CSP stand-in (route `CSP_ISSUER`).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from portal_common import http  # noqa: E402
from tools.keys import ec_jwk  # noqa: E402

REALM = "/auth/realms/CX-Central"
OSP_A_ID, OSP_B_ID = "22222222-2222-4222-8222-222222222222", "99a5685c-7397-402c-88de-b1b3a939ecc9"
OSP_COMPANY = "11111111-1111-4111-8111-111111111111"


@dataclass
class Target:
    osp_a: str
    csp: str
    osp_b: str | None = None
    ctx: dict = field(default_factory=dict)
    results: dict = field(default_factory=dict)
    current: str = ""
    quiet: bool = False
    csp_issuer: bool = False   # OSP B issues through the CSP issuer instead of the stand-in STUB

    # ------------------------------------------------------------ output
    def log(self, line: str) -> None:
        if not self.quiet:
            print(line, flush=True)

    def scenario(self, name: str) -> None:
        self.current = name
        self.results[name] = []
        self.log(f"\n=== {name}")

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        self.results[self.current].append((label, bool(ok), detail))
        self.log(f"  [{'OK' if ok else 'FAILED'}] {label}" + (f" - {detail}" if detail and not ok else ""))
        return bool(ok)

    def note(self, text: str) -> None:
        self.log(f"         {text}")

    # ------------------------------------------------------------ HTTP
    def call(self, method: str, url: str, token: str | None = None, **kw):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return http.request(method, url, timeout=30, headers=headers, **kw)

    def token(self, base: str, person: str) -> str:
        return self.call("GET", f"{base}{REALM}/test/tokens/{person}").json()["access_token"]

    def claims(self, base: str, person: str) -> dict:
        r = self.call("GET", f"{base}{REALM}/test/tokens/{person}").json()
        import jwt
        return jwt.decode(r["access_token"], options={"verify_signature": False})

    def process(self, base: str, admin: str, flow: str, subject: str, until: tuple[str, ...], timeout: float = 30):
        """Waits until the process of `flow` for `subject` has one of the states in `until`."""
        end, p = time.time() + timeout, None
        while time.time() < end:
            r = self.call("GET", f"{base}/api/c12-m1/processes", self.token(base, admin),
                          params={"flowKey": flow, "subjectId": subject})
            items = r.json().get("items", []) if r.status_code == 200 else []
            if items:
                p = self.call("GET", f"{base}/api/c12-m1/processes/{items[0]['id']}", self.token(base, admin)).json()
                if p["status"] in until:
                    return p
            time.sleep(0.2)
        return p

    def trace(self, p: dict | None) -> str:
        if not p:
            return "no process"
        out = ", ".join(f"{s['stepKey']}={s['state']}" for s in p["steps"])
        errors = [f"{s['stepKey']}: {s['lastError']}" for s in p["steps"] if s.get("lastError")]
        return f"{p['status']}: {out}" + (f" | lastError {errors}" if errors else "")

    def step_result(self, base: str, admin: str, p: dict, key: str) -> dict:
        return self.call("GET", f"{base}/api/c12-m1/processes/{p['id']}/steps/{key}", self.token(base, admin)).json()


# ============================================================================ scenario 1
def onboarding(t: Target, base: str, applicant: str, reviewer: str, admin: str, company: str, osp_id: str,
               ui_client: str) -> bool:
    """FRAMEWORK 5.2 with the Business Wallet; returns True when the company is ACTIVE.
    `ui_client` is the client of the installation's UI at the EBW-Connector (FRAMEWORK 7a)."""
    tok = t.token(base, applicant)
    perms = t.claims(base, applicant)["resource_access"]["portal-core"]["roles"]
    t.check("applicant signs in (Applicant, register_company)", "register_company" in perms)

    def application(client: str) -> dict:
        att = t.call("POST", f"{base}/ebw/test/attestations", json={"company": company, "clientId": client})
        return {"kind": "ONBOARDING", "verification": "EBW", "attestation": att.json()["attestation"],
                "companyRoles": ["ACTIVE_PARTICIPANT"],
                "companyData": {"identifiers": [{"type": "VAT_ID", "value": "DE123456789" if company == "maschinenbau"
                                                 else "DE300000003"}]}}
    r = t.call("POST", f"{base}/api/c1-m2/applications", tok, json=application("demo-osp"))
    t.check(f"C1-M2 refuses an attestation for another client (ATTESTATION_CLIENT_IDS = {ui_client})",
            r.status_code == 400 and r.json().get("reason") == "client", f"{r.status_code} {r.text[:200]}")
    r = t.call("POST", f"{base}/api/c1-m2/applications", tok, json=application(ui_client))
    if not t.check(f"C1-M2 creates the application (attestation for {ui_client} from the EBW-Connector double)",
                   r.status_code == 201,
                   f"{r.status_code} {r.text[:200]}"):
        return False
    app = r.json()
    t.ctx[f"{company}.companyId"], aid = app["companyId"], app["id"]
    consents = [{"agreementKey": k, "version": "1.0", "language": "en"}
                for k in ("terms-active-participant", "security-regulations")]
    r = t.call("POST", f"{base}/api/c1-m2/applications/{aid}/submit", tok, json={"consents": consents})
    t.check("C1-M2 submit with consents -> ApplicationSubmitted", r.status_code == 200 and r.json()["status"] == "SUBMITTED",
            r.text[:200])
    p = t.process(base, admin, "onboarding", aid, ("WAITING", "FAILED"))
    if not t.check("C12 onboarding: create-company and open-review DONE, WAITING for the decision",
                   p and p["status"] == "WAITING", t.trace(p)):
        return False
    rv = t.token(base, reviewer)
    cases = t.call("GET", f"{base}/api/c1-m3/review-cases", rv).json().get("items", [])
    case = next((c for c in cases if c["applicationId"] == aid), None)
    if not t.check("C1-M3 review case visible to the OSP Reviewer", case is not None):
        return False
    checks = {c["checkKey"]: c["result"] for c in case["checks"]}
    t.check("C1-M3 duplicate check at the CSP through C3-M2 passed", checks.get("DUPLICATE_AT_CSP") == "PASSED",
            json.dumps(checks))
    r = t.call("POST", f"{base}/api/c1-m3/review-cases/{case['id']}/approve", rv)
    t.check("OSP Reviewer approves -> ApplicationApproved", r.status_code == 200, r.text[:200])
    p = t.process(base, admin, "onboarding", aid, ("COMPLETED", "FAILED", "ENDED"), timeout=60)
    if not t.check("C12 onboarding runs to COMPLETED", p and p["status"] == "COMPLETED", t.trace(p)):
        return False
    cid = app["companyId"]
    c = t.call("GET", f"{base}/api/c3-m1/osp/companies/{cid}", t.token(base, admin)).json()
    t.ctx[f"{company}.bpn"] = c.get("bpn")
    t.check("C3-M1: company ACTIVE with BPN and company role ACTIVE_PARTICIPANT",
            c.get("status") == "ACTIVE" and c.get("bpn") and "ACTIVE_PARTICIPANT" in c.get("companyRoles", []),
            json.dumps({k: c.get(k) for k in ("status", "bpn", "companyRoles")}))
    t.note(f"companyId {cid}, BPN {c.get('bpn')}"
           + (" (test IDs of FRAMEWORK 7a)" if company == "maschinenbau" else " (no test IDs in FRAMEWORK 7a)"))
    row = next((x for x in t.call("GET", f"{t.csp}/admin/companies").json() if x["bpn"] == c.get("bpn")), {})
    wallet_did = t.step_result(base, admin, p, "create-wallet").get("result", {}).get("did")
    t.check("CSP: BPN registered, home OSP = this OSP, membership active, DID in the directory = wallet DID",
            row.get("homeOspId") == osp_id and row.get("memberActive") and row.get("did") == wallet_did and wallet_did,
            json.dumps({k: row.get(k) for k in ("homeOspId", "memberActive", "did")}) + f" wallet={wallet_did}")
    addr = row.get("address") or {}
    t.check("CSP: address complete (streetAddress, postalCode, locality, country)",
            all(addr.get(k) for k in ("streetAddress", "postalCode", "locality", "country")), json.dumps(addr))
    t.note(f"address at the CSP: {json.dumps(addr, ensure_ascii=False)}")
    tok = t.token(base, applicant)
    claims = t.claims(base, applicant)
    t.check("applicant's next token: company_id, bpn and the permissions of Company Admin",
            claims.get("company_id") == cid and claims.get("bpn") == c.get("bpn")
            and "manage_company_users" in claims["resource_access"]["portal-core"]["roles"],
            json.dumps({k: claims.get(k) for k in ("company_id", "bpn")}))
    creds = t.call("GET", f"{base}/api/c4-m1/companies/{cid}/credentials", tok).json().get("credentials", [])
    t.check("C4-M1: credentials BPN and MEMBERSHIP ISSUED",
            {x["credentialType"]: x["status"] for x in creds} == {"BPN": "ISSUED", "MEMBERSHIP": "ISSUED"}, json.dumps(creds))
    inbox = [n["type"] for n in t.call("GET", f"{base}/api/c11-m1/notifications", tok).json().get("items", [])]
    t.check("C11: applicant sees APPLICATION_SUBMITTED and APPLICATION_COMPLETED",
            {"APPLICATION_SUBMITTED", "APPLICATION_COMPLETED"} <= set(inbox), str(inbox))
    return True


def scenario_1(t: Target) -> None:
    t.scenario("Scenario 1 - onboarding at OSP A (FRAMEWORK 5.2)")
    onboarding(t, t.osp_a, "erika", "rolf", "otto", "maschinenbau", OSP_A_ID, "osp-a-ui")


# ============================================================================ scenario 2
def scenario_2(t: Target) -> None:
    t.scenario("Scenario 2 - connector into the discovery of the CSP (FRAMEWORK 5.9)")
    bpn = t.ctx.get("maschinenbau.bpn")
    if not t.check("precondition: company of scenario 1 is active", bool(bpn)):
        return
    endpoint = "https://edc.beispiel-maschinenbau.example/api/v1/dsp"
    r = t.call("POST", f"{t.osp_a}/api/c6-m1/connectors", t.token(t.osp_a, "erika"),
               json={"name": "EDC Beispiel Maschinenbau", "endpoint": endpoint, "location": "DE"})
    if not t.check("C6-M1: Company Admin registers a connector -> ConnectorRegistered", r.status_code == 201, r.text[:200]):
        return
    con = r.json()["connectorId"]
    p = t.process(t.osp_a, "otto", "connector-registered", con, ("COMPLETED", "FAILED"))
    t.check("C12 connector-registered -> C3-M2 register-connector COMPLETED", p and p["status"] == "COMPLETED", t.trace(p))
    row = next((x for x in t.call("GET", f"{t.csp}/admin/companies").json() if x["bpn"] == bpn), {})
    t.check("CSP discovery: the connector with its endpoint under the company's BPN",
            (row.get("connectors") or {}).get(con) == endpoint, json.dumps(row.get("connectors")))


# ============================================================================ scenario 3
def scenario_3(t: Target) -> None:
    t.scenario("Scenario 3 - app: offer, approval, subscription, setup, ACTIVE (FRAMEWORK 5.8)")
    base, pia = t.osp_a, t.token(t.osp_a, "pia")
    if not t.check("precondition: customer company of scenario 1 is active", bool(t.ctx.get("maschinenbau.bpn"))):
        return
    r = t.call("POST", f"{base}/api/c8-m1/offers", pia, json={"offerType": "APP", "name": "Demo App", "instanceKind": "MULTI"})
    if not t.check("C8-M1: provider (OSP company, APP_PROVIDER) creates an app offer", r.status_code == 201, r.text[:200]):
        return
    oid = r.json()["offerId"]
    t.call("PUT", f"{base}/api/c8-m1/offers/{oid}/roles", pia, json=[{"name": "Viewer"}, {"name": "Editor"}])
    t.call("PUT", f"{base}/api/c8-m1/offers/{oid}/technical-user-profile", pia,
           json={"name": "connector-sync", "roles": [{"name": "Dataspace Discovery"}]})
    r = t.call("POST", f"{base}/api/c8-m1/offers/{oid}/submit", pia)
    t.check("C8-M1: submit -> IN_REVIEW", r.status_code == 200 and r.json()["status"] == "IN_REVIEW", r.text[:200])
    r = t.call("POST", f"{base}/api/c8-m1/osp/offers/{oid}/approve", t.token(base, "otto"))
    t.check("C8-M1: OSP Admin approves -> ACTIVE, OfferApproved", r.status_code == 200 and r.json()["status"] == "ACTIVE")
    p = t.process(base, "otto", "app-approval", oid, ("COMPLETED", "FAILED"))
    t.check("C12 app-approval: C2-M2 create-client-template with the roles of the offer (lookup offer)",
            p and p["status"] == "COMPLETED" and t.step_result(base, "otto", p, "create-client-template")["state"] == "DONE",
            t.trace(p))
    prov = f"{base}/test-tools/provider"
    r = t.call("PUT", f"{base}/api/c10-m1/provider-configuration", pia, json={
        "setupUrl": f"{prov}/setup", "callbackUrl": f"{prov}/callback", "tokenUrl": f"{prov}/token",
        "clientId": "portal", "clientSecret": "test-only-provider-secret"})
    t.check("C10-M1: provider stores its provider configuration", r.status_code == 200, r.text[:200])
    erika = t.token(base, "erika")
    r = t.call("POST", f"{base}/api/c10-m1/subscriptions", erika, json={"offerId": oid})
    if not t.check("C10-M1: customer (scenario 1 company) subscribes", r.status_code == 201, r.text[:200]):
        return
    sid = r.json()["offerSubscriptionId"]
    p = t.process(base, "otto", "subscription-requested", sid, ("COMPLETED", "FAILED"))
    calls = t.call("GET", f"{prov}/calls").json()
    setup = next((c["body"] for c in calls if c["kind"] == "setup" and c["body"]["offerSubscriptionId"] == sid), {})
    t.check("C12 notify-provider: provider system receives setup with the customer's name",
            p and p["status"] == "COMPLETED" and setup.get("customerName") == "Beispiel Maschinenbau GmbH",
            t.trace(p) + f" setup={setup}")
    r = t.call("POST", f"{base}/api/c10-m1/subscriptions/{sid}/start-setup", pia,
               json={"instanceUrl": "https://demo-app.customer.example"})
    t.check("C10-M1: provider starts the setup -> SubscriptionSetupStarted", r.status_code == 200, r.text[:200])
    p = t.process(base, "otto", "subscription-setup", sid, ("WAITING", "FAILED"))
    st = {s["stepKey"]: s["state"] for s in (p or {}).get("steps", [])}
    t.check("C12 subscription-setup: instance client (C2-M2) and technical user (C2-M3) DONE, WAITING for activation",
            p and p["status"] == "WAITING" and st.get("create-instance-client") == "DONE"
            and st.get("create-technical-user") == "DONE", t.trace(p))
    r = t.call("POST", f"{base}/api/c10-m1/subscriptions/{sid}/activate", pia)
    t.check("C10-M1: provider activates (setup WAITING at C12) -> SubscriptionActivated", r.status_code == 200, r.text[:200])
    p = t.process(base, "otto", "subscription-setup", sid, ("COMPLETED", "FAILED"))
    t.check("C12: enable-instance-client and send-activation-callback -> COMPLETED", p and p["status"] == "COMPLETED",
            t.trace(p))
    calls = t.call("GET", f"{prov}/calls").json()
    cb = next((c for c in calls if c["kind"] == "callback" and c["body"]["offerSubscriptionId"] == sid), {})
    body = cb.get("body", {})
    t.check("provider callback: client ID of the instance, technical user with a secret, instance URL",
            bool(body.get("clientId")) and bool((body.get("technicalUser") or {}).get("clientId")) and cb.get("hasSecret")
            and body.get("instanceUrl") == "https://demo-app.customer.example", json.dumps(body))
    s = t.call("GET", f"{base}/api/c10-m1/subscriptions/{sid}", erika).json()
    t.check("C10-M1: customer sees the subscription ACTIVE, setup COMPLETED",
            s.get("status") == "ACTIVE" and s.get("setupStatus") == "COMPLETED", json.dumps(s))


# ============================================================================ scenario 4
def scenario_4(t: Target) -> None:
    issuer = "issuer of the CSP" if t.csp_issuer else "credentials from the stand-in STUB (OP-53)"
    t.scenario(f"Scenario 4 - OSP B as a second installation, wallet of the CSP, {issuer}")
    if not t.check("OSP B installation is running", bool(t.osp_b)):
        return
    # Gerd and Rita: scaffold-only test persons, FRAMEWORK 7a has none for OSP B
    if not onboarding(t, t.osp_b, "gerd", "rita", "rita", "werkzeugbau", OSP_B_ID, "osp-b-ui"):
        return
    bpn = t.ctx.get("werkzeugbau.bpn")
    wallets = t.call("GET", f"{t.csp}/admin/wallets").json()
    w = next((x for x in wallets if x["bpn"] == bpn), {})
    cbs = [c for c in t.call("GET", f"{t.csp}/admin/callbacks").json() if c["body"].get("bpn") == bpn]
    if t.csp_issuer:
        t.check("CSP wallet: dataspace wallet of the company holds the BPN and membership credentials",
                sorted(c["type"] for c in w.get("credentials", [])) == ["bpn", "membership"], json.dumps(w)[:300])
        t.check("CSP issuer called back to C4-M1 of OSP B (status 200)", cbs and all(c["status"] == 200 for c in cbs),
                json.dumps(cbs)[:300])
    else:
        t.check("CSP wallet: dataspace wallet of the company exists at the CSP", bool(w), json.dumps(wallets)[:300])
        t.check("route STUB: no credential in the CSP wallet and no callback of the CSP issuer (OP-53)",
                not w.get("credentials") and not cbs, json.dumps({"wallet": w, "callbacks": cbs})[:300])
    a = t.call("GET", f"{t.osp_b}/api/c3-m1/osp/companies/{t.ctx['maschinenbau.companyId']}", t.token(t.osp_b, "rita")) \
        if t.ctx.get("maschinenbau.companyId") else None
    t.check("installations are separate: OSP B does not know the company of OSP A", a is None or a.status_code == 404)
    r = t.call("GET", f"{t.osp_b}/api/c12-m1/processes", t.token(t.osp_a, "otto"))
    t.check("a token of OSP A is rejected by OSP B (401)", r.status_code == 401, str(r.status_code))


SCENARIOS = {"1": scenario_1, "2": scenario_2, "3": scenario_3, "4": scenario_4}


def run(t: Target, which: list[str]) -> bool:
    for n in which:
        try:
            SCENARIOS[n](t)
        except Exception as e:  # noqa: BLE001 - a crash is a result too
            t.check(f"aborted with {type(e).__name__}", False, str(e)[:300])
    t.log("\n=== Summary")
    ok_all = True
    for name, steps in t.results.items():
        ok = sum(1 for _, o, _ in steps if o)
        ok_all &= ok == len(steps)
        t.log(f"  {name}: {ok}/{len(steps)} checks OK")
    return ok_all


# ============================================================================ processes
def _wait(url: str, timeout: float = 30) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if http.request("GET", url, timeout=2).status_code == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.3)
    return False


def start_processes(with_b: bool, csp_issuer: bool = False) -> tuple[Target, list[subprocess.Popen]]:
    rt = ROOT / "runtime"
    rt.mkdir(exist_ok=True)
    ports = {k: int(os.environ.get(f"DEMO_{k}_PORT", v)) for k, v in (("CSP", 8100), ("OSP_A", 8000), ("OSP_B", 8001))}
    csp, a, b = (f"http://localhost:{ports[k]}" for k in ("CSP", "OSP_A", "OSP_B"))
    keys = {OSP_A_ID: ec_jwk("osp-a-1"), OSP_B_ID: ec_jwk("osp-b-1")}
    (rt / "osp-a-key.json").write_text(json.dumps(keys[OSP_A_ID][0]))
    (rt / "osp-b-key.json").write_text(json.dumps(keys[OSP_B_ID][0]))
    trust = {OSP_A_ID: {"name": "OSP A", "installationUrl": a, "jwk": keys[OSP_A_ID][1],
                        "iamTokenUrl": f"{a}{REALM}/protocol/openid-connect/token"},
             OSP_B_ID: {"name": "OSP B", "installationUrl": b, "jwk": keys[OSP_B_ID][1],
                        "iamTokenUrl": f"{b}{REALM}/protocol/openid-connect/token"}}
    (rt / "csp-trust.json").write_text(json.dumps(trust, indent=2))
    common = {**os.environ, "PYTHONWARNINGS": "ignore", "CSP_URL": f"{csp}/v1", "CSP_TOKEN_URL": f"{csp}/oauth/token",
              "CSP_JWKS_URL": f"{csp}/.well-known/jwks.json", "RETRY_BASE_SECONDS": "1", "RETRY_MAX_SECONDS": "4",
              "WORKER_POLL_SECONDS": "0.5", "AWAIT_TIMEOUT": "PT60S"}
    envs = [("csp", ports["CSP"], "tools.csp_standin.app:app", False,
             {**common, "CSP_TRUST_FILE": str(rt / "csp-trust.json"), "CSP_PUBLIC_URL": csp,
              "CSP_SIGNING_KEY_FILE": str(rt / "csp-key.json")}),
            ("osp-a", ports["OSP_A"], "scaffold.app:create_app", True,
             {**common, "SETTINGS_FILE": str(ROOT / "config/osp-a/installation.json"), "BASE_URL": a,
              "OSP_SIGNING_KEY_FILE": str(rt / "osp-a-key.json")})]
    if with_b:
        envs.append(("osp-b", ports["OSP_B"], "scaffold.app:create_app", True,
                     {**common, "SETTINGS_FILE": str(ROOT / "config/osp-b/installation.json"), "BASE_URL": b,
                      "OSP_SIGNING_KEY_FILE": str(rt / "osp-b-key.json"), "CSP_WALLET_URL": f"{csp}/wallet/v1",
                      **({"CSP_ISSUER_URL": f"{csp}/issuer/v1"} if csp_issuer else {})}))
    procs = []
    for name, port, target, factory, env in envs:
        cmd = [sys.executable, "-m", "uvicorn", target, "--port", str(port), "--log-level", "warning"]
        if factory:
            cmd.insert(3, "--factory")
        procs.append(subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=open(rt / f"{name}.log", "w"), stderr=subprocess.STDOUT))
    for url in (csp, a) + ((b,) if with_b else ()):
        if not _wait(f"{url}/health"):
            stop(procs)
            raise SystemExit(f"{url} did not start; see runtime/*.log")
    return Target(osp_a=a, csp=csp, osp_b=b if with_b else None, csp_issuer=csp_issuer), procs


def stop(procs: list[subprocess.Popen]) -> None:
    for p in procs:
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scenarios", default="1,2,3,4")
    ap.add_argument("--no-start", action="store_true", help="use running processes (DEMO_*_URL or default ports)")
    ap.add_argument("--serve", action="store_true", help="only start CSP stand-in, OSP A and OSP B and keep them running")
    ap.add_argument("--csp-issuer", action="store_true",
                    help="OSP B issues through the issuer of the CSP stand-in (Community Days; not used at the hackathon, OP-53)")
    args = ap.parse_args()
    which = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    if args.serve:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        t, procs = start_processes(with_b=True, csp_issuer=args.csp_issuer)
        print(f"CSP stand-in {t.csp}, OSP A {t.osp_a}/docs, OSP B {t.osp_b}/docs - Ctrl-C stops them", flush=True)
        try:
            while all(p.poll() is None for p in procs):
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop(procs)
        return 0
    if args.no_start:
        t, procs = Target(os.environ.get("DEMO_OSP_A_URL", "http://localhost:8000"),
                          os.environ.get("DEMO_CSP_URL", "http://localhost:8100"),
                          os.environ.get("DEMO_OSP_B_URL"), csp_issuer=args.csp_issuer), []
    else:
        t, procs = start_processes(with_b="4" in which, csp_issuer=args.csp_issuer)
    try:
        ok = run(t, which)
    finally:
        stop(procs)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
