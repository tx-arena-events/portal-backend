# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Shared CSP stand-in for OSP A and OSP B, run as a process of its own:

    uv run uvicorn tools.csp_standin.app:app --port 8100

APIs (proposals of the spec; one data set behind all of them):
- `POST /oauth/token`: client credentials with `private_key_jwt`; client = `ospId` from the trust
  list, key = its JWK (C3-M2 section 4).
- `/v1/...`: core services API (C3-M2 11.1), plus read side `/v1/directory/{bpn}`,
  `/v1/discovery/{bpn}`, `/v1/trust-list` (not in the spec).
- `/issuer/v1/holders/...`: issuer of the CSP (C4-M1 11.5).
- `/wallet/v1/wallets/...`: dataspace wallet of the CSP (C4-M2 11.3).
- `/identity/v1/participants/{participantContextId}/credentials/request`: identity API of the
  CSP wallet (C4-M1 11.4/11.5 step 3). Path and body are provisional: the spec refers to the
  identity interface of the IdentityHub without naming it.
- Callbacks to the installations with a token of their Portal IAM (`sa-csp`, `sa-csp-issuer`,
  `private_key_jwt` with the CSP key, JWKS at `/.well-known/jwks.json`): C3-M2
  `/csp-notifications`, C4-M1 `/issuer-callbacks/{credentialKind}`, only under the
  `installationUrl` of the OSP in the trust list.
- `/admin/...`: test tools without authentication (never expose outside a test network).

Configuration: `CSP_TRUST_FILE` (JSON `ospId -> {name, installationUrl, iamTokenUrl, jwk}`),
`CSP_STUB_FILE` (default `testdata/csp-companies.json`), `CSP_SIGNING_KEY_FILE` (created if
missing), `CSP_PUBLIC_URL`, `CSP_CALLBACK_DELAY_SECONDS`, `CSP_DATASPACE` (default `Catena-X`).
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from pathlib import Path

import httpx
import jwt
from fastapi import Body, FastAPI, Form, Header, Query, Request, Response
from fastapi.responses import JSONResponse

from portal_common import http
from portal_common.spec import ROOT
from tools.keys import ec_jwk, load_or_create_ec

from .core import CoreServices, problem

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
TOKEN_SECONDS = 300


def _reply(result: tuple[int, object]):
    status, body = result
    if body is None:
        return Response(status_code=status)
    media = "application/problem+json" if status >= 400 else None
    return JSONResponse(body, status_code=status, media_type=media)


