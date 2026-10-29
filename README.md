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
| `config/osp-a`, `config/osp-b` | installation settings of OSP A and OSP B (FRAMEWORK 4.8) |
| `testdata/` | one test data package for all modules: OSP companies with all roles, one catalogue of terms with one ID per version, test persons in one state each, companies at the CSP |
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
6. **Test persons:** Erika Mustermann starts without a company (she onboards her company in
   scenario 1), although C2-M5 14.2 lists her as bound. Pia Provider (Company Admin of the OSP
   company), Gerd Beispiel and Rita Zweit (OSP B) and the OSP B company `33333333-…` are additions.
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
