# Copyright (c) 2026 Contributors to the Eclipse Foundation
# SPDX-License-Identifier: Apache-2.0
"""Problem Details (RFC 9457, FRAMEWORK 4.5) for every module.

- `Problem(module, status, name, ...)` raised anywhere in a route becomes `application/problem+json`
  with `type = urn:<module>:problem:<name>`.
- `route_class(module)` is the route class for a module's `APIRouter` (a router cannot register
  exception handlers): request validation gives 400 `validation` (not 422), any unhandled
  exception gives 500 `internal-error`, and steps always carry `retryable`.
- `from_neighbour()` applies the rule for errors of a called neighbour: 4xx -> 502
  `neighbour-rejected` with `retryable: false`; 5xx, timeout, no connection -> 502
  `upstream-error` with `retryable: true`.
"""
from __future__ import annotations

import logging
import traceback

from fastapi import FastAPI, HTTPException, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

log = logging.getLogger("portal.problems")
MEDIA_TYPE = "application/problem+json"
NAMES = {400: "validation", 401: "unauthorized", 403: "forbidden", 404: "not-found", 405: "not-allowed",
         409: "conflict", 500: "internal-error", 501: "not-implemented", 502: "upstream-error"}


class Problem(Exception):
    def __init__(self, module: str, status: int, name: str | None = None, title: str | None = None,
                 detail: str | None = None, **ext):
        self.module, self.status = module, status
        self.name = name or NAMES.get(status, "error")
        self.title = title or self.name.replace("-", " ")
        self.detail, self.ext = detail, ext
        super().__init__(f"{self.status} {self.name}: {detail or self.title}")

    def body(self) -> dict:
        out = {"type": f"urn:{self.module}:problem:{self.name}", "title": self.title, "status": self.status}
        if self.detail:
            out["detail"] = self.detail
        return out | self.ext

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, media_type=MEDIA_TYPE)


def _validation(module: str, e: RequestValidationError) -> Problem:
    errors = [{"field": ".".join(str(x) for x in err.get("loc", ())), "message": err.get("msg")} for err in e.errors()]
    return Problem(module, 400, "validation", errors=errors)


def _to_problem(module: str, exc: Exception, is_step: bool) -> Problem:
    if isinstance(exc, Problem):
        p = exc
    elif isinstance(exc, RequestValidationError):
        p = _validation(module, exc)
    elif isinstance(exc, (HTTPException, StarletteHTTPException)):
        p = Problem(module, exc.status_code, detail=str(exc.detail) if exc.detail else None)
    else:
        log.error("unhandled error in %s: %s", module, "".join(traceback.format_exception(exc)))
        p = Problem(module, 500, "internal-error", detail=type(exc).__name__)
    if is_step and "retryable" not in p.ext:
        p.ext["retryable"] = p.status >= 500 and p.status != 501
    return p


def route_class(module: str) -> type[APIRoute]:
    class ProblemRoute(APIRoute):
        def get_route_handler(self):
            original = super().get_route_handler()
            is_step = "/steps/" in self.path

            async def handler(request: Request):
                try:
                    return await original(request)
                except Exception as exc:  # noqa: BLE001 - every error leaves as Problem Details
                    return _to_problem(module, exc, is_step).response()

            return handler

    return ProblemRoute


def install(app: FastAPI, module: str = "portal") -> None:
    """App-level fallback for routes without the route class (404 of unknown paths, 405, 500)."""

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError):
        return _validation(module, exc).response()

    @app.exception_handler(StarletteHTTPException)
    async def _http_handler(request: Request, exc: StarletteHTTPException):
        return Problem(module, exc.status_code).response()

    @app.exception_handler(Exception)
    async def _any_handler(request: Request, exc: Exception):
        return _to_problem(module, exc, "/steps/" in request.url.path).response()


def from_neighbour(module: str, neighbour: str, status: int | None, body: object = None) -> Problem:
    """Error rule for answers of a neighbour (proposal for FRAMEWORK 4.5, not yet in the spec)."""
    upstream = body.get("type") if isinstance(body, dict) else None
    if status is not None and 400 <= status < 500:
        return Problem(module, 502, "neighbour-rejected", detail=f"{neighbour} answered {status}",
                       neighbour=neighbour, neighbourStatus=status, neighbourType=upstream, retryable=False)
    return Problem(module, 502, "upstream-error", detail=f"{neighbour} answered {status or 'nothing'}",
                   neighbour=neighbour, neighbourStatus=status, neighbourType=upstream, retryable=True)
