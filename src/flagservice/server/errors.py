"""Domain errors raised by the store and their HTTP responses."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from flagservice.core.validation import Problem


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


class BadRequest(Exception):
    pass


class PreconditionFailed(Exception):
    def __init__(self, current_version: int, current_etag: str) -> None:
        super().__init__("the flag has changed since it was read")
        self.current_version = current_version
        self.current_etag = current_etag


class InvalidRequest(Exception):
    """Semantic validation failures, reported in the same shape as FastAPI's 422s."""

    def __init__(self, problems: Sequence[Problem]) -> None:
        super().__init__("; ".join(problem.msg for problem in problems))
        self.problems = list(problems)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFound)
    def not_found(request: Request, exc: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Conflict)
    def conflict(request: Request, exc: Conflict) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(BadRequest)
    def bad_request(request: Request, exc: BadRequest) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PreconditionFailed)
    def precondition_failed(request: Request, exc: PreconditionFailed) -> JSONResponse:
        return JSONResponse(
            {
                "detail": str(exc),
                "current_version": exc.current_version,
                "current_etag": exc.current_etag,
            },
            status_code=412,
            headers={"ETag": exc.current_etag},
        )

    @app.exception_handler(InvalidRequest)
    def invalid(request: Request, exc: InvalidRequest) -> JSONResponse:
        detail = [
            {"type": "value_error", "loc": ["body", *problem.loc], "msg": problem.msg}
            for problem in exc.problems
        ]
        return JSONResponse({"detail": detail}, status_code=422)
