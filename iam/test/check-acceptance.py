#!/usr/bin/env python3
# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Run the acceptance cases of C2-M5 (README 13) against a freshly started test installation.

Start it with IAM_VARIANT=test. The cases change the realm (for example case 5 and 12), so
start again from a fresh database before a second run. Not automated: case 19 (needs the
seeding job of the portal releases) and case 22 (needs a real wallet).
"""
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TEST = Path(__file__).resolve().parent
sys.path.insert(0, str(TEST.parents[1]))
from tools import keycloak_login as gt  # noqa: E402

IAM, REALM = gt.iam_url(), "CX-Central"
ENV = gt.env_test()
results = []


def claims(token):
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def roles(token, client="portal-core"):
    return set(claims(token).get("resource_access", {}).get(client, {}).get("roles", []))


def http(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:
            text = response.read()
            return response.status, json.loads(text) if text else None
    except urllib.error.HTTPError as error:
        text = error.read()
        try:
            return error.code, json.loads(text) if text else None
        except ValueError:
            return error.code, text.decode(errors="replace")


def admin_token():
    status, tokens = gt.post_form(f"{IAM}/realms/master/protocol/openid-connect/token", {"grant_type": "password", "client_id": "admin-cli", "username": ENV["KEYCLOAK_ADMIN_USER"], "password": ENV["KEYCLOAK_ADMIN_PASSWORD"]})
    assert status == 200, tokens
    return tokens["access_token"]


def admin(method, path, body=None, realm=REALM):
    return http(method, f"{IAM}/admin/realms/{realm}{path}", admin_token(), body)


def case(number, title):
    def wrap(fn):
        try:
            fn()
            results.append((number, title, True, ""))
            print(f"PASS {number:>2} {title}")
        except Exception as error:  # a failed case must not stop the others
            results.append((number, title, False, str(error)))
            print(f"FAIL {number:>2} {title}: {str(error)[:300]}")
        return fn
    return wrap


def must(value, message="unexpected"):
    if not value:
        raise AssertionError(message)


def user_by_name(username):
    status, users = admin("GET", f"/users?username={urllib.parse.quote(username)}&exact=true")
    must(status == 200 and users, f"user {username}")
    return users[0]


def service_user(client_id):
    status, clients = admin("GET", f"/clients?clientId={client_id}")
    must(status == 200 and clients, client_id)
    status, user = admin("GET", f"/clients/{clients[0]['id']}/service-account-user")
    must(status == 200, client_id)
    return clients[0], user


def client_ref(client_id):
    status, clients = admin("GET", f"/clients?clientId={client_id}")
    must(status == 200 and clients, client_id)
    return clients[0]


def stand_in(name):
    return next(u for u in json.loads((TEST / "import" / "test" / "TEST-EBW-realm.json").read_text(encoding="utf-8"))["users"] if u["username"] == name)


def switch_idp_client(client_id, secret=None):
    status, idp = admin("GET", "/identity-provider/instances/ebw-connector")
    must(status == 200)
    idp["config"]["clientId"] = client_id
    if secret:
        idp["config"]["clientAuthMethod"] = "private_key_jwt"
    status, _ = admin("PUT", "/identity-provider/instances/ebw-connector", idp)
    must(status == 204, f"switch: {status}")


@case(1, "Import")
def _():
    status, realm = admin("GET", "")
    must(status == 200)
    must(realm["attributes"].get("portal.config.version") == "0.2.0", realm["attributes"].get("portal.config.version"))


@case(2, "Discovery")
def _():
    status, d = http("GET", f"{IAM}/realms/{REALM}/.well-known/openid-configuration")
    must(status == 200)
    must(d["issuer"] == f"{IAM}/realms/{REALM}", d["issuer"])
    must("private_key_jwt" in d["token_endpoint_auth_methods_supported"])
    must({"authorization_code", "client_credentials"} <= set(d["grant_types_supported"]))
    _, jwks = http("GET", d["jwks_uri"])
    types = {(k["kty"], k.get("crv")) for k in jwks["keys"] if k.get("use") == "sig"}
    must(("RSA", None) in types and ("EC", "P-256") in types, types)


nina = {}


@case(3, "First login")
def _():
    browser = gt.Browser()
    tokens = gt.person_token("nina", REALM, browser)
    nina["token"] = tokens["access_token"]
    stand_in_requests = [u for _, u in browser.trail if "/realms/TEST-EBW/protocol/openid-connect/auth" in u]
    must(stand_in_requests, "no redirect to the stand-in")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(stand_in_requests[0]).query)
    must(q["scope"][0].split() == ["openid", "profile"], q["scope"])
    must(q["code_challenge_method"][0] == "S256" and "nonce" in q)
    must(not [u for s, u in browser.trail if s == 200 and f"/realms/{REALM}/" in u], "page of the Portal IAM")
    user = user_by_name(f"ebw.{stand_in('nina')['id']}")
    nina["id"] = user["id"]
    must(user["firstName"] == "Nina" and user["lastName"] == "Neu")
    must(not user.get("attributes", {}).get("company_id"))
    _, cred = admin("GET", f"/users/{user['id']}/credentials")
    must(cred == [], cred)
    _, links = admin("GET", f"/users/{user['id']}/federated-identity")
    must(links and links[0]["identityProvider"] == "ebw-connector", links)
    _, events = admin("GET", "/events?type=REGISTER&max=5")
    must(any(e.get("userId") == user["id"] for e in events), "event REGISTER")
    _, events = admin("GET", "/events?type=LOGIN&max=20")
    must(any(e.get("userId") == user["id"] for e in events), "event LOGIN")


@case(4, "Token after the first login")
def _():
    c = claims(nina["token"])
    must(c["azp"] == "portal-ui" and "portal-core" in (c["aud"] if isinstance(c["aud"], list) else [c["aud"]]), c.get("aud"))
    must(c["given_name"] == "Nina" and c["family_name"] == "Neu")
    must(roles(nina["token"]) == {"Applicant", "register_company", "accept_invitation", "view_notifications"}, roles(nina["token"]))
    for absent in ("company_id", "bpn", "birthdate", "email"):
        must(absent not in c, absent)


@case(5, "Further sign-in")
def _():
    before = admin("GET", "/users/count")[1]
    token = gt.person_token("nina", REALM)["access_token"]
    must(claims(token)["sub"] == nina["id"])
    must(admin("GET", "/users/count")[1] == before)
    uid = stand_in("nina")["id"]
    status, user = admin("GET", f"/users/{uid}", realm="TEST-EBW")
    must(status == 200)
    user["lastName"] = "Neu-Test"
    must(admin("PUT", f"/users/{uid}", user, realm="TEST-EBW")[0] == 204)
    token = gt.person_token("nina", REALM)["access_token"]
    must(claims(token)["family_name"] == "Neu-Test", claims(token)["family_name"])
    must(admin("GET", f"/users/{nina['id']}")[1]["lastName"] == "Neu-Test")


@case(6, "Client assertion")
def _():
    _, jwks = http("GET", f"{IAM}/realms/{REALM}/protocol/openid-connect/certs")
    must(any(k["kty"] == "EC" for k in jwks["keys"]))
    _, events = admin("GET", "/events?type=CODE_TO_TOKEN&client=portal-iam&max=20", realm="TEST-EBW")
    must(any(e.get("details", {}).get("client_auth_method") == "client-jwt" for e in events), "stand-in saw no client-jwt")
    switch_idp_client("portal-iam-secret")
    try:
        failed = False
        try:
            gt.person_token("rolf", REALM)
        except RuntimeError:
            failed = True
        must(failed, "a stand-in with client secret must reject the Portal IAM")
    finally:
        switch_idp_client("portal-iam")


@case(7, "No binding through claims")
def _():
    switch_idp_client("portal-iam-claims")
    try:
        # a person without an account, so that the first login runs with the further claims
        status, _ = admin("POST", "/users", {"username": "claims-test", "firstName": "Claims", "lastName": "Test", "enabled": True, "email": "claims-test@stand-in.example.org", "emailVerified": True, "credentials": [{"type": "password", "value": "claims-test", "temporary": False}]}, realm="TEST-EBW")
        must(status == 201, f"create the person: {status}")
        browser = gt.Browser()
        browser.sign_in(REALM, "portal-ui", gt.REDIRECT, "claims-test", "claims-test")
        _, people = admin("GET", "/users?firstName=Claims&lastName=Test")
        must(people, "no account")
        attrs = people[0].get("attributes", {})
        must("company_id" not in attrs and "bpn" not in attrs, attrs)
        # Keycloak takes over an email claim on the first login and has no setting against it.
        # The EBW-Connector delivers no email (C5-M1), so accounts stay without one in operation.
        print(f"NOTE  7 email on the account from the stand-in claim: {people[0].get('email')}")
    finally:
        switch_idp_client("portal-iam")


def expected_roles(role_name):
    _, composites = admin("GET", f"/clients/{client_ref('portal-core')['id']}/roles/{urllib.parse.quote(role_name)}/composites")
    return {r["name"] for r in composites}


@case(8, "Bound account")
def _():
    token = gt.person_token("erika", REALM)["access_token"]
    c = claims(token)
    must(c["company_id"] == "c0000001-0000-4000-8000-000000000001" and c["bpn"] == "BPNLIUK2REMLK3F4")
    wanted = expected_roles("Company Admin") | expected_roles("Applicant") | {"Company Admin", "Applicant"}
    must(roles(token) == wanted, roles(token) ^ wanted)


@case(9, "Staff")
def _():
    otto = gt.person_token("otto", REALM)["access_token"]
    rolf = gt.person_token("rolf", REALM)["access_token"]
    must("company_id" not in claims(otto) and "company_id" not in claims(rolf))
    must("review_application" not in roles(otto) and "review_application" in roles(rolf))
    must(roles(otto) == expected_roles("OSP Admin") | expected_roles("Applicant") | {"OSP Admin", "Applicant"})


@case(10, "Roles")
def _():
    cid = client_ref("portal-core")["id"]
    _, all_roles = admin("GET", f"/clients/{cid}/roles?briefRepresentation=false&max=500")
    portal = {"Applicant", "Member", "IT Admin", "Company Admin", "OSP Reviewer", "OSP Admin"}
    must(portal <= {r["name"] for r in all_roles})
    for r in all_roles:
        a = r.get("attributes", {})
        must(a.get("description.de") and a.get("description.en"), r["name"])
        must(r["description"] == a["description.en"][0], r["name"])
        if r["name"] not in portal:
            must(a.get("module") and a.get("scope"), r["name"])
    _, scopes = admin("GET", "/client-scopes")
    api = next(s for s in scopes if s["name"] == "portal-core-api")
    _, mapped = admin("GET", f"/client-scopes/{api['id']}/scope-mappings/clients/{cid}")
    must({r["name"] for r in mapped} == {r["name"] for r in all_roles}, "scope mapping")
    only_service = {"report_csp_notification", "report_credential_status", "submit_wallet_callback", "read_wallet_holder_access", "rotate_subscription_credentials"}
    for p in portal:
        must(not (expected_roles(p) & only_service), p)
        must(not [n for n in expected_roles(p) if n.startswith(("execute_steps_", "query_"))], p)


@case(11, "Service account with private_key_jwt")
def _():
    status, t = gt.client_token("sa-csp", REALM)
    must(status == 200, t)
    c = claims(t["access_token"])
    must(c["azp"] == "sa-csp" and "portal-core" in (c["aud"] if isinstance(c["aud"], list) else [c["aud"]]))
    must(roles(t["access_token"]) == {"report_csp_notification"}, roles(t["access_token"]))
    must("refresh_token" not in t)
    form = gt.assertion_form("sa-csp", "csp", REALM)
    must(gt.post_form(gt.token_url(REALM), form)[0] == 200)
    must(gt.post_form(gt.token_url(REALM), form)[0] in (400, 401), "the same assertion twice")
    must(gt.post_form(gt.token_url(REALM), gt.assertion_form("sa-csp", "csp-other", REALM))[0] in (400, 401), "another key")
    must(gt.post_form(gt.token_url(REALM), gt.secret_form("sa-csp", "wrong"))[0] in (400, 401), "secret")


@case(12, "Exactly one permission")
def _():
    client, user = service_user("sa-csp")
    core = client_ref("portal-core")["id"]
    _, role = admin("GET", f"/clients/{core}/roles/Company%20Admin")
    must(admin("POST", f"/users/{user['id']}/role-mappings/clients/{core}", [role])[0] == 204)
    _, t = gt.client_token("sa-csp", REALM)
    must(roles(t["access_token"]) == {"report_csp_notification"}, roles(t["access_token"]))


@case(13, "Service account with secret")
def _():
    status, t = gt.client_token("sa-div-callback", REALM)
    must(status == 200, t)
    must(roles(t["access_token"]) == {"submit_wallet_callback"}, roles(t["access_token"]))


@case(14, "Technical user")
def _():
    status, t = gt.client_token("sa-test-u1", REALM)
    must(status == 200, t)
    c = claims(t["access_token"])
    must(c["company_id"] == "c0000001-0000-4000-8000-000000000001" and c["bpn"] == "BPNLIUK2REMLK3F4", c)
    must("Dataspace Discovery" in roles(t["access_token"], "technical_roles_management"))
    must("view_discovery_endpoint" in roles(t["access_token"], "Cl21-CX-DF"))
    must("view_bpn_discovery" in roles(t["access_token"], "Cl22-CX-BPND"))
    aud = c["aud"] if isinstance(c["aud"], list) else [c["aud"]]
    must("portal-core" not in aud, aud)


@case(15, "App sign-in with session")
def _():
    browser = gt.Browser()
    gt.person_token("erika", REALM, browser)
    browser.trail.clear()
    code, verifier = browser.sign_in(REALM, "app-test", "http://localhost:8081/cb")
    must(not [u for _, u in browser.trail if "TEST-EBW" in u], "redirect to the stand-in")
    status, t = browser.exchange(REALM, "app-test", code, verifier, "http://localhost:8081/cb")
    must(status == 200, t)
    c = claims(t["access_token"])
    aud = c["aud"] if isinstance(c["aud"], list) else [c["aud"]]
    must("app-test" in aud, aud)
    must(c["company_id"] == "c0000001-0000-4000-8000-000000000001" and "bpn" in c)
    must(roles(t["access_token"], "app-test") == {"Viewer"}, roles(t["access_token"], "app-test"))
    must("given_name" not in c and "family_name" not in c)
    must(not roles(t["access_token"]), roles(t["access_token"]))


@case(16, "No password")
def _():
    for client in ("portal-ui", "app-test", "sa-csp"):
        status, t = gt.post_form(gt.token_url(REALM), {"grant_type": "password", "client_id": client, "username": "x", "password": "y"})
        must(status in (400, 401) and "access_token" not in t, client)
    for u in admin("GET", "/users?max=500")[1]:
        must(admin("GET", f"/users/{u['id']}/credentials")[1] == [], u["username"])
    _, realm = admin("GET", "")
    must(not realm.get("passwordPolicy") and realm["registrationAllowed"] is False and realm["resetPasswordAllowed"] is False)
    must(client_ref("account-console")["enabled"] is False and client_ref("account")["enabled"] is False)


@case(17, "Rights of the C2 service accounts")
def _():
    for name in ("svc-c2-m2", "svc-c2-m3"):
        status, t = gt.client_token(name, REALM)
        must(status == 200, t)
        token = t["access_token"]
        base = f"{IAM}/admin/realms/{REALM}"
        must(http("PUT", f"{base}/identity-provider/instances/ebw-connector", token, {"alias": "ebw-connector"})[0] == 403, name)
        must(http("PUT", base, token, {"displayName": "x"})[0] == 403, name)
    _, t = gt.client_token("svc-c2-m2", REALM)
    token = t["access_token"]
    base = f"{IAM}/admin/realms/{REALM}"
    uid = user_by_name(f"ebw.{stand_in('lieschen')['id']}")["id"]
    _, user = http("GET", f"{base}/users/{uid}", token)
    user.setdefault("attributes", {})["company_id"] = ["c0000002-0000-4000-8000-000000000002"]
    status, body = http("PUT", f"{base}/users/{uid}", token, user)
    must(status == 204, f"set company_id: {status} {body}")
    core = client_ref("portal-core")["id"]
    _, member = http("GET", f"{base}/clients/{core}/roles/Member", token)
    must(http("POST", f"{base}/users/{uid}/role-mappings/clients/{core}", token, [member])[0] == 204)
    must(http("POST", f"{base}/users/{uid}/logout", token)[0] == 204)
    _, t = gt.client_token("svc-c2-m3", REALM)
    token = t["access_token"]
    status, _ = http("POST", f"{base}/clients", token, {"clientId": "sa999", "enabled": True, "serviceAccountsEnabled": True, "standardFlowEnabled": False, "publicClient": False})
    must(status == 201, f"create client: {status}")
    cid = http("GET", f"{base}/clients?clientId=sa999", token)[1][0]["id"]
    status, sa_user = http("GET", f"{base}/clients/{cid}/service-account-user", token)
    must(status == 200)
    sa_user.setdefault("attributes", {}).update({"company_id": ["c0000001-0000-4000-8000-000000000001"], "bpn": ["BPNLIUK2REMLK3F4"]})
    status, body = http("PUT", f"{base}/users/{sa_user['id']}", token, sa_user)
    must(status == 204, f"attributes: {status} {body}")
    tech = http("GET", f"{base}/clients?clientId=technical_roles_management", token)[1][0]["id"]
    _, role = http("GET", f"{base}/clients/{tech}/roles/Dataspace%20Discovery", token)
    must(http("POST", f"{base}/users/{sa_user['id']}/role-mappings/clients/{tech}", token, [role])[0] == 204)


@case(18, "User profile")
def _():
    uid = user_by_name(f"ebw.{stand_in('hans')['id']}")["id"]
    _, user = admin("GET", f"/users/{uid}")
    bad = {**user, "attributes": {**user.get("attributes", {}), "company_id": ["abc"]}}
    must(admin("PUT", f"/users/{uid}", bad)[0] == 400, "company_id=abc")
    extra = {**user, "attributes": {**user.get("attributes", {}), "birthdate": ["2000-01-01"], "organisation": ["x"]}}
    admin("PUT", f"/users/{uid}", extra)
    attrs = admin("GET", f"/users/{uid}")[1].get("attributes", {})
    must("birthdate" not in attrs and "organisation" not in attrs, attrs)


@case(20, "Operation without test objects")
def _():
    text = (TEST.parent / "realm-osp.json").read_text()
    realm = json.loads(text)
    must(not [c for c in realm["clients"] if c["clientId"].endswith("-test")])
    must("TEST-EBW" not in text and "Erika" not in text and "ebw." not in text.replace("ebw.${CLAIM.sub}", ""))
    for c in realm["clients"]:
        if "secret" in c:
            must(c["secret"].startswith("${"), c["clientId"])


@case(21, "Foreign issuer")
def _():
    # The library is not part of this module; here only the difference of the issuers is checked.
    status, t = gt.client_token("sa-csp", REALM)
    must(status == 200)
    iss = claims(t["access_token"])["iss"]
    must(iss == f"{IAM}/realms/{REALM}" and iss != f"{IAM}/realms/TEST-EBW", iss)


failed = [r for r in results if not r[2]]
print(f"\n{len(results) - len(failed)} of {len(results)} cases passed; not automated: 19 (seeding job), 22 (real wallet)")
sys.exit(1 if failed else 0)
