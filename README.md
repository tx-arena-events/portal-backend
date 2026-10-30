<!--
Copyright (c) 2026 Contributors to the Eclipse Foundation
SPDX-License-Identifier: Apache-2.0
-->

# Hackathon scaffold for the portal backend (OSP 2.0)

A running installation of the portal backend as a modular monolith (Python 3.12, FastAPI), built
from the module specs in [portal-backend-spec](https://github.com/tx-arena-events/portal-backend-spec).
Every module of the spec is already mounted, as a stand-in. A team replaces one folder under
`modules/` with its implementation; the rest of the installation keeps working, and `demo.py`
shows at once whether the flows still run end to end.

## Quickstart (about 10 minutes)

Prerequisites: [uv](https://docs.astral.sh/uv/) and git.

```bash
git clone https://github.com/tx-arena-events/portal-backend-spec.git spec   # read-only, not committed
uv sync                       # Python 3.12 and the dependencies (behind a TLS proxy: UV_NATIVE_TLS=1 uv sync)
uv run pytest -q              # all tests, about 5 seconds
uv run python demo.py         # starts the CSP stand-in, OSP A and OSP B, runs scenarios 1 to 4
```

`demo.py` prints one line per check and a summary; exit code 0 means everything is green.
Only some scenarios: `uv run python demo.py --scenarios 1,2,3`.

Keep the three processes running for your own calls (UI, curl, Swagger at `/docs`):

```bash
uv run python demo.py --serve           # CSP stand-in :8100, OSP A :8000, OSP B :8001; Ctrl-C stops them
curl http://localhost:8000/auth/realms/CX-Central/test/tokens/erika   # token of a test person
uv run python demo.py --no-start --scenarios 1,2,3                    # in a second terminal
```

`start_processes()` in `demo.py` shows the environment of each process (keys, trust list, CSP
addresses). One installation alone, with the built-in CSP stand-in of C3-M2 (no `CSP_URL`):
`SETTINGS_FILE=config/osp-a/installation.json uv run uvicorn --factory scaffold.app:create_app --port 8000`.

## What is inside

| Folder | Content |
|---|---|
| `portal_common/` | shared building blocks: token validation (`tokens`), `service_token(module)`, Problem Details (`problems`), installation settings (`settings`), neighbour calls with the error rule (`neighbours`), event relay (`events`), access to the CSP with `private_key_jwt` (`csp`), spec access (`spec`), spec-driven stand-ins (`standin`) |
| `modules/<module>/` | one folder per module of the spec; `c12_m1` is Process Orchestration (provided), all others are stand-ins |
| `scaffold/` | the installation app (`create_app`) and the module registry |
| `tools/` | test tools: Portal IAM (`test_iam`), shared CSP stand-in (`csp_standin`, own process), EBW-Connector double for attestations (`ebw_signer`), provider system double (`provider_double`), keys |
| `config/osp-a`, `config/osp-b` | per installation: settings (`installation.json`, FRAMEWORK 4.8), configuration variables of the modules (`module-config.json`, e.g. `ATTESTATION_CLIENT_IDS`, `ISSUER_MODE`; the process environment wins), clients at the EBW-Connector (`ebw-clients.json`, C5-M1 6.8) |
| `testdata/` | one test data package for all modules, IDs as in FRAMEWORK 7a (see [Test data](#test-data-framework-7a)) |
| `demo.py` | acceptance demo, HTTP only, with content checks |
| `tests/` | tests of every part, including scenarios 1 to 3 in one process |

### Rules that every module gets for free

- **Tokens** (C2-M5 7.6): RS256 against the JWKS of the installation's Portal IAM, `iss` of the own
  installation, `aud` `portal-core`. A token of another installation gives 401.
- **Permissions from the spec:** `tokens.require_spec(module, method, path)` takes the permission
  from `x-permission` in `openapi.yaml`. When the spec renames a permission, nothing changes in code.
- **Service token:** `tokens.service_token("c3-m1")` returns the token of the service account
  `svc-c3-m1` with every step and query permission of the spec. Process Orchestration uses it for
  steps and lookups, `neighbours.call()` for queries.
- **Problem Details:** route class `problems.route_class(module)`: validation 400 (not 422),
  unhandled errors 500 as Problem Details, steps always with `retryable`.
- **Errors of a neighbour:** 4xx -> 502 `neighbour-rejected`, `retryable: false`; 5xx, timeout,
  no connection -> 502 `upstream-error`, `retryable: true`.
- **Events:** `events.publish()` puts the event into the outbox and returns at once; a relay
  thread delivers it. A failing subscriber does not stop the others. Events with secret fields
  are refused.
- **Neighbour addresses** (FRAMEWORK 4.7), one notation: `<MODULE>_URL` (e.g. `C3_M1_URL`), else
  `MODULE_URLS` (JSON), else `MODULE_URLS_DEFAULT` (the scaffold sets `in-process`). `in-process`
  calls the module of the same installation through its ASGI interface, with token and checks.

## How a team replaces its module

1. Delete the stand-in `modules/<module>/router.py` (or start from it) and write the module there.
2. `router.py` provides `router` (an `APIRouter` under `/api/<module>`), `reset()`, optionally
   `on_event(event)` and `on_startup()`. Keep `__init__.py` empty, no other top-level modules.
3. Every operation of the spec that the module does not implement yet is added automatically
   (steps answer `DONE`, everything else 501), so the installation never has a missing path.
4. Read configuration in `reset()` or at call time, never at import.
5. `uv run pytest -q && uv run python demo.py` before every merge. `GET /scaffold/modules` shows
   which modules are stand-ins.

## Test data (FRAMEWORK 7a)

The test IDs, test persons and the start state of a hackathon installation come from
[FRAMEWORK 7a](https://github.com/tx-arena-events/portal-backend-spec/blob/main/FRAMEWORK.md#7a-common-test-ids);
the files under `testdata/` follow it, and entries that 7a does not define say
"scaffold-only, not in FRAMEWORK 7a" in `_comment`.

- **Start state:** C3-M1 holds only the company of the OSP. Erika, Max, Hans and Lieschen have accounts
  without a company, Otto (`OSP Admin`) and Rolf (`OSP Reviewer`) are OSP staff without a company
  (`testdata/persons-osp-a.json`). Scenario 1 binds Erika as `Company Admin`.
- **Companies:** Beispiel Maschinenbau GmbH (with Business Wallet) gets `c0000001-0000-4000-8000-000000000001`
  and the BPN `BPNLIUK2REMLK3F4` in scenario 1; Beispiel Logistik GmbH (`c0000002-…`, without Business
  Wallet, `COMMERCIAL_REG_NUMBER` `HRB 200002`) is in `testdata/applicant-companies.json` for the path with
  supporting documents, without a demo scenario.
- **Client of the UI at the EBW-Connector:** `osp-a-ui` and `osp-b-ui`, each registered in
  `config/osp-*/ebw-clients.json` and allowed in `ATTESTATION_CLIENT_IDS`. The verification attestation
  carries it as `client_id`; C1-M2 refuses an attestation for another client (`demo-osp`).
- **Scaffold-only:** Pia Provider (`Company Admin` of the OSP company, app provider in scenario 3), and for
  OSP B, for which 7a defines nothing, Gerd Beispiel, Rita Zweit, Beispiel Werkzeugbau GmbH and the OSP B
  company `33333333-…`.

## Credentials at the hackathon (OP-53)

At the hackathon no real credentials are issued: OSP A and OSP B both run on the route `STUB` of C4-M1
(C4-M1 11.6, 14). OSP A has `ISSUER_MODE` `OWN` with empty `ISSUER_ROUTES`, OSP B `ISSUER_MODE` `CSP`
with empty `CSP_ISSUER_URL` (`config/osp-*/module-config.json`). OSP B keeps the dataspace wallet of the CSP.

The path through the issuer of the CSP (route `CSP_ISSUER`, C4-M1 11.5) stays available for the demo at
the Community Days and is **not used at the hackathon (OP-53)**: `uv run python demo.py --csp-issuer`
points `CSP_ISSUER_URL` of OSP B to the issuer API of the CSP stand-in, and scenario 4 then checks the
credentials in the CSP wallet and the callbacks of the CSP issuer.

## Process Orchestration (C12-M1)

Runs every flow of `flows.yaml` of the spec (`FLOWS_FILE` replaces it). At start-up it checks the
flows against all `openapi.yaml` files and event tables of the spec and refuses to start on an
unknown step, lookup or event, a source that is not earlier, or a required input without a
source (README 6.2; `FLOWS_CHECK=warn` turns the last two into warnings). Lookups follow README
6.8 (operationId, field paths, object mappings, `join`); a failed lookup is an error of the step.
With an empty module address, the built-in stand-in answers with the results of README 11.3.
Processes are kept in memory.

## Provisional readings of the spec

These points are not (yet) fixed by the spec; the scaffold decides as follows:

1. **Error rule for neighbours** (above): not yet in FRAMEWORK 4.5.
2. **Service accounts `svc-<module>`** get all step and query permissions of the spec (like
   `svc-test-internal` in C2-M5 14.2), not only those of section 11 of the caller.
3. **Identity API of the CSP wallet:** `POST {identityApiUrl}/participants/{participantContextId}/credentials/request`
   with header `x-api-key` and body `{issuerDid, holderPid, credentials: [{type}]}`. The spec refers
   to the identity interface of the IdentityHub without naming path and body.
4. **Read side of the CSP** (`/v1/directory/{bpn}`, `/v1/discovery/{bpn}`, `/v1/trust-list`) and the
   test endpoints `/admin/...` are not in the spec.
5. **Terms:** one catalogue (`testdata/agreements.json`) maps `agreementKey` + `version` (C1-M2) to
   `agreementId` (C3-M1).
6. **Test persons and company IDs:** as in FRAMEWORK 7a (above). C1-M2 7.2 generates the `companyId`;
   the stand-in of C1-M2 takes the 7a ID for Beispiel Maschinenbau GmbH, so that the IDs of 7a appear
   in the flows. `demo.py` does not depend on it. The app provider of scenario 3 is Pia, not Otto:
   Otto, bound to the OSP company as in C2-M2 7.20, would approve his own offer (C8-M1 6.2, 403
   `own-offer`; C8-M1 open question 5).
7. **Stand-ins of C1-M2, C3-M1 and C3-M2** read and pass on the formats each spec defines for its
   own module; C3-M1 maps the C1-M2 application as C3-M1 11.1 describes.

## Not included yet

- Persistence: all modules and Process Orchestration keep their data in memory.
- A Keycloak test installation (C2-M5 14): the test IAM covers tokens, JWKS, client credentials and
  `private_key_jwt`, not the browser flows.
- The stand-ins cover the hackathon paths (scenarios 1 to 4); other operations answer 501.
- Retries of callbacks from the CSP stand-in.

## Licence

Apache License 2.0, see [LICENSE](LICENSE). Third-party packages are listed in
[DEPENDENCIES](DEPENDENCIES); their status must be checked with the Eclipse Dash license tool before
a release. JSON files cannot carry a licence header; they are covered by the licence of the repository.
