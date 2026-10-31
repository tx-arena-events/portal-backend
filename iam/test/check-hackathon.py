#!/usr/bin/env python3
# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Check the start state of the variant hackathon against testdata/ (FRAMEWORK 7a)."""
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools import keycloak_login as login  # noqa: E402
from tools.keycloak_admin import KeycloakAdmin, reset_tokens  # noqa: E402

APPLICANT = {"Applicant", "register_company", "accept_invitation", "view_notifications"}
failures = 0


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def check(condition, text):
    global failures
    print(("PASS " if condition else "FAIL ") + text)
    failures += 0 if condition else 1


for realm, file in (("CX-Central", "persons-osp-a.json"), ("CX-Central-B", "persons-osp-b.json")):
    persons = json.loads((ROOT / "testdata" / file).read_text(encoding="utf-8"))["persons"]
    for person in persons:
        c = claims(login.person_token(person["key"], realm)["access_token"])
        roles = set(c["resource_access"]["portal-core"]["roles"])
        wanted = set(person.get("portalRoles", [])) | {"Applicant"}
        ok = c["sub"] == person["id"] and c["iss"] == f"{login.iam_url()}/realms/{realm}" and wanted <= roles and APPLICANT <= roles
        ok = ok and c.get("company_id") == person.get("companyId") and c.get("bpn") == person.get("bpn")
        ok = ok and c["given_name"] == person["givenName"] and c["family_name"] == person["familyName"]
        check(ok, f"{realm} {person['key']}: sub, issuer, name, binding, roles")
    status, tokens = login.client_token("sa-div-callback", realm)
    check(status == 200 and claims(tokens["access_token"])["resource_access"]["portal-core"]["roles"] == ["submit_wallet_callback"], f"{realm} sa-div-callback: exactly one permission")
    for folder in sorted((ROOT / "modules").iterdir()):
        if (folder / "__init__.py").exists():
            client = f"svc-{folder.name.replace('_', '-')}"
            status, tokens = login.client_token(client, realm)
            names = set(claims(tokens["access_token"]).get("resource_access", {}).get("portal-core", {}).get("roles", [])) if status == 200 else set()
            check({"query_c3_m1", "execute_steps_c10_m1", "query_c12_m1", "rotate_subscription_credentials"} <= names, f"{realm} {client}: steps and queries")
    os.environ.update(IAM_URL=login.iam_url(), IAM_REALM=realm)
    reset_tokens()
    bound = [p for p in persons if p.get("companyId")]
    for person in bound:
        users = KeycloakAdmin("c2-m2").users_of_company(person["companyId"])
        expected = {p["id"] for p in bound if p["companyId"] == person["companyId"]}
        check(set(users) == expected, f"{realm} {person['key']}: the company has exactly its persons")
        for role in person.get("portalRoles", []):
            check(person["id"] in KeycloakAdmin("c2-m2").users_of_company(person["companyId"], role), f"{realm} {person['key']}: found by {role}")
    check(KeycloakAdmin("c2-m2").users_with_portal_role("no such role") == [], f"{realm}: unknown portal role finds nobody")
    check("Dataspace Discovery" in KeycloakAdmin("c2-m3").technical_roles, f"{realm}: technical roles readable by svc-c2-m3")
sys.exit(1 if failures else 0)
