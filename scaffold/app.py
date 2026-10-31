# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""One installation (OSP A or OSP B) as a modular monolith (FRAMEWORK 1).

    SETTINGS_FILE=config/osp-a/installation.json BASE_URL=http://localhost:8000 \
        uv run uvicorn --factory scaffold.app:create_app --port 8000

Start order: settings -> test tools (Portal IAM, EBW-Connector, provider system) -> modules
-> Notifications and Process Orchestration last. All neighbours default to `in-process`.
With `IAM_URL` the Portal IAM is that Keycloak (`iam/`), otherwise the in-memory IAM of the scaffold's
own tests. `TEST_TOOLS=false` leaves all test tools out (then `IAM_URL` and `EBW_CONNECTOR_URL` point
to real systems), and the realm is never reset.
"""
from __future__ import annotations

import contextlib
import json
import os

from fastapi import FastAPI

from portal_common import csp, events, http, problems, settings, standin, tokens

from tools import iam_backend

from . import registry


def _defaults() -> None:
    base = settings.base_url()
    os.environ.setdefault("MODULE_URLS_DEFAULT", "in-process")
    os.environ.setdefault("EBW_CONNECTOR_URL", base + "/ebw")
    # Configuration variables of the modules for this installation (`moduleConfigFile`, e.g.
    # ATTESTATION_CLIENT_IDS, ISSUER_MODE); the process environment wins. `{BASE_URL}` in a value
    # is the address of the installation.
    file = settings.resolve_path(settings.get("moduleConfigFile"))
    if file and file.exists():
        for k, v in json.loads(file.read_text(encoding="utf-8")).items():
            if not k.startswith("_"):
                os.environ.setdefault(k, str(v).replace("{BASE_URL}", base))


def reset_all(entries) -> None:
    """Fresh state in every module and test tool (IAM first, C11/C12 last)."""
    from tools import ebw_signer, provider_double, test_iam
    events.RELAY.reset()
    tokens.reset()
    csp.reset()
    test_iam.reset()
    if iam_backend.uses_keycloak() and os.environ.get("TEST_TOOLS", "true") == "true":
        from tools import keycloak_admin
        keycloak_admin.reset_realm()
    provider_double.reset()
    for e in entries:
        if e.module and hasattr(e.module, "reset"):
            e.module.reset()
    _ = ebw_signer  # the signing key stays, so attestations issued before a reset stay valid


def create_app() -> FastAPI:
    settings.load()
    _defaults()
    entries = registry.load()
    events.RELAY.clear_subscribers()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        events.RELAY.start()
        for e in entries:
            if e.module and hasattr(e.module, "on_startup"):
                e.module.on_startup()
        yield
        events.RELAY.stop()

    app = FastAPI(title=f"Portal installation {settings.get('ospName')}", lifespan=lifespan)
    problems.install(app)
    if os.environ.get("TEST_TOOLS", "true") == "true":
        from tools import ebw_signer, provider_double, test_iam
        for r in (ebw_signer.router, provider_double.router):
            app.include_router(r)
        if not iam_backend.uses_keycloak():
            app.include_router(test_iam.router)
    for e in entries:
        if e.module:
            app.include_router(e.module.router)
        app.include_router(standin.fallback_router(e.id, e.module.router if e.module else None))
        if e.module and hasattr(e.module, "on_event"):
            events.subscribe(e.id, e.module.on_event)
    app.state.entries = entries
    reset_all(entries)

    @app.get("/scaffold/modules", tags=["Scaffold"])
    def modules():
        return [{"module": e.id, "kind": e.kind} for e in entries]

    @app.get("/scaffold/events", tags=["Scaffold"])
    def event_log(eventType: str | None = None):
        """Test tool: events published so far (no secrets by construction) and subscriber failures."""
        return {"events": [x for x in events.RELAY.log if not eventType or x["eventType"] == eventType],
                "failures": events.RELAY.failures}

    @app.post("/scaffold/reset", tags=["Scaffold"])
    def reset():
        reset_all(entries)
        return {"reset": True}

    @app.get("/health", tags=["Scaffold"])
    def health():
        return {"status": "UP", "osp": settings.get("ospName")}

    http.register_local(settings.base_url(), app)
    return app


