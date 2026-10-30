# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Stand-in of C4-M1 Credentials: `issue-credentials`, status reports of issuers, credential queries.

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

from fastapi import Body

from portal_common import events, standin
from portal_common.problems import Problem

from . import interface
from .interface import MODULE, all_issued, credentials

STANDIN = True
router = standin.router(MODULE)


def reset() -> None:
    interface.reset()


@router.post("/steps/issue-credentials")
def issue(body: dict = Body(...), _: dict = standin.perm(MODULE, "POST", "/steps/issue-credentials")):
    return interface.issue_credentials(body)


@router.post("/issuer-callbacks/{credentialKind}")
def issuer_callback(credentialKind: str, body: dict = Body(...),
                    _: dict = standin.perm(MODULE, "POST", "/issuer-callbacks/{credentialKind}")):
    t = credentialKind.upper()
    cid = next((c for c, creds in credentials.items() if (creds.get(t) or {}).get("bpn") == body.get("bpn")), None)
    if cid is None:
        raise Problem(MODULE, 404, "not-found")
    cred = credentials[cid][t]
    if cred["status"] == "PENDING":
        if body.get("status") == "SUCCESSFUL":
            cred["status"] = "ISSUED"
            all_issued(cid)
        else:
            cred["status"] = "FAILED"
            events.publish({"eventType": "CredentialIssuanceFailed", "companyId": cid, "credentialType": t})
    return {"credentialType": t, "status": cred["status"]}


@router.get("/internal/companies/{companyId}/credentials")
def query_credentials(companyId: str,
                      _: dict = standin.perm(MODULE, "GET", "/internal/companies/{companyId}/credentials")):
    return interface.query_company_credentials(companyId=companyId)


@router.get("/companies/{companyId}/credentials")
def own_credentials(companyId: str, claims: dict = standin.perm(MODULE, "GET", "/companies/{companyId}/credentials")):
    standin.company_scope(MODULE, claims, companyId)
    return interface.query_company_credentials(companyId=companyId)["credentials"]   # 7.1: list