class Csp:
    def __init__(self) -> None:
        self.public_url = os.environ.get("CSP_PUBLIC_URL", "http://localhost:8100").rstrip("/")
        self.dataspace = os.environ.get("CSP_DATASPACE", "Catena-X")
        self.delay = float(os.environ.get("CSP_CALLBACK_DELAY_SECONDS", "0.2"))
        key_file = os.environ.get("CSP_SIGNING_KEY_FILE")
        self.key, self.public_jwk = (load_or_create_ec(Path(key_file), "csp-1") if key_file else ec_jwk("csp-1"))
        self.access_secret = secrets.token_urlsafe(32)
        osps = {}
        if f := os.environ.get("CSP_TRUST_FILE"):
            osps = json.loads(Path(f).read_text())
        stub = json.loads(Path(os.environ.get("CSP_STUB_FILE") or ROOT / "testdata" / "csp-companies.json").read_text())
        self.core = CoreServices(osps, stub["companies"], notify=self.notify)
        self.holders: dict[str, str] = {}                 # bpn -> did
        self.grants: dict[tuple[str, str], dict] = {}     # (bpn, kind) -> {callbackUrl, memberOf}
        self.wallets: dict[str, dict] = {}                # bpn -> wallet
        self.callbacks: list[dict] = []                   # outbox of every callback with its result

    # -------------------------------------------------------- callbacks to an installation
    def _osp_token(self, osp_id: str, client_id: str) -> str:
        osp = self.core.osps[osp_id]
        url = osp["iamTokenUrl"]
        now = int(time.time())
        assertion = jwt.encode({"iss": client_id, "sub": client_id, "aud": url, "iat": now, "exp": now + 60,
                                "jti": str(uuid.uuid4())}, jwt.PyJWK(self.key).key, algorithm="ES256",
                               headers={"kid": self.public_jwk["kid"]})
        r = http.request("POST", url, data={"grant_type": "client_credentials", "client_assertion_type": ASSERTION_TYPE,
                                            "client_assertion": assertion})
        r.raise_for_status()
        return r.json()["access_token"]

    def callback(self, osp_id: str, client_id: str, url: str, body: dict) -> None:
        entry = {"ospId": osp_id, "url": url, "body": body, "status": None}
        self.callbacks.append(entry)
        base = (self.core.osps.get(osp_id) or {}).get("installationUrl") or ""
        if not base or not url.startswith(base.rstrip("/") + "/"):
            entry["status"] = "refused: address not under the installation in the trust list"
            return
        try:
            r = http.request("POST", url, json=body, headers={"Authorization": "Bearer " + self._osp_token(osp_id, client_id)})
            entry["status"] = r.status_code
        except (httpx.HTTPError, KeyError) as e:   # MINIMAL: no retry of callbacks
            entry["status"] = f"error: {type(e).__name__}"

    def later(self, fn, *args) -> None:
        def run():
            time.sleep(self.delay)
            fn(*args)
        threading.Thread(target=run, daemon=True).start()

    def notify(self, osp_id: str, notification: dict) -> None:
        url = (self.core.osps.get(osp_id) or {}).get("installationUrl", "").rstrip("/") + "/api/c3-m2/csp-notifications"
        self.later(self.callback, osp_id, "sa-csp", url, notification)

    # -------------------------------------------------------- authentication of the OSPs
    def token(self, form: dict):
        if form.get("grant_type") != "client_credentials" or form.get("client_assertion_type") != ASSERTION_TYPE:
            return 400, {"error": "unsupported_grant_type"}
        try:
            osp_id = jwt.decode(form["client_assertion"], options={"verify_signature": False})["sub"]
            jwk = (self.core.osps.get(osp_id) or {}).get("jwk")
            if not jwk:
                raise jwt.InvalidTokenError("OSP not on the trust list")
            c = jwt.decode(form["client_assertion"], jwt.PyJWK(jwk).key, algorithms=["ES256"],
                           options={"verify_aud": False})
            if c.get("iss") != osp_id:
                raise jwt.InvalidTokenError("iss")
        except (jwt.PyJWTError, KeyError) as e:
            return 401, {"error": "invalid_client", "error_description": str(e)}
        now = int(time.time())
        access = jwt.encode({"sub": osp_id, "iat": now, "exp": now + TOKEN_SECONDS, "aud": "csp"},
                            self.access_secret, algorithm="HS256")
        return 200, {"access_token": access, "token_type": "Bearer", "expires_in": TOKEN_SECONDS}

    def caller(self, authorization: str | None) -> str | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        try:
            return jwt.decode(authorization.removeprefix("Bearer "), self.access_secret, algorithms=["HS256"],
                              audience="csp")["sub"]
        except jwt.PyJWTError:
            return None

    # -------------------------------------------------------- issuer and wallet
    def put_holder(self, caller: str, bpn: str, body: dict):
        if err := self.core.home_check(caller, bpn):
            return (403, err[1]) if err[0] == 404 else err
        if body.get("did") != self.core.companies[bpn].get("did"):
            return problem(409, "did-mismatch")
        status = 200 if bpn in self.holders else 201
        self.holders[bpn] = body["did"]
        return status, {"bpn": bpn, "did": body["did"]}

    def put_grant(self, caller: str, bpn: str, kind: str, body: dict):
        if err := self.core.home_check(caller, bpn):
            return err
        if bpn not in self.holders:
            return problem(404, "unknown-holder")
        if kind == "membership" and body.get("memberOf") != self.dataspace:
            return problem(422, "wrong-dataspace")
        self.grants[(bpn, kind)] = {"callbackUrl": body.get("callbackUrl"), "ospId": caller}
        return 204, None

    def create_wallet(self, caller: str, body: dict):
        bpn = body.get("bpn")
        if not bpn or (err := self.core.home_check(caller, bpn)):
            return problem(403, "not-home-osp")
        if bpn in self.wallets:
            return problem(409, "wallet-exists")
        w = {"bpn": bpn, "participantContextId": str(uuid.uuid4()), "did": f"did:web:csp-wallet.example.org:{bpn}",
             "credentialServiceUrl": f"https://csp-wallet.example.org/api/credentials/v1/participants/{bpn}",
             "identityApiUrl": f"{self.public_url}/identity/v1", "apiKey": secrets.token_urlsafe(24), "accounts": {},
             "credentials": []}
        self.wallets[bpn] = w
        return 201, {k: w[k] for k in ("participantContextId", "did", "credentialServiceUrl", "identityApiUrl", "apiKey")}

    def request_credentials(self, pcid: str, api_key: str | None, body: dict):
        w = next((w for w in self.wallets.values() if w["participantContextId"] == pcid), None)
        if w is None or not api_key or not secrets.compare_digest(api_key, w["apiKey"]):
            return problem(401, "unauthorized")
        kinds = [str(c.get("type") or c.get("id") or c).lower() for c in body.get("credentials") or []]
        missing = [k for k in kinds if (w["bpn"], k) not in self.grants]
        if not kinds or missing:
            return problem(400, "validation", detail=f"no grant for {missing or 'nothing requested'}")
        for k in kinds:   # DCP with the issuer is simulated: the credential lands in the wallet
            g = self.grants[(w["bpn"], k)]
            w["credentials"].append({"type": k, "holderPid": body.get("holderPid")})
            self.later(self.callback, g["ospId"], "sa-csp-issuer", g["callbackUrl"],
                       {"bpn": w["bpn"], "status": "SUCCESSFUL", "message": f"{k} credential stored in the wallet"})
        return 202, {"holderPid": body.get("holderPid"), "state": "REQUESTED"}


