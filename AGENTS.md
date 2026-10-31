# Instructions for coding agents

For coding agents (Claude Code, Codex, GitHub Copilot) and the people who use them.
`CLAUDE.md` and `.github/copilot-instructions.md` are links to this file.

## What this repository is

The portal backend of the Tractus-X hackathon as one Python application (Python 3.12, FastAPI).
Every module of the spec has a folder `modules/<id>/` and runs as a stand-in at first. Each team
replaces the stand-in of its module with its implementation. `demo.py` runs the flows across all
modules and shows whether they work together.

Green module tests are not enough: the module must work with the other modules on the first merge.
Merges break on field names between modules, missing service tokens, own error formats, own
variable names, own test data and silent errors. The rules below prevent this.

## Rules

1. Work only in your team's folder `modules/<id>/` (for example `modules/c3_m1/`). Outside it, you
   may only add your module tests as `tests/test_<id>_*.py` (for example `tests/test_c3_m1_steps.py`).
2. Never change another team's module, `portal_common/`, `scaffold/`, `tools/`, `iam/`, `testdata/`,
   `config/` or `spec/`. If something there is wrong or missing, open an issue and work with the
   current state.
3. The spec in `spec/` is the contract. Read `spec/FRAMEWORK.md`, then the `README.md`,
   `openapi.yaml` and `open-points.md` of your module. Paths, methods, field names, permissions,
   error types and events come from there. Do not invent names.
4. Use `portal_common` for shared tasks. Never build your own version of them:
   - `tokens`: token check; `tokens.require_spec(module, method, path)` takes the permission from
     `x-permission` in `openapi.yaml`; `tokens.service_token(module)` for calls to neighbours.
   - `problems`: `Problem` and `problems.route_class(module)` (validation errors 400, never 422;
     unhandled errors 500 as Problem Details).
   - `settings`: installation settings (FRAMEWORK 4.8), for example `settings.get("ospCompanyId")`.
   - `events`: `events.publish(event)`.
   - `sse`: Server-Sent Events for the UI, `sse.Broker()` and `sse.response(...)` (paths ending in `/events`).
   - `neighbours`: `neighbours.query(caller, target, function, **params)` calls a neighbour's
     interface function in-process or over HTTP with service token, with the shared error rule.
   - `standin`: `standin.router(module)`, `standin.perm(...)`, `standin.step_reply(...)`.
5. Follow the call convention below exactly.
6. Configuration variables: use the names exactly as in section 4 ("Configuration") of your module
   README. Read them from the process environment in `reset()` or at call time, never at import.
   `config/osp-*/module-config.json` sets the values per installation. Do not read the address
   variable of a neighbour yourself; `portal_common.neighbours` resolves it. It reads
   `<MODULE>_URL` (for example `C3_M1_URL`) and `MODULE_URLS`, not yet the names of section 4
   (`COMPANY_URL`, `REVIEW_URL`, ...). `neighbours.is_standin("<neighbour>")` tells you when to
   answer with your built-in stand-in (empty address).
7. Test data only from FRAMEWORK 7a and `testdata/` (helpers in `tools/testdata.py`). Do not
   create other IDs for objects that 7a lists.
8. Events: names and fields as in FRAMEWORK section 6 and section 10 of your README. Publish with
   `events.publish({"eventType": "...", ...})`. Receive events in `on_event(event)` in `router.py`.
9. No secrets (passwords, client secrets, tokens, API keys) in logs, events or step results
   (FRAMEWORK 4.6).
10. If the spec is unclear or contradicts itself, do not guess silently. Write the question in
    `modules/<id>/OPEN-QUESTIONS.md`, implement the reading the spec supports most directly, and
    name that reading there.

## Call convention between modules (binding)

1. Every module `modules/<id>/` (id like `c1_m2`) has `interface.py` with its public functions.
   Other modules import ONLY from there: `from modules.c1_m2.interface import get_application_internal`.
2. Names: step = step name in snake_case (`create-company` → `create_company`); query =
   operationId in snake_case (`getApplicationInternal` → `get_application_internal`).
