# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""In-memory Portal IAM of one installation for tests (stand-in for the Keycloak realm of C2-M5).

- Permissions and portal roles are read from the spec (C2-M5 README 6.2, 6.3); technical user
  roles from 6.4. No hand-made copy that could drift.
- Tokens as in C2-M5 7.5, signed RS256 with a key per installation, `iss` =
  `{IAM_URL}/realms/{IAM_REALM}`. JWKS, discovery and token endpoint under the paths of
  C2-M5 `openapi.yaml`; mount `router` with prefix `/auth`.
- Token endpoint: client credentials with secret, or with `private_key_jwt` for the CSP service
  accounts (`sa-csp`, `sa-csp-issuer`, `sa-csp-wallet`), checked against `CSP_JWKS_URL`.
- Test only: `GET .../test/tokens/{person}` returns the token of a test person (key or ID), as
  `get-token.py` does for the Keycloak test installation (C2-M5 14.4).
- Python API for the C2 modules (bind a person, portal roles, technical users): the Keycloak
  Admin API is not simulated.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import uuid

import jwt
from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from portal_common import http, settings, spec, tokens
from tools.keys import rsa_key

PORTAL_CORE = tokens.AUDIENCE
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
TOKEN_SECONDS = 300
CSP_CLIENTS = {"sa-csp": "report_csp_notification", "sa-csp-issuer": "report_credential_status",
               "sa-csp-wallet": "report_credential_status"}


