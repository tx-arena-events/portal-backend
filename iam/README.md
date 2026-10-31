<!--
Copyright (c) 2026 Contributors to the Eclipse Foundation
SPDX-License-Identifier: Apache-2.0
-->

# Test Portal IAM (C2-M5)

C2-M5 ready to use for the hackathon: Keycloak with the realm of the spec, the test persons of `testdata/` and a stand-in for the EBW-Connector, so that teams can work without wallets. Test values only, not for operation. Keys, secrets and passwords are public on purpose.

```text
realm-osp.json                 realm file for operation, the only source
test/
  build-test-realm.py          builds test/import/ from realm-osp.json and the test objects
  test-objects.hackathon.json  variant hackathon: OSP A and OSP B, persons from testdata/
  test-objects.json            variant test: for the tests of C2-M5, Erika and Max are bound
  import/                      generated realm files, one folder per variant
  keys/, jwks/, .env.test      test keys and secrets
  get-token.py                 gets a token
  check-hackathon.py           start state of the variant hackathon
  check-acceptance.py          acceptance cases of C2-M5 section 13 (variant test)
```

## Start

From the root of the repository:

```bash
docker compose up -d
```

Keycloak answers after about a minute at `http://localhost:8080/auth`. `demo.py` sets `IAM_URL` and `IAM_REALM` for each installation, and stops with a hint if Keycloak does not answer yet. To run an installation yourself, set them as in the table.

| | OSP A | OSP B |
|---|---|---|
| `IAM_URL` | `http://localhost:8080/auth` | `http://localhost:8080/auth` |
| `IAM_REALM` | `CX-Central` | `CX-Central-B` |
| Issuer | `…/realms/CX-Central` | `…/realms/CX-Central-B` |
| UI | `http://localhost:3000` | `http://localhost:3001` |
| Client at the stand-in | `portal-iam` | `portal-iam-b` |

One Keycloak with two realms keeps it light enough for a laptop. Tokens of the two OSPs differ in `iss`, so an OSP refuses the tokens of the other one. An account belongs to one realm, so its ID differs per OSP.

For a clean Keycloak, remove the container with `docker compose down`. The variant for the tests of C2-M5:

```bash
IAM_VARIANT=test docker compose up -d
```

## Test persons

From `testdata/persons-osp-a.json` and `testdata/persons-osp-b.json`; `sub` in the token is the `id` there. Every account also has the portal role `Applicant`. At the stand-in `TEST-EBW` the name is the `key`, the password is in `test/.env.test`. Nina (`nina`) has no account yet: her first sign-in creates it.

## Tokens

```bash
uv run python iam/test/get-token.py erika
```

```bash
uv run python iam/test/get-token.py gerd --realm CX-Central-B
```

```bash
uv run python iam/test/get-token.py svc-c3-m1
```

A token of a test person comes from the real browser flow: Portal IAM, redirect to the stand-in, form, back. After a binding, the next sign-in returns the new roles.

## Service accounts

- `svc-<module>` for every folder in `modules/`, with every step and query permission and the secret `SERVICE_CLIENT_SECRET` (`tokens.service_token`).
- `svc-c2-m2` and `svc-c2-m3` also have their rights on the Admin API (C2-M5 5.9), so teams of C2-M2 and C2-M3 build against the real interface.
- `sa-csp`, `sa-csp-issuer`, `sa-csp-wallet` sign in with `private_key_jwt` against the JWKS of the CSP stand-in. Keycloak in the container reaches the stand-in on the host at `host.docker.internal:8100`.

## Start state

Every start of an installation with `IAM_URL` restores the start state of its realm from `iamRealmFile` of its settings, like every other module in memory: `/scaffold/reset` does the same. Accounts get their attributes and portal roles from the file, further accounts and clients are deleted. The realm and its keys stay. With `TEST_TOOLS=false` the realm is never touched. The admin account of the test installation is in `test/.env.test`; `KEYCLOAK_ADMIN_USER` and `KEYCLOAK_ADMIN_PASSWORD` replace it.

## Checks

On a fresh start of the variant hackathon:

```bash
uv run python iam/test/check-hackathon.py
```

On a fresh start of the variant test:

```bash
uv run python iam/test/check-acceptance.py
```

Not automated: case 19 needs the seeding job of the portal releases, case 22 a real wallet.

## Changing the realm

Change `realm-osp.json`, run `iam/test/build-test-realm.py`, and start with a clean Keycloak. Values per OSP are placeholders `${NAME}` as in C2-M5 section 4. The service accounts of `CALLBACK_CLIENTS` have one placeholder each: `CALLBACK_JWKS_URL_SA_CREDENTIAL_ISSUER`, `CALLBACK_CLIENT_SECRET_SA_IH_CALLBACK`, `CALLBACK_CLIENT_SECRET_SA_DIV_CALLBACK`.

## Where this differs from C2-M5 version 0.2

To be taken over by the spec:

1. **5.3:** RSA key with 4096 bit. Keycloak 25 does not accept 3072.
2. **5.9:** `svc-c2-m2` and `svc-c2-m3` have their rights on `realm-management` also in their own scope mapping. With `fullScopeAllowed: false` they are otherwise missing from the token.
3. **13 case 7:** Keycloak takes over an `email` claim on the first login, and no setting prevents it. `check-acceptance.py` checks `company_id` and `bpn` and prints a note on the email. The EBW-Connector delivers no email, so accounts stay without one in operation.
4. **14:** realm files named `<realm>-realm.json`, the variant hackathon with OSP B, `svc-<module>`, persons from `testdata/`.