3. Signatures: step `def create_company(input: dict) -> dict` (input = JSON body of
   `POST /steps/<step>` incl. `processId`, `stepRunId`; returns `{stepRunId, state, result}`);
   query `def get_application_internal(**params)` (path and query parameters by their openapi
   names, for example `applicationId=...`; returns the JSON body). No token in-process.
4. Errors: raise `portal_common.problems.Problem` with `type`, `status`, `retryable`. Example:
   `raise Problem("c3-m1", 409, "invalid-state", detail="...", retryable=False)` gives the `type`
   `urn:c3-m1:problem:invalid-state`.
5. The router is a thin HTTP layer (token, permission, then the interface function).
6. Neighbour address per FRAMEWORK 4.7: `in-process` → direct call of the interface function;
   URL → HTTP with service token (`portal_common.neighbours`); empty → built-in stand-in
   (`neighbours.is_standin(target)`). If README section 11 names an in-process function that
   differs from this convention (for example `get_company`), the convention wins.
7. C12 finds steps and queries by these names. Stand-in modules expose the same functions, so a
   team replaces the stand-in folder and nothing else changes.

The folder name uses `c3_m1`; the spec, the URL path, `Problem` and `service_token` use `c3-m1`.

## The module folder

`interface.py` (public functions), `router.py` (`router` under `/api/<id>`, `reset()`, optional
`on_event(event)` and `on_startup()`; remove `STANDIN = True`), an empty `__init__.py`. Further
files of the module (for example `service.py`, data files) belong in the same folder.
Operations you have not implemented yet are added from the spec (steps answer `DONE`, others 501).

## Definition of done

1. Your module tests pass: `uv run pytest -q tests/test_<id>_*.py`.
2. All tests pass: `uv run pytest -q`.
3. `uv run python demo.py` ends with exit code 0.
4. Open questions are in `modules/<id>/OPEN-QUESTIONS.md`.
5. A pull request to `main`.

## Commits

- The code is meant for Eclipse Tractus-X. The Eclipse Foundation requires a signed Eclipse
  Contributor Agreement (ECA) for the email address in the `Author` field of each commit. The ECA
  fulfils the sign-off of the Developer Certificate of Origin; Eclipse does not require a separate
  `Signed-off-by` line.
- The Eclipse Project Handbook asks contributors to disclose AI assistance. For AI-assisted
  commits, add the trailer `Assisted-by: <tool> (<model>)`, for example
  `Assisted-by: GitHub Copilot (GPT-4o-2024-08-06)`. For files that are largely AI-generated, the
  handbook also shows a disclosure in the file header; ask the maintainers before you use it.
- A person reviews every AI-generated change before it is committed.
- New `.py` files start with the same two header lines as the existing files
  (`Copyright (c) 2026 Contributors to the Eclipse Foundation`, `SPDX-License-Identifier: Apache-2.0`).

## How to run

```bash
git clone https://github.com/tx-arena-events/portal-backend-spec.git spec   # once; update: git -C spec pull --ff-only
uv sync                       # behind a TLS proxy: UV_SYSTEM_CERTS=1 uv sync (older uv: UV_NATIVE_TLS=1)
uv run pytest -q              # all tests
docker compose up -d          # once: Keycloak, the test Portal IAM of C2-M5 (iam/README.md)
uv run python demo.py         # CSP stand-in, OSP A and OSP B, scenarios 1 to 4; exit code 0 = green
uv run python demo.py --serve # keep the processes running (OSP A :8000, OSP B :8001, Swagger at /docs)
```

A token of a test person: `uv run python iam/test/get-token.py erika`. The Portal IAM is Keycloak: teams of
C2-M2 and C2-M3 call its Admin API with the service accounts `svc-c2-m2` and `svc-c2-m3`.

## Skills

- `.claude/skills/implement-module-from-spec/SKILL.md`: build or extend your module from the spec.
- `.claude/skills/review-module/SKILL.md`: check a module before a pull request.

The skills are plain Markdown. Claude Code loads them as skills; any other agent can use them, for
example: "read `.claude/skills/implement-module-from-spec/SKILL.md` and follow it".