# ---------------------------------------------------------------- spec parsing (C2-M5 6.2 - 6.4)
def _section(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


def _c2m5() -> str:
    folder = next(p.parent.name for p in spec.spec_dir().glob("C2-M5*/README.md"))
    return spec.readme(folder)


def spec_portal_roles() -> dict[str, set[str]]:
    """Portal role -> permissions; 'same as X, plus ...' is resolved, '; later ...' is left out.
    MINIMAL: parses the markdown table of 6.3; adapt the regex if the table layout changes."""
    roles: dict[str, set[str]] = {}
    for line in _section(_c2m5(), "### 6.3", "### 6.4").splitlines():
        m = re.match(r"\| `([A-Za-z ]+)` \| [^|]+ \| ([^|]+) \|", line)
        if not m:
            continue
        contains = m.group(2).partition("; later")[0]
        perms = set(re.findall(r"`([a-z0-9_]+)`", contains))
        if base := re.match(r"same as `([A-Za-z ]+)`", contains):
            perms |= roles[base.group(1)]
        roles[m.group(1)] = perms
    return roles


def spec_technical_roles() -> set[str]:
    out: set[str] = set()
    for line in _section(_c2m5(), "### 6.4", "### 6.5").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Role", "") or cells[0].startswith("---") or "dropped" in cells[2]:
            continue
        out |= {n.strip() for n in cells[0].split(",")}
    return out


# ---------------------------------------------------------------- realm
class IAM:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.key, self.public_jwk = rsa_key(f"test-{uuid.uuid4().hex[:8]}")
        self.portal_roles = spec_portal_roles()
        self.technical_roles = spec_technical_roles()
        self.users: dict[str, dict] = {}       # id -> {givenName, familyName, roles, companyId, bpn, key}
        self.clients: dict[str, dict] = {}     # clientId -> {secret, permissions, jwksUrl, companyId, bpn, roles}
        self._seed()

    # -- seed: configuration of the realm (C2-M5 5.9, 14.2) -----------------------------------
    def _seed(self) -> None:
        secret = os.environ.get("SERVICE_CLIENT_SECRET", tokens.DEFAULT_SERVICE_SECRET)
        every = spec.service_permissions()
        for mid in spec.modules():
            # MINIMAL: every svc-<module> gets every step and query permission (like svc-test-internal);
            # upgrade: exactly the permissions named in section 11 of the calling module.
            self.clients[f"svc-{mid}"] = {"secret": secret, "permissions": set(every)}
        for cid, perm in CSP_CLIENTS.items():
            self.clients[cid] = {"jwksUrl": os.environ.get("CSP_JWKS_URL"), "permissions": {perm}}
        self.clients["sa-credential-issuer"] = {"secret": os.environ.get("CREDENTIAL_ISSUER_CALLBACK_SECRET",
                                                                          "test-only-issuer-secret"),
                                                "permissions": {"report_credential_status"}}
        file = settings.resolve_path(settings.get("testPersonsFile"))
        if file and file.exists():
            for p in json.loads(file.read_text(encoding="utf-8"))["persons"]:
                self.first_login(p["id"], p["givenName"], p["familyName"], key=p.get("key"))
                self.add_portal_roles(p["id"], p.get("portalRoles", []))
                if p.get("companyId"):
                    self.bind(p["id"], p["companyId"], p.get("bpn"))

    # -- people ------------------------------------------------------------------------------
    def first_login(self, uid: str, given: str, family: str, key: str | None = None) -> str:
        """First sign-in through the EBW-Connector creates the account with `Applicant` (C2-M5 5.6)."""
        with self.lock:
            u = self.users.setdefault(uid, {"roles": {"Applicant"}, "companyId": None, "bpn": None})
            u.update(givenName=given, familyName=family)
            if key:
                u["key"] = key
            return uid

    def find(self, person: str) -> str | None:
        if person in self.users:
            return person
        return next((uid for uid, u in self.users.items() if u.get("key") == person), None)

    def add_portal_roles(self, uid: str, roles: list[str]) -> None:
        unknown = set(roles) - set(self.portal_roles)
        if unknown:
            raise ValueError(f"unknown portal roles {sorted(unknown)}")
        with self.lock:
            self.users[uid]["roles"] |= set(roles)

    def remove_portal_roles(self, uid: str, roles: list[str]) -> None:
        with self.lock:
            self.users[uid]["roles"] -= set(roles) - {"Applicant"}

    def bind(self, uid: str, company_id: str | None, bpn: str | None) -> None:
        with self.lock:
            self.users[uid].update(companyId=company_id, bpn=bpn)

    def person(self, uid: str) -> dict:
        u = self.users[uid]
        return {"id": uid, "givenName": u["givenName"], "familyName": u["familyName"], "companyId": u["companyId"],
                "bpn": u["bpn"], "portalRoles": sorted(u["roles"])}

    def permissions_of(self, uid: str) -> list[str]:
        roles = self.users[uid]["roles"]
        out = set(roles)
        for r in roles:
            out |= self.portal_roles.get(r, set())
        return sorted(out)

    # -- technical users (C2-M3) -------------------------------------------------------------
    def create_technical_user(self, client_id: str, company_id: str, bpn: str | None, roles: list[str]) -> str:
        unknown = set(roles) - self.technical_roles
        if unknown:
            raise ValueError(f"unknown technical user roles {sorted(unknown)}")
        with self.lock:
            self.clients[client_id] = {"secret": secrets.token_urlsafe(24), "permissions": set(),
                                       "technicalRoles": set(roles), "companyId": company_id, "bpn": bpn}
            return self.clients[client_id]["secret"]

    def rotate_secret(self, client_id: str) -> str:
        with self.lock:
            self.clients[client_id]["secret"] = secrets.token_urlsafe(24)
            return self.clients[client_id]["secret"]

    # -- tokens (C2-M5 7.5) ------------------------------------------------------------------
    def _sign(self, claims: dict) -> str:
        now = int(time.time())
        body = {"iss": tokens.issuer(), "iat": now, "exp": now + TOKEN_SECONDS, "jti": str(uuid.uuid4()),
                "typ": "Bearer", **claims}
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": self.public_jwk["kid"]})

    def person_token(self, uid: str) -> str:
        u = self.users[uid]
        claims = {"aud": PORTAL_CORE, "azp": "portal-ui", "sub": uid, "given_name": u["givenName"],
                  "family_name": u["familyName"], "resource_access": {PORTAL_CORE: {"roles": self.permissions_of(uid)}}}
        if u["companyId"]:
            claims |= {"company_id": u["companyId"], "bpn": u["bpn"]}
        return self._sign(claims)

    def client_token(self, client_id: str) -> str:
        c = self.clients[client_id]
        if c.get("technicalRoles"):   # technical user: never portal-core (C2-M5 7.5)
            return self._sign({"aud": ["technical_roles_management"], "azp": client_id, "sub": f"sa-{client_id}",
                               "company_id": c["companyId"], "bpn": c["bpn"],
                               "resource_access": {"technical_roles_management": {"roles": sorted(c["technicalRoles"])}}})
        return self._sign({"aud": PORTAL_CORE, "azp": client_id, "sub": f"service-account-{client_id}",
                           "resource_access": {PORTAL_CORE: {"roles": sorted(c["permissions"])}}})

    def _check_assertion(self, client_id: str, assertion: str) -> bool:
        url = self.clients[client_id].get("jwksUrl")
        if not url:
            return False
        try:
            keys = http.request("GET", url).json()["keys"]
            kid = jwt.get_unverified_header(assertion).get("kid")
            jwk = next(k for k in keys if k.get("kid") == kid or kid is None)
            c = jwt.decode(assertion, jwt.PyJWK(jwk).key, algorithms=["ES256", "PS256", "RS256"],
                           audience=[tokens.issuer(), tokens.token_endpoint()])
            return c.get("iss") == client_id and c.get("sub") == client_id and c["exp"] - c.get("iat", c["exp"]) <= 60
        except Exception:  # noqa: BLE001 - every failure is invalid_client
            return False

    def token_request(self, form: dict) -> tuple[int, dict]:
        if form.get("grant_type") != "client_credentials":
            return 400, {"error": "unsupported_grant_type"}
        client_id = form.get("client_id")
        if form.get("client_assertion_type") == ASSERTION_TYPE and form.get("client_assertion"):
            try:
                client_id = jwt.decode(form["client_assertion"], options={"verify_signature": False})["sub"]
            except jwt.PyJWTError:
                return 401, {"error": "invalid_client"}
            ok = client_id in self.clients and self._check_assertion(client_id, form["client_assertion"])
        else:
            c = self.clients.get(client_id or "")
            ok = bool(c and c.get("secret") and secrets.compare_digest(c["secret"], form.get("client_secret") or ""))
        if not ok:
            return 401, {"error": "invalid_client"}
        return 200, {"access_token": self.client_token(client_id), "token_type": "Bearer", "expires_in": TOKEN_SECONDS}


_iam: IAM | None = None


def iam() -> IAM:
    global _iam
    if _iam is None:
        _iam = IAM()
    return _iam


def reset() -> None:
    global _iam
    _iam = None


# ---------------------------------------------------------------- HTTP (paths of C2-M5 openapi.yaml)
router = APIRouter(prefix="/auth", tags=["Test IAM (C2-M5 stand-in)"])


def _realm(realm: str) -> None:
    if realm != tokens.realm():
        raise HTTPException(404, "realm unknown")


@router.get("/realms/{realm}/.well-known/openid-configuration")
def discovery(realm: str):
    _realm(realm)
    base = f"{tokens.iam_url()}/realms/{realm}/protocol/openid-connect"
    return {"issuer": tokens.issuer(), "jwks_uri": f"{base}/certs", "token_endpoint": f"{base}/token",
            "authorization_endpoint": f"{base}/auth", "grant_types_supported": ["client_credentials", "authorization_code"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "private_key_jwt"]}


@router.get("/realms/{realm}/protocol/openid-connect/certs")
def certs(realm: str):
    _realm(realm)
    return {"keys": [iam().public_jwk]}


@router.post("/realms/{realm}/protocol/openid-connect/token")
def token(realm: str, grant_type: str = Form(...), client_id: str | None = Form(None),
          client_secret: str | None = Form(None), client_assertion_type: str | None = Form(None),
          client_assertion: str | None = Form(None)):
    _realm(realm)
    status, body = iam().token_request({"grant_type": grant_type, "client_id": client_id, "client_secret": client_secret,
                                        "client_assertion_type": client_assertion_type,
                                        "client_assertion": client_assertion})
    return JSONResponse(body, status_code=status)


@router.get("/realms/{realm}/test/tokens/{person}")
def test_token(realm: str, person: str):
    """Test only: token of a test person by key (`erika`) or ID, as after sign-in or refresh."""
    _realm(realm)
    uid = iam().find(person)
    if not uid:
        raise HTTPException(404, "test person unknown")
    return {"access_token": iam().person_token(uid), "token_type": "Bearer", "expires_in": TOKEN_SECONDS,
            "person": iam().person(uid)}


@router.post("/realms/{realm}/test/persons/{uid}")
def test_first_login(realm: str, uid: str, body: dict):
    """Test only: first sign-in of a new person (account with `Applicant`)."""
    _realm(realm)
    iam().first_login(uid, body.get("givenName", ""), body.get("familyName", ""), key=body.get("key"))
    return iam().person(uid)
