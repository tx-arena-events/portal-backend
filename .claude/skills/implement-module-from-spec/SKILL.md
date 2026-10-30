---
name: implement-module-from-spec
description: Build or extend one portal module in modules/<id>/ from its spec in spec/ so that it works with the other modules on the first merge. Use when asked to implement, replace the stand-in of, or add operations, steps, queries or events to a module such as C3-M1 or c3_m1.
---

# Implement a module from the spec

Follow the steps in this order. The rules in `AGENTS.md` apply throughout. Work only in
`modules/<id>/` and in `tests/test_<id>_*.py`.

## 1. Read the spec

Read in this order, completely:

1. `spec/FRAMEWORK.md`: sections 4 (conventions), 6 (events), 7 (steps) and 7a (test IDs), and the
   flows in section 5 in which your module takes part.
2. `spec/<Module>/README.md` of your module, all sections.
3. `spec/<Module>/openapi.yaml`.
4. `spec/<Module>/open-points.md` and the points with your module in `spec/open-points.md`.
5. For every neighbour in section 11 of your README: the operation you call in the neighbour's
   `openapi.yaml`, so that you use its field names.

Update the spec first: `git -C spec pull --ff-only`.

## 2. List what the module must provide

Write a short list (in your answer or as notes, not as a file in the repository):

- Operations: method, path, `operationId`, `x-permission`, `x-permission-scope` (from `openapi.yaml`).
- Steps (`/steps/...`, README section 8): step name, Python name (`create-company` → `create_company`),
  input fields, preconditions, result fields, event.
- Queries (`/internal/...`, README section 9): `operationId`, Python name
  (`getApplicationInternal` → `get_application_internal`), parameters, response fields.
- Events the module publishes (README section 10, FRAMEWORK 6) and events it receives.
- Configuration variables (README section 4), with the exact names.
- Calls to neighbours (README section 11): target module, operation, needed permission, built-in
  stand-in.
- Error types (README section 12).

## 3. Start from the stand-in

Read `modules/<id>/router.py` and `modules/<id>/interface.py` of the stand-in. Keep every public
function name of `interface.py` and its signature. Other modules and Process Orchestration call
these names. You may rewrite the bodies. Remove `STANDIN = True`.

## 4. Implement

- `interface.py`: one function per step and per query, following the call convention in
  `AGENTS.md`:
  - step: `def create_company(input: dict) -> dict`, returns
    `{"stepRunId": input["stepRunId"], "state": "DONE", "result": {...}}`;
  - query: `def get_application_internal(**params)`, parameters by their openapi names, returns
    the JSON body.
  - Errors: `raise Problem("<module-id>", status, "<name>", detail=..., retryable=...)` with the
    names from README section 12 and FRAMEWORK 4.5.
  - No token checks here. The caller in the same process has no token.
- `router.py`: a thin HTTP layer. Create the router with `standin.router(MODULE)` (base path and
  Problem Details). Each route checks the token and the permission with `standin.perm(MODULE,
  method, path)` (or `tokens.require_spec`), checks the scope (`company`, `osp`, `own`,
  FRAMEWORK 4.3), then calls the interface function. Business logic stays out of the router.
  Provide `reset()` and, if you receive events, `on_event(event)`.
- Own data: keep it in the module (in memory is fine for the hackathon). Clear it in `reset()`.
  Never read or write the data of another module.
- Steps are idempotent (FRAMEWORK 4.4): find the object by the business key from the input,
  create only what is missing, return the same result again, and publish events only when
  something changed.
- Events: `events.publish({"eventType": "...", ...})` with exactly the fields of FRAMEWORK 6 and
  README section 10. No secrets.
- Configuration: read the variables of README section 4 with their exact names from `os.environ`
  in `reset()` or at call time. Installation settings with `settings.get(...)`.

## 5. Neighbours

For every call in README section 11:

- Call a neighbour with `portal_common.neighbours.query("<your-module>", "<neighbour>",
  "<function>", **params)`, for example `neighbours.query("c3-m1", "c1-m2",
  "get_application_internal", applicationId=app_id)`. It resolves the address as FRAMEWORK 4.7
  defines it: `in-process` → direct call of the function in `modules.<neighbour>.interface`; a
  URL → HTTP with the service token. Either way a neighbour error arrives as
  `502 neighbour-rejected` (not retryable) or `502 upstream-error` (retryable).
- Empty address → the built-in stand-in described in section 11; `neighbours.is_standin("<neighbour>")`
  is true then. It answers with the field names of the neighbour's spec, not with names you choose.
- The function name is always the `operationId` in snake_case, also where section 11 names
  another in-process function (for example `get_company` → `query_company`).
- Import only from `modules.<neighbour>.interface`, never from its `router.py` or other files.
- A failed call is an error, not "not found". Only a 404 or an empty list means "not found".

## 6. Acceptance tests

- Write one pytest test per case of README section 13 in `tests/test_<id>_*.py`.
- Use the fixtures and helpers of `tests/conftest.py` (`person_token(...)`, `bearer(...)`) and
  the test data of FRAMEWORK 7a and `testdata/`.
- Check content, not only status codes: field values, the number of events, the state after a
  repetition, the `type` and `retryable` of errors.
- Test each step twice with the same input (idempotency).
- Test at least one call per interface function directly (in-process) and one over HTTP with a
  token.

## 7. Run the checks

```bash
uv run pytest -q tests/test_<id>_*.py
uv run pytest -q
uv run python demo.py
```

All three must pass. If `demo.py` fails at a step of your module, fix your module. If it fails
in another module, open an issue and do not change that module.

## 8. Open questions

Write every point where the spec is unclear, contradicts itself or is missing into
`modules/<id>/OPEN-QUESTIONS.md`: the question, the spec section, the reading you implemented,
and who could decide it (another module, the spec authors). Do not decide such points silently.

## 9. Final checklist

- [ ] Every operation of `openapi.yaml` that the team planned is implemented with the same path,
      method and permission.
- [ ] `interface.py` has every step and query with the names and signatures of the call
      convention; the stand-in names are kept.
- [ ] The router only checks token, permission and scope and then calls `interface.py`.
- [ ] Errors are `Problem` with the type names of the spec; steps always carry `retryable`.
- [ ] Configuration variables have the names of README section 4.
- [ ] Events have the names and fields of FRAMEWORK 6 and README section 10.
- [ ] No secrets in logs, events or results.
- [ ] Steps are idempotent.
- [ ] Imports from other modules come only from their `interface.py`.
- [ ] Test data comes only from FRAMEWORK 7a and `testdata/`.
- [ ] `uv run pytest -q` and `uv run python demo.py` pass.
- [ ] `modules/<id>/OPEN-QUESTIONS.md` lists the open points.
- [ ] Nothing outside `modules/<id>/` and `tests/test_<id>_*.py` changed.
