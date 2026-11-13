"""JSON error responses for the admin API: {"code", "message", "details"}."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from smtrading.common.errors import ErrorCode, SMTError

_STATUS = {ErrorCode.NOT_FOUND: 404, ErrorCode.FORBIDDEN: 403, ErrorCode.RATE_LIMITED: 429}


class NotAuthenticated(Exception):
    """No valid admin session: HTTP 401."""


def _body(code: str, message: str, details: dict | None = None) -> dict:
    return {"code": code, "message": message, "details": details}


async def _smt_error(request: Request, exc: SMTError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=_STATUS.get(exc.code, 400))


async def _not_authenticated(request: Request, exc: NotAuthenticated) -> JSONResponse:
    return JSONResponse(_body(str(ErrorCode.FORBIDDEN), "login richiesto"), status_code=401)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    # only field locations: the rejected input (maybe a password) is never echoed back
    fields = sorted({".".join(str(p) for p in e.get("loc", ()) if p != "body") for e in exc.errors()})
    return JSONResponse(
        _body(str(ErrorCode.INVALID_ORDER), "richiesta non valida", {"fields": fields}),
        status_code=400,
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(SMTError, _smt_error)
    app.add_exception_handler(NotAuthenticated, _not_authenticated)
    app.add_exception_handler(RequestValidationError, _validation_error)
