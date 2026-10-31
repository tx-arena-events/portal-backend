# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Tokens from the Keycloak test installation in `iam/` (C2-M5 14.4).

A test person signs in through the browser flow, with the form of the stand-in `TEST-EBW` for the
EBW-Connector. A service account uses a secret or a client assertion. Used by `demo.py`,
`iam/test/get-token.py` and the checks in `iam/test/`.
"""
from __future__ import annotations

import base64
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

TEST = Path(__file__).resolve().parents[1] / "iam" / "test"
DEFAULT_IAM_URL = "http://localhost:8080/auth"
REDIRECT = "http://localhost/callback"
KEY_CLIENTS = {"sa-csp": "csp", "sa-csp-issuer": "csp", "sa-csp-wallet": "csp", "sa-credential-issuer": "credential-issuer"}
SECRET_CLIENTS = {"sa-ih-callback": "SA_IH_CALLBACK_SECRET", "sa-div-callback": "SA_DIV_CALLBACK_SECRET",
                  "sa-test-u1": "SA_TEST_U1_SECRET"}


def iam_url() -> str:
    return (os.environ.get("IAM_URL") or DEFAULT_IAM_URL).rstrip("/")


def env_test() -> dict[str, str]:
    values = {}
    for line in (TEST / ".env.test").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def token_url(realm: str, base: str | None = None) -> str:
    return f"{(base or iam_url())}/realms/{realm}/protocol/openid-connect/token"


def post_form(url: str, form: dict) -> tuple[int, dict]:
    request = urllib.request.Request(url, urllib.parse.urlencode(form).encode(), method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def client_assertion(client_id: str, key_name: str, realm: str) -> str:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils

    key = serialization.load_pem_private_key((TEST / "keys" / f"{key_name}.TEST-ONLY.pem").read_bytes(), None)
    now = int(time.time())
    header = {"alg": "ES256", "typ": "JWT", "kid": f"{key_name}-test-1"}
    claims = {"iss": client_id, "sub": client_id, "aud": f"{iam_url()}/realms/{realm}", "jti": secrets.token_hex(16), "iat": now, "exp": now + 60}
    signing_input = f"{b64url(json.dumps(header).encode())}.{b64url(json.dumps(claims).encode())}".encode()
    r, s = utils.decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    return f"{signing_input.decode()}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def assertion_form(client_id: str, key_name: str, realm: str) -> dict:
    return {"grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": client_assertion(client_id, key_name, realm)}


def secret_form(client_id: str, secret: str) -> dict:
    return {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Browser:
    """A minimal browser for the sign-in: follows redirects, keeps cookies, fills the form of the stand-in."""

    def __init__(self, base: str | None = None) -> None:
        self.base = (base or iam_url()).rstrip("/")
        # Keycloak 26 sets its cookies as Secure; browsers accept that on http://localhost, so this one does too.
        policy = http.cookiejar.DefaultCookiePolicy(secure_protocols=("https", "wss", "http"))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar(policy)), _NoRedirect)
        self.trail: list[tuple[int, str]] = []

    def _request(self, url: str, data: dict | None = None):
        request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None)
        try:
            with self.opener.open(request) as response:
                return response.status, response.headers, response.read().decode()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read().decode(errors="replace")

    def sign_in(self, realm: str, client_id: str, redirect: str, person: str | None = None, password: str | None = None) -> tuple[str, str]:
        """Returns (code, verifier). Fills the form of the stand-in if it appears."""
        verifier = b64url(secrets.token_bytes(32))
        query = urllib.parse.urlencode({
            "client_id": client_id, "response_type": "code", "scope": "openid", "redirect_uri": redirect,
            "state": secrets.token_hex(8), "nonce": secrets.token_hex(8), "code_challenge_method": "S256",
            "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest())})
        url, data = f"{self.base}/realms/{realm}/protocol/openid-connect/auth?{query}", None
        for _ in range(15):
            status, headers, body = self._request(url, data)
            self.trail.append((status, url))
            data = None
            if status in (301, 302, 303):
                url = urllib.parse.urljoin(url, headers["Location"])
                if url.startswith(redirect):
                    params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
                    if "code" in params:
                        return params["code"][0], verifier
                    raise RuntimeError(f"no code: {params}")
                continue
            form = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', body)
            if status == 200 and form and person:
                url, data = unescape(form.group(1)), {"username": person, "password": password, "credentialId": ""}
                continue
            raise RuntimeError(f"sign-in stopped with HTTP {status} at {url}: {re.sub(r'<[^>]+>', ' ', body)[:300]}")
        raise RuntimeError("too many redirects")

    def exchange(self, realm: str, client_id: str, code: str, verifier: str, redirect: str) -> tuple[int, dict]:
        return post_form(token_url(realm, self.base), {"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                                       "redirect_uri": redirect, "code_verifier": verifier})


def person_token(person: str, realm: str, browser: Browser | None = None, client_id: str = "portal-ui",
                 redirect: str = REDIRECT, base: str | None = None) -> dict:
    """Tokens of a test person, as after a sign-in. `person` is the name at the stand-in, for example `erika`."""
    browser = browser or Browser(base)
    code, verifier = browser.sign_in(realm, client_id, redirect, person, env_test()["TEST_EBW_PASSWORD"])
    status, tokens = browser.exchange(realm, client_id, code, verifier, redirect)
    if status != 200:
        raise RuntimeError(f"token request failed: {status} {tokens}")
    return tokens


def client_token(name: str, realm: str) -> tuple[int, dict]:
    if name in KEY_CLIENTS:
        return post_form(token_url(realm), assertion_form(name, KEY_CLIENTS[name], realm))
    env = env_test()
    secret = env["SERVICE_CLIENT_SECRET"] if name.startswith("svc-") else env[SECRET_CLIENTS[name]]
    return post_form(token_url(realm), secret_form(name, secret))
