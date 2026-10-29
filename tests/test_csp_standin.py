# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Shared CSP stand-in: trust list with OSP A and OSP B, private_key_jwt, core services API,
discovery, issuer with callback through the Portal IAM of the OSP, wallet and identity API."""
import json
import time

import jwt
import pytest
from fastapi import FastAPI, Header
from fastapi.testclient import TestClient

from conftest import BASE
from portal_common import csp as csp_client
from portal_common import http as phttp
from portal_common import tokens
from tools import test_iam
from tools.csp_standin.app import create_app
from tools.keys import ec_jwk

CSP = "http://csp.test"
OSP_A, OSP_B = "22222222-2222-4222-8222-222222222222", "99a5685c-7397-402c-88de-b1b3a939ecc9"


@pytest.fixture
def world(tmp_path, monkeypatch):
    keys = {OSP_A: ec_jwk("osp-a"), OSP_B: ec_jwk("osp-b")}
    trust = {OSP_A: {"name": "OSP A", "installationUrl": BASE, "jwk": keys[OSP_A][1],
                     "iamTokenUrl": f"{BASE}/auth/realms/CX-Central/protocol/openid-connect/token"},
             OSP_B: {"name": "OSP B", "installationUrl": "http://osp-b.test", "jwk": keys[OSP_B][1],
                     "iamTokenUrl": "http://osp-b.test/token"}}
    (tmp_path / "trust.json").write_text(json.dumps(trust))
    (tmp_path / "a.json").write_text(json.dumps(keys[OSP_A][0]))
    monkeypatch.setenv("CSP_TRUST_FILE", str(tmp_path / "trust.json"))
    monkeypatch.setenv("CSP_PUBLIC_URL", CSP)
    monkeypatch.setenv("CSP_CALLBACK_DELAY_SECONDS", "0")
    monkeypatch.setenv("CSP_TOKEN_URL", f"{CSP}/oauth/token")
    monkeypatch.setenv("OSP_SIGNING_KEY_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CSP_JWKS_URL", f"{CSP}/.well-known/jwks.json")
    csp_client.reset()
    app = create_app()
    phttp.register_local(CSP, app)
    return {"app": app, "keys": keys, "client": TestClient(app, base_url=CSP)}


def token_for(world, osp):
    priv = world["keys"][osp][0]
    a = jwt.encode({"iss": osp, "sub": osp, "aud": f"{CSP}/oauth/token", "iat": int(time.time()),
                    "exp": int(time.time()) + 60}, jwt.PyJWK(priv).key, algorithm="ES256", headers={"kid": priv["kid"]})
    r = world["client"].post("/oauth/token", data={"grant_type": "client_credentials", "client_assertion": a,
                                                   "client_assertion_type": csp_client.ASSERTION_TYPE})
    return r


def h(world, osp):
    return {"Authorization": "Bearer " + token_for(world, osp).json()["access_token"]}


def test_private_key_jwt_per_osp_and_foreign_key_rejected(world):
    assert token_for(world, OSP_A).status_code == 200
    world["keys"][OSP_A] = ec_jwk("other")
    assert token_for(world, OSP_A).status_code == 401
    assert world["client"].get("/v1/companies", params={"ebwoid": "x"}).status_code == 401


def test_one_data_set_for_both_osps(world):
    c = world["client"]
    body = {"legalName": "Beispiel Maschinenbau GmbH", "country": "DE", "ebwoid": "EBWOID-DE-0000000001",
            "address": {"streetAddress": "Musterstraße 1", "postalCode": "70569", "locality": "Stuttgart", "country": "DE"}}
    r = c.post("/v1/companies", json=body, headers=h(world, OSP_A))
    assert r.status_code == 201 and r.json()["bpn"] == "BPNLIUK2REMLK3F4"
    r = c.post("/v1/companies", json=body, headers=h(world, OSP_B))
    assert r.status_code == 409 and r.json()["homeOspId"] == OSP_A
    found = c.get("/v1/companies", params={"ebwoid": "EBWOID-DE-0000000001"}, headers=h(world, OSP_B)).json()
    assert found[0]["homeOspName"] == "OSP A"
    assert c.put("/v1/companies/BPNLIUK2REMLK3F4/did", json={"did": "did:web:x"}, headers=h(world, OSP_B)).status_code == 403


def test_discovery_lists_connectors_of_members_only(world):
    c = world["client"]
    a = h(world, OSP_A)
    c.put("/v1/companies/BPNL00000000OSPA/connectors/k1", json={"endpointUrl": "https://edc.example/dsp"}, headers=a)
    assert c.get("/v1/discovery/BPNL00000000OSPA", headers=a).json()["connectors"][0]["connectorId"] == "k1"
    c.put("/v1/companies/BPNL00000000OSPA/membership", json={"member": False}, headers=a)
    assert c.get("/v1/discovery/BPNL00000000OSPA", headers=a).json()["connectors"] == []


def test_issuer_calls_back_to_c4_m1_with_a_token_of_the_osp_iam(world):
    received = {}
    osp = FastAPI()
    osp.include_router(test_iam.router)

    @osp.post("/api/c4-m1/issuer-callbacks/membership")
    def callback(body: dict, authorization: str = Header("")):
        received["claims"] = tokens.verify(authorization.removeprefix("Bearer "))
        received["body"] = body
        return {}

    phttp.register_local(BASE, osp)
    c, a = world["client"], h(world, OSP_A)
    w = c.post("/wallet/v1/wallets", json={"bpn": "BPNL00000000OSPA"}, headers=a)
    assert w.status_code == 201
    w = w.json()
    c.put("/v1/companies/BPNL00000000OSPA/did", json={"did": w["did"]}, headers=a)
    assert c.put("/issuer/v1/holders/BPNL00000000OSPA", json={"did": w["did"]}, headers=a).status_code in (200, 201)
    r = c.put("/issuer/v1/holders/BPNL00000000OSPA/grants/membership",
              json={"callbackUrl": f"{BASE}/api/c4-m1/issuer-callbacks/membership", "memberOf": "Catena-X"}, headers=a)
    assert r.status_code == 204
    r = c.post(f"/identity/v1/participants/{w['participantContextId']}/credentials/request",
               json={"holderPid": "p1", "credentials": [{"type": "membership"}]}, headers={"x-api-key": w["apiKey"]})
    assert r.status_code == 202
    for _ in range(50):
        if received:
            break
        time.sleep(0.05)
    assert received["body"] == {"bpn": "BPNL00000000OSPA", "status": "SUCCESSFUL",
                                "message": "membership credential stored in the wallet"}
    assert received["claims"]["azp"] == "sa-csp-issuer"
    assert received["claims"]["resource_access"]["portal-core"]["roles"] == ["report_credential_status"]


def test_callback_only_under_the_installation_address(world):
    a = h(world, OSP_A)
    c = world["client"]
    c.put("/issuer/v1/holders/BPNL00000000OSPA", json={"did": "did:web:wallet.example.org:BPNL00000000OSPA"}, headers=a)
    world["app"].state.csp.callback(OSP_A, "sa-csp-issuer", "http://evil.test/x", {"bpn": "x"})
    assert world["app"].state.csp.callbacks[-1]["status"].startswith("refused")
