# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Writes to the Keycloak test installation through the Admin API, for the stand-ins of C2-M2 and C2-M3.

Same methods as the in-memory IAM of `tools/test_iam.py`. A team that builds C2-M2 or C2-M3 calls
the Admin API itself, as its spec describes (C2-M3 11.1). `reset_realm()` restores the start state
of the realm from its realm file.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib.parse import quote

from portal_common import http, settings, tokens

PORTAL_CORE = tokens.AUDIENCE
TECHNICAL_ROLES_CLIENT = "technical_roles_management"
_lock = threading.Lock()
_tokens: dict[str, tuple[str, float]] = {}


def reset_tokens() -> None:
    with _lock:
        _tokens.clear()


def _token_of(cache_key: str, url: str, form: dict) -> str:
    with _lock:
        cached = _tokens.get(cache_key)
        if cached and cached[1] > time.time() + 30:
            return cached[0]
    r = http.request("POST", url, data=form)
    if r.status_code != 200:
        raise RuntimeError(f"Portal IAM token endpoint answered {r.status_code} for {cache_key}")
    body = r.json()
    with _lock:
        _tokens[cache_key] = (body["access_token"], time.time() + int(body.get("expires_in", 60)))
    return body["access_token"]


def _master_token() -> str:
    return _token_of("master", f"{tokens.iam_url()}/realms/master/protocol/openid-connect/token", {
        "grant_type": "password", "client_id": "admin-cli",
        "username": os.environ.get("KEYCLOAK_ADMIN_USER", "admin"),
        "password": os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin-test-only")})


def realm_file() -> Path:
    """The realm file of this installation: `iamRealmFile` of the settings, `IAM_REALM_DIR` replaces its folder."""
    file = settings.resolve_path(settings.get("iamRealmFile"))
    if not file:
        raise RuntimeError(f"setting iamRealmFile is missing in {settings.get('_file')}")
    folder = os.environ.get("IAM_REALM_DIR")
    return Path(folder) / file.name if folder else file


def reset_realm() -> None:
    """Restores the start state of the realm from its realm file: accounts and their roles as in the file, further
    accounts and clients are deleted. The realm itself stays, so its keys stay valid for the stand-in of the
    EBW-Connector and for running services. A realm that does not exist yet is imported."""
    wanted = json.loads(realm_file().read_text(encoding="utf-8"))
    reset_tokens()
    headers = {"Authorization": f"Bearer {_master_token()}"}
    base = f"{tokens.iam_url()}/admin/realms/{tokens.realm()}"

    def call(method: str, path: str, **kw):
        r = http.request(method, f"{base}{path}", headers=headers, timeout=60, **kw)
        if not r.is_success:
            raise RuntimeError(f"resetting the realm: {method} {path} answered {r.status_code}: {r.text[:200]}")
        return r

    if http.request("GET", base, headers=headers).status_code == 404:
        r = http.request("POST", f"{tokens.iam_url()}/admin/realms", headers=headers, json=wanted, timeout=120)
        if r.status_code != 201:
            raise RuntimeError(f"importing {realm_file().name} answered {r.status_code}: {r.text[:300]}")
        return
    people = {u["id"]: u for u in wanted["users"] if u.get("federatedIdentities")}
    for user in call("GET", "/users", params={"max": 1000}).json():
        if user["id"] not in people and not user["username"].startswith("service-account-"):
            call("DELETE", f"/users/{user['id']}")
    clients = {c["clientId"] for c in wanted["clients"]}
    for client in call("GET", "/clients", params={"max": 1000}).json():
        if client["clientId"] not in clients:
            call("DELETE", f"/clients/{client['id']}")
    core = call("GET", "/clients", params={"clientId": PORTAL_CORE}).json()[0]["id"]
    for uid, person in people.items():
        rep = call("GET", f"/users/{uid}").json()
        rep["attributes"] = person.get("attributes", {})
        call("PUT", f"/users/{uid}", json=rep)
        mapped = call("GET", f"/users/{uid}/role-mappings/clients/{core}").json()
        if mapped:
            call("DELETE", f"/users/{uid}/role-mappings/clients/{core}", json=mapped)
        roles = [call("GET", f"/clients/{core}/roles/{quote(name)}").json() for name in person["clientRoles"][PORTAL_CORE]]
        call("POST", f"/users/{uid}/role-mappings/clients/{core}", json=roles)
        call("POST", f"/users/{uid}/logout")
    reset_tokens()


class KeycloakAdmin:
    """Admin API of the realm of this installation, with the service account `svc-<module>`."""

    def __init__(self, module: str) -> None:
        self.client_id = f"svc-{module}"

    def _call(self, method: str, path: str, **kw):
        token = _token_of(self.client_id, tokens.token_endpoint(), {
            "grant_type": "client_credentials", "client_id": self.client_id,
            "client_secret": os.environ.get("SERVICE_CLIENT_SECRET", tokens.DEFAULT_SERVICE_SECRET)})
        return http.request(method, f"{tokens.iam_url()}/admin/realms/{tokens.realm()}{path}",
                            headers={"Authorization": f"Bearer {token}"}, **kw)

    def _ok(self, r, what: str):
        if not r.is_success:
            raise RuntimeError(f"{what}: Portal IAM answered {r.status_code}: {r.text[:200]}")
        return r

    def _client_uuid(self, client_id: str) -> str | None:
        found = self._ok(self._call("GET", "/clients", params={"clientId": client_id}), "search client").json()
        return found[0]["id"] if found else None

    def _role(self, client_uuid: str, name: str) -> dict | None:
        r = self._call("GET", f"/clients/{client_uuid}/roles/{quote(name)}")
        return None if r.status_code == 404 else self._ok(r, f"read role {name}").json()

    # -- people -----------------------------------------------------------------------------
    def find(self, person: str) -> str | None:
        return person if self._call("GET", f"/users/{person}").status_code == 200 else None

    def person(self, uid: str) -> dict:
        u = self._ok(self._call("GET", f"/users/{uid}"), "read account").json()
        attrs = u.get("attributes") or {}
        core = self._client_uuid(PORTAL_CORE)
        roles = self._ok(self._call("GET", f"/users/{uid}/role-mappings/clients/{core}"), "read roles").json()
        return {"id": uid, "givenName": u.get("firstName"), "familyName": u.get("lastName"),
                "companyId": (attrs.get("company_id") or [None])[0], "bpn": (attrs.get("bpn") or [None])[0],
                "portalRoles": sorted(r["name"] for r in roles)}

    def bind(self, uid: str, company_id: str | None, bpn: str | None) -> None:
        u = self._ok(self._call("GET", f"/users/{uid}"), "read account").json()
        attrs = u.get("attributes") or {}
        for key, value in (("company_id", company_id), ("bpn", bpn)):
            if value:
                attrs[key] = [value]
            else:
                attrs.pop(key, None)
        u["attributes"] = attrs
        self._ok(self._call("PUT", f"/users/{uid}", json=u), "bind account")

    def add_portal_roles(self, uid: str, roles: list[str]) -> None:
        core = self._client_uuid(PORTAL_CORE)
        found = [self._role(core, name) for name in roles]
        if None in found:
            raise ValueError(f"unknown portal roles in {roles}")
        self._ok(self._call("POST", f"/users/{uid}/role-mappings/clients/{core}", json=found), "assign portal roles")

    def users_with_portal_role(self, role: str) -> list[str]:
        core = self._client_uuid(PORTAL_CORE)
        r = self._call("GET", f"/clients/{core}/roles/{quote(role)}/users", params={"max": 1000})
        return [] if r.status_code == 404 else [u["id"] for u in self._ok(r, "read role users").json()]

    def users_of_company(self, company_id: str, portal_role: str | None = None) -> list[str]:
        r = self._call("GET", "/users", params={"q": f"company_id:{company_id}", "max": 1000})
        ids = [u["id"] for u in self._ok(r, "read company users").json() if not u["username"].startswith("service-account-")]
        if portal_role:
            with_role = set(self.users_with_portal_role(portal_role))
            ids = [uid for uid in ids if uid in with_role]
        return ids

    # -- technical users (C2-M3 6.6, 11.1) ----------------------------------------------------
    @property
    def technical_roles(self) -> set[str]:
        r = self._call("GET", f"/clients/{self._client_uuid(TECHNICAL_ROLES_CLIENT)}/roles", params={"max": 1000})
        return {role["name"] for role in self._ok(r, "read technical roles").json()}

    def create_technical_user(self, client_id: str, company_id: str, bpn: str | None, roles: list[str]) -> str:
        technical = self._client_uuid(TECHNICAL_ROLES_CLIENT)
        found = [self._role(technical, name) for name in roles]
        unknown = [name for name, role in zip(roles, found) if role is None]
        if unknown:
            raise ValueError(f"unknown technical user roles {sorted(unknown)}")
        uuid = self._client_uuid(client_id)
        if uuid is None:
            r = self._call("POST", "/clients", json={
                "clientId": client_id, "name": client_id, "enabled": True, "publicClient": False,
                "standardFlowEnabled": False, "implicitFlowEnabled": False, "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True, "fullScopeAllowed": False, "protocol": "openid-connect",
                "clientAuthenticatorType": "client-secret", "attributes": {"client_credentials.use_refresh_token": "false"},
                "defaultClientScopes": ["basic", "roles", "catena", "technical-roles"], "optionalClientScopes": []})
            self._ok(r, "create technical user")
            uuid = self._client_uuid(client_id)
        user = self._ok(self._call("GET", f"/clients/{uuid}/service-account-user"), "read service account").json()
        self.bind(user["id"], company_id, bpn)
        if found:
            self._ok(self._call("POST", f"/users/{user['id']}/role-mappings/clients/{technical}", json=found), "assign technical roles")
        return self._ok(self._call("GET", f"/clients/{uuid}/client-secret"), "read secret").json()["value"]

    def rotate_secret(self, client_id: str) -> str:
        uuid = self._client_uuid(client_id)
        return self._ok(self._call("POST", f"/clients/{uuid}/client-secret"), "rotate secret").json()["value"]
