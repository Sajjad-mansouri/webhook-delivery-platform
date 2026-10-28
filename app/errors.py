from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.errors import ErrorInfo, ErrorResponse, FieldError


class AppError(Exception):
    def __init__(self, *, code: str, message: str, status_code: int) -> None:
        super().__int__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "too_many_requests",
    503: "service_unavailable",
}

HTTP_ERROR_MESSAGES = {
    400: "Bad request.",
    401: "Authentication required.",
    403: "Permission denied.",
    404: "Resource not found.",
    405: "Method not allowed.",
    409: "Conflict.",
    429: "Too many requests.",
    503: "Service temporarily unavailable.",
}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:

        del request

        body = ErrorResponse(error=ErrorInfo(code=exc.code, message=exc.message))

        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json", exclude_none=True),
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:

        del request
        status_code = exc.status_code
        code = HTTP_ERROR_CODES.get(status_code, "http_error")

        if status_code >= 500:
            message = HTTP_ERROR_MESSAGES.get(
                status_code, "An unexpected server error occurred."
            )

        else:
            message = HTTP_ERROR_MESSAGES.get(
                status_code, "The request could not be completed."
            )

        body = ErrorResponse(error=ErrorInfo(code=code, message=message))

        return JSONResponse(
            status_code=status_code,
            content=body.model_dump(mode="json", exclude_none=True),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        del request

        details = [
            FieldError(
                location=[
                    part if isinstance(part, (str, int)) else str(part)
                    for part in error["loc"]
                ],
                message=error["msg"],
                type=error["type"],
            )
            for error in exc.errors()
        ]

        body = ErrorResponse(
            error=ErrorInfo(
                code="validation_error",
                message="Request validation failed.",
                details=details,
            )
        )
        return JSONResponse(
            status_code=422, content=body.model_dump(mode="json", exclude_none=True)
        )
