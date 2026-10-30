# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Public interface of the C4-M1 stand-in: `issue-credentials` and the credential query.

Route as in C4-M1 6.3, from `ISSUER_MODE` (`module-config.json` of the installation):
- `STUB` (11.6), credentials are issued at once: `OWN` with empty `ISSUER_ROUTES`, `CSP` with empty
  `CSP_ISSUER_URL`, or a wallet of provider `STUB`. At the hackathon OSP A and OSP B both run on
  this route (OP-53).
- `CSP_ISSUER` (11.5), `CSP` with `CSP_ISSUER_URL` and a wallet with `HOLDER_REQUESTS`: register
  the holder, set the grants with `callbackUrl` = `{BASE_URL}/api/c4-m1/issuer-callbacks/{kind}`,
  request the credentials in the holder's wallet (holder access from C4-M2); the CSP reports to
  `/issuer-callbacks/{kind}`. Target for the Community Days, not used at the hackathon (OP-53).
- `OWN` with `ISSUER_ROUTES`: the routes of an own issuer are not in the stand-in (409 `no-issuer-route`).
"""
from __future__ import annotations

import os

from portal_common import csp, events, http, neighbours, settings, standin
from portal_common.problems import Problem

MODULE = "c4-m1"
TYPES = ("BPN", "MEMBERSHIP")
credentials: dict[str, dict[str, dict]] = {}   # companyId -> type -> credential


def reset() -> None:
    credentials.clear()


def _route(cid: str) -> tuple[str, dict | None]:
    """C4-M1 6.3; returns the route and, for `CSP_ISSUER`, the wallet."""
    if os.environ.get("ISSUER_MODE", "OWN") != "CSP":
        if os.environ.get("ISSUER_ROUTES"):
            raise Problem(MODULE, 409, "no-issuer-route", detail="stand-in: only the routes STUB and CSP_ISSUER",
                          retryable=False)
        return "STUB", None
    if not os.environ.get("CSP_ISSUER_URL"):
        return "STUB", None
    wallet = neighbours.query(MODULE, "c4-m2", "get_wallet_info", companyId=cid)
    if not wallet:
        raise Problem(MODULE, 409, "invalid-state", detail="no dataspace wallet", retryable=False)
    if wallet.get("provider") == "STUB":
        return "STUB", None
    if wallet.get("issuanceMode") != "HOLDER_REQUESTS":
        raise Problem(MODULE, 409, "no-issuer-route", retryable=False)
    return "CSP_ISSUER", wallet


def all_issued(cid: str) -> None:
    creds = credentials.get(cid, {})
    if creds and all(c["status"] == "ISSUED" for c in creds.values()):
        events.publish({"eventType": "CredentialsIssued", "companyId": cid, "credentialTypes": sorted(creds)})


def _csp(method: str, path: str, body: dict) -> None:
    try:
        status, answer = csp.request(os.environ["CSP_ISSUER_URL"], method, path, body)
    except csp.CspUnavailable as e:
        raise Problem(MODULE, 502, "upstream-error", detail=str(e), retryable=True) from e
    if status >= 500 or status == 401:
        raise Problem(MODULE, 502, "upstream-error", detail=f"CSP issuer answered {status}", retryable=True)
    if status >= 400:
        raise Problem(MODULE, 409, "issuer-rejected", detail=f"CSP issuer answered {status}",
                      issuerType=(answer or {}).get("type"), retryable=False)


def issue_credentials(input: dict) -> dict:
    cid = input.get("companyId")
    company = neighbours.query(MODULE, "c3-m1", "query_company", companyId=cid)
    if not company or not company.get("bpn"):
        raise Problem(MODULE, 409, "invalid-state", detail="company without BPN", retryable=False)
    bpn = company["bpn"]
    creds = credentials.setdefault(cid, {})
    if creds and all(c["status"] == "ISSUED" for c in creds.values()):
        return standin.step_reply(input, {"credentialTypes": sorted(creds)})
    route, wallet = _route(cid)
    if route == "STUB":
        for t in TYPES:
            creds[t] = {"credentialType": t, "status": "ISSUED", "bpn": bpn, "route": "STUB"}
        all_issued(cid)
        return standin.step_reply(input, state="RUNNING")
    access = neighbours.query(MODULE, "c4-m2", "get_holder_access", companyId=cid)
    if not access:
        raise Problem(MODULE, 409, "invalid-state", detail="no holder access", retryable=False)
    for t in TYPES:
        creds[t] = {"credentialType": t, "status": "PENDING", "bpn": bpn, "route": "CSP_ISSUER"}
    _csp("PUT", f"/holders/{bpn}", {"did": wallet["did"]})
    base = settings.base_url()
    for t in TYPES:
        kind = t.lower()
        grant = {"callbackUrl": f"{base}/api/c4-m1/issuer-callbacks/{kind}"}
        if kind == "membership":
            grant["memberOf"] = settings.get("dataspace", {}).get("memberOf")
        _csp("PUT", f"/holders/{bpn}/grants/{kind}", grant)
    r = http.request("POST", f"{access['identityApiUrl']}/participants/{access['participantContextId']}/credentials/request",
                     json={"issuerDid": os.environ.get("CSP_ISSUER_DID", "did:web:issuer.csp.example.org"),
                           "holderPid": f"{cid}:onboarding:{input.get('stepRunId')}",
                           "credentials": [{"type": t.lower()} for t in TYPES]},
                     headers={"x-api-key": access["apiKey"]})
    if not r.is_success:
        raise Problem(MODULE, 502, "upstream-error", detail=f"wallet identity API answered {r.status_code}",
                      retryable=r.status_code >= 500)
    return standin.step_reply(input, state="RUNNING")   # completed by CredentialsIssued


def query_company_credentials(*, companyId: str) -> dict:
    return {"companyId": companyId, "credentials": list(credentials.get(companyId, {}).values())}


__getattr__ = standin.missing_interface(MODULE)   # steps and queries not implemented yet: stand-in
