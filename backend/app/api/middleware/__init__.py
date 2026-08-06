"""
API middleware for Neryva Agent Studio.

Provides correlation IDs, request/response logging, and error handling.
"""

import time
import uuid
from typing import Callable

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from structlog.contextvars import bound_contextvars

logger = structlog.get_logger(__name__)

CORRELATION_ID_HEADER = "X-Request-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Generates/forwards a correlation ID and binds it to all logs in this request."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or uuid.uuid4().hex
        request.state.correlation_id = correlation_id

        # structlog contextvars binding for the whole request
        with bound_contextvars(correlation_id=correlation_id):
            response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    """Logs all incoming requests and outgoing responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()

        # Log request
        logger.info(
            "request_started",
            method=request.method,
            path=request.url.path,
            client_ip=request.client.host if request.client else None,
        )

        # Process request
        response = await call_next(request)

        # Log response
        process_time = time.time() - start_time
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            process_time_ms=round(process_time * 1000, 2),
        )

        return response


class ErrorHandlingMiddleware(BaseHTTPMiddleware):
    """Handles uncaught exceptions and returns appropriate error responses."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            return await call_next(request)
        except Exception as e:
            logger.error(
                "unhandled_error",
                path=request.url.path,
                error=str(e),
                exc_info=True,
            )
            # Re-raise to let FastAPI handle it
            raise
