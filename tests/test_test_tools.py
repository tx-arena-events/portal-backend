# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Test IAM (C2-M5 stand-in), EBW-Connector signer (C5-M1), provider double (C10-M1 11.2), test data."""
import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

from conftest import BASE
from portal_common import http as phttp
from portal_common import tokens
from tools import ebw_signer, provider_double, test_iam, testdata

REALM = "/auth/realms/CX-Central"


def _client() -> TestClient:
    app = FastAPI()
    for r in (test_iam.router, ebw_signer.router, provider_double.router):
        app.include_router(r)
    phttp.register_local(BASE, app)
    return TestClient(app, base_url=BASE)


def test_portal_roles_follow_c2_m5_6_3():
    roles = test_iam.spec_portal_roles()
    assert roles["Member"] <= roles["IT Admin"] <= roles["Company Admin"]
    assert "review_application" in roles["OSP Reviewer"] and "review_application" not in roles["OSP Admin"]
    assert "prove_wallet_control" not in roles["Applicant"]   # "later" in 6.3
    assert "Dataspace Discovery" in test_iam.spec_technical_roles()


def test_token_per_test_person_over_http():
    c = _client()
    r = c.get(f"{REALM}/test/tokens/otto")
    claims = tokens.verify(r.json()["access_token"])
    assert claims["sub"] == "5a1c0012-0000-4000-8000-000000000012" and "view_processes" in tokens.permissions(claims)
    assert c.get(f"{REALM}/test/tokens/5a1c0012-0000-4000-8000-000000000012").status_code == 200
    assert c.get(f"{REALM}/test/tokens/nobody").status_code == 404
    assert c.get(f"{REALM}/.well-known/openid-configuration").json()["issuer"] == tokens.issuer()


def test_token_refresh_after_binding():
    iam = test_iam.iam()
    uid = iam.find("erika")
    _client()
    assert "company_id" not in tokens.verify(iam.person_token(uid))
    iam.bind(uid, "c1", "BPNL1")
    iam.add_portal_roles(uid, ["Company Admin"])
    c = tokens.verify(iam.person_token(uid))
    assert c["company_id"] == "c1" and c["bpn"] == "BPNL1" and "manage_company_users" in tokens.permissions(c)


def test_client_credentials_with_secret():
    c = _client()
    ok = c.post(f"{REALM}/protocol/openid-connect/token", data={"grant_type": "client_credentials",
                                                                 "client_id": "svc-c3-m1",
                                                                 "client_secret": tokens.DEFAULT_SERVICE_SECRET})
    assert ok.status_code == 200
    bad = c.post(f"{REALM}/protocol/openid-connect/token", data={"grant_type": "client_credentials",
                                                                  "client_id": "svc-c3-m1", "client_secret": "x"})
    assert bad.status_code == 401


def test_attestation_verifies_against_the_jwks_endpoint():
    c = _client()
    att = c.post("/ebw/test/attestations", json={"company": "werkzeugbau"}).json()["attestation"]
    key = jwt.PyJWK(c.get("/ebw/jwks").json()["keys"][0]).key
    claims = jwt.decode(att, key, algorithms=["ES256"], audience="portal-registration")
    assert claims["company"]["ebwoid"] == "EBWOID-DE-0000000003" and claims["person"]["given_name"] == "Erika"
    assert jwt.get_unverified_header(att)["typ"] == "ebw-attestation+jwt"


def test_provider_double_needs_its_token():
    c = _client()
    assert c.post("/test-tools/provider/setup", json={}).status_code == 401
    t = c.post("/test-tools/provider/token", data={"grant_type": "client_credentials", "client_id": provider_double.CLIENT_ID,
                                                   "client_secret": provider_double.CLIENT_SECRET}).json()["access_token"]
    r = c.post("/test-tools/provider/callback", json={"clientId": "a", "technicalUser": {"clientSecret": "s"}},
               headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    assert c.get("/test-tools/provider/calls").json()[0]["body"]["technicalUser"]["clientSecret"] == "***"


def test_test_data_package_is_consistent():
    keys = [(a["agreementKey"], a["version"]) for a in testdata.agreements()]
    ids = [a["agreementId"] for a in testdata.agreements()]
    assert len(set(keys)) == len(keys) and len(set(ids)) == len(ids)
    osp = testdata.osp_company()
    assert osp["companyId"] == "11111111-1111-4111-8111-111111111111" and osp["bpn"] == "BPNL00000000OSPA"
    assert {"ONBOARDING_SERVICE_PROVIDER", "ACTIVE_PARTICIPANT", "APP_PROVIDER", "SERVICE_PROVIDER"} <= set(osp["companyRoles"])
    iam = test_iam.iam()
    assert iam.find("rolf") == "bbede0f3-fbc4-433b-b7ca-1982e8724912"   # C2-M5 14.2
    assert iam.person(iam.find("erika"))["companyId"] is None           # one state: applicant