def create_app() -> FastAPI:
    csp = Csp()
    app = FastAPI(title="CSP stand-in (test tool)")
    app.state.csp = csp

    def who(authorization: str | None):
        c = csp.caller(authorization)
        if c is None:
            raise _Unauthorized()
        return c

    @app.exception_handler(_Unauthorized)
    async def _unauthorized(request: Request, exc: _Unauthorized):
        return _reply(problem(401, "unauthorized"))

    @app.get("/.well-known/jwks.json")
    def jwks():
        return {"keys": [csp.public_jwk]}

    @app.post("/oauth/token")
    def token(grant_type: str = Form(...), client_assertion_type: str = Form(""), client_assertion: str = Form("")):
        status, body = csp.token({"grant_type": grant_type, "client_assertion_type": client_assertion_type,
                                  "client_assertion": client_assertion})
        return JSONResponse(body, status_code=status)

    # ---------------- core services API (C3-M2 11.1)
    @app.get("/v1/companies")
    def find(authorization: str | None = Header(None), ebwoid: str | None = None, euid: str | None = None,
             identifier: list[str] = Query(default=[]), legalName: str | None = None, country: str | None = None):
        return _reply(csp.core.find_companies(who(authorization), ebwoid, euid, identifier, legalName, country))

    @app.api_route("/v1/companies", methods=["POST"])
    @app.api_route("/v1/companies/{rest:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
    async def change(request: Request, authorization: str | None = Header(None), rest: str = ""):
        body = await request.json() if request.method != "DELETE" and await request.body() else {}
        return _reply(csp.core.dispatch(who(authorization), request.method, "/companies/" + rest, body))

    @app.get("/v1/directory/{bpn}")
    def directory(bpn: str, authorization: str | None = Header(None)):
        who(authorization)
        return _reply(csp.core.directory(bpn))

    @app.get("/v1/discovery/{bpn}")
    def discovery(bpn: str, authorization: str | None = Header(None)):
        who(authorization)
        return _reply(csp.core.discovery(bpn))

    @app.get("/v1/trust-list")
    def trust_list():
        return [{"ospId": k, "name": v.get("name"), "installationUrl": v.get("installationUrl"), "hasKey": bool(v.get("jwk"))}
                for k, v in csp.core.osps.items()]

    # ---------------- issuer of the CSP (C4-M1 11.5)
    @app.put("/issuer/v1/holders/{bpn}")
    def holder(bpn: str, body: dict = Body(...), authorization: str | None = Header(None)):
        return _reply(csp.put_holder(who(authorization), bpn, body))

    @app.put("/issuer/v1/holders/{bpn}/grants/{kind}")
    def grant(bpn: str, kind: str, body: dict = Body(...), authorization: str | None = Header(None)):
        return _reply(csp.put_grant(who(authorization), bpn, kind, body))

    @app.delete("/issuer/v1/holders/{bpn}/grants/{kind}")
    def revoke(bpn: str, kind: str, authorization: str | None = Header(None)):
        who(authorization)
        csp.grants.pop((bpn, kind), None)
        return Response(status_code=204)

    # ---------------- dataspace wallet of the CSP (C4-M2 11.3) and its identity API
    @app.post("/wallet/v1/wallets")
    def wallet(body: dict = Body(...), authorization: str | None = Header(None)):
        return _reply(csp.create_wallet(who(authorization), body))

    @app.delete("/wallet/v1/wallets/{bpn}")
    def delete_wallet(bpn: str, authorization: str | None = Header(None)):
        who(authorization)
        csp.wallets.pop(bpn, None)
        return Response(status_code=204)

    @app.post("/identity/v1/participants/{pcid}/credentials/request")
    def request_credentials(pcid: str, body: dict = Body(...), x_api_key: str | None = Header(None)):
        return _reply(csp.request_credentials(pcid, x_api_key, body))

    # ---------------- test tools
    @app.put("/admin/osps/{osp_id}")
    def put_osp(osp_id: str, body: dict = Body(...)):
        csp.core.osps[osp_id] = body
        return body

    @app.get("/admin/companies")
    def companies():
        return list(csp.core.companies.values())

    @app.get("/admin/callbacks")
    def callbacks():
        return csp.callbacks

    @app.get("/admin/wallets")
    def wallets():
        return [{k: v for k, v in w.items() if k != "apiKey"} for w in csp.wallets.values()]

    @app.put("/admin/config")
    def config(body: dict = Body(...)):
        csp.core.async_operations = bool(body.get("asyncOperations", csp.core.async_operations))
        return {"asyncOperations": csp.core.async_operations}

    @app.post("/admin/operations/{operation_id}/complete")
    def complete(operation_id: str, body: dict = Body(default={})):
        return _reply(csp.core.complete_operation(operation_id, body.get("outcome", "SUCCEEDED"), body.get("reason")))

    @app.get("/health")
    def health():
        return {"status": "UP", "osps": sorted(csp.core.osps)}

    return app


class _Unauthorized(Exception):
    pass


app = create_app()
