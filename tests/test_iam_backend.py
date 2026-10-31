# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Choice of the Portal IAM for the stand-ins, and the realm files of the Keycloak test installation in `iam/`."""
import json
from pathlib import Path

import pytest

from conftest import ROOT
from portal_common import settings
from scaffold import app as scaffold_app
from tools import iam_backend, keycloak_admin, test_iam

INSTALLATIONS = ["osp-a", "osp-b"]


def _realm(osp: str) -> dict:
    data = json.loads((ROOT / "config" / osp / "installation.json").read_text(encoding="utf-8"))
    return json.loads((ROOT / "config" / osp / data["iamRealmFile"]).resolve().read_text(encoding="utf-8"))


def _persons(osp: str) -> list[dict]:
    return json.loads((ROOT / "testdata" / f"persons-{osp}.json").read_text(encoding="utf-8"))["persons"]


def test_without_iam_url_the_stand_ins_use_the_in_memory_iam(monkeypatch):
    monkeypatch.delenv("IAM_URL", raising=False)
    assert iam_backend.backend("c2-m2") is test_iam.iam()


def test_with_iam_url_the_stand_ins_use_keycloak(monkeypatch):
    monkeypatch.setenv("IAM_URL", "http://iam.test/auth")
    assert isinstance(iam_backend.backend("c2-m2"), keycloak_admin.KeycloakAdmin)


def test_users_with_portal_role():
    otto = test_iam.iam().find("otto")
    assert otto in test_iam.iam().users_with_portal_role("OSP Admin")
    assert test_iam.iam().find("erika") not in test_iam.iam().users_with_portal_role("OSP Admin")


def test_users_of_company():
    iam, company = test_iam.iam(), "c0000000-0000-4000-8000-000000000001"
    otto = iam.find("otto")
    iam.bind(otto, company, None)
    assert iam.users_of_company(company) == [otto]
    assert iam.users_of_company(company, "OSP Admin") == [otto]
    assert iam.users_of_company(company, "CX Admin") == []


def _paths(app) -> set[str]:
    return set(app.openapi()["paths"])


def test_without_iam_url_the_app_serves_the_in_memory_iam(monkeypatch):
    monkeypatch.delenv("IAM_URL", raising=False)
    monkeypatch.setattr(keycloak_admin, "reset_realm", lambda: pytest.fail("no Keycloak here"))
    assert any(p.startswith("/auth/") for p in _paths(scaffold_app.create_app()))


def test_keycloak_replaces_the_in_memory_iam_and_resets_the_realm(monkeypatch):
    monkeypatch.setenv("IAM_URL", "http://iam.test/auth")
    calls = []
    monkeypatch.setattr(keycloak_admin, "reset_realm", lambda: calls.append(1))
    paths = _paths(scaffold_app.create_app())
    assert calls == [1]
    assert not any(p.startswith("/auth/") for p in paths)
    assert any(p.startswith("/ebw/") for p in paths)


def test_a_real_iam_is_never_reset(monkeypatch):
    monkeypatch.setenv("IAM_URL", "http://iam.test/auth")
    monkeypatch.setenv("TEST_TOOLS", "false")
    monkeypatch.setattr(keycloak_admin, "reset_realm", lambda: pytest.fail("the realm must stay"))
    assert not any(p.startswith("/ebw/") for p in _paths(scaffold_app.create_app()))


def test_realm_file_of_the_settings(monkeypatch):
    monkeypatch.setenv("IAM_REALM_DIR", "/elsewhere")
    assert keycloak_admin.realm_file() == Path("/elsewhere") / Path(settings.get("iamRealmFile")).name


@pytest.mark.parametrize("osp", INSTALLATIONS)
def test_realm_has_the_persons_of_the_test_data(osp):
    users = {u["id"]: u for u in _realm(osp)["users"] if u.get("federatedIdentities")}
    for person in _persons(osp):
        user = users[person["id"]]
        assert (user["firstName"], user["lastName"]) == (person["givenName"], person["familyName"])
        assert set(user["clientRoles"]["portal-core"]) == {"Applicant", *person.get("portalRoles", [])}
        assert user.get("attributes", {}).get("company_id", [None])[0] == person.get("companyId")


@pytest.mark.parametrize("osp", INSTALLATIONS)
def test_realm_has_a_service_account_per_module(osp):
    clients = {c["clientId"] for c in _realm(osp)["clients"]}
    modules = [p.name for p in (ROOT / "modules").iterdir() if (p / "__init__.py").exists()]
    assert modules and all(f"svc-{m.replace('_', '-')}" in clients for m in modules)
