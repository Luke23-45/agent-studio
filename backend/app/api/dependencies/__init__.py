"""
API dependencies for Neryva Agent Studio.

Provides dependency injection for database sessions, authentication, etc.
The primary auth path is API-key based (`dependencies.auth`); the helpers
here cover tenant header extraction used by integrations.
"""

from uuid import UUID

import structlog
from fastapi import HTTPException, Request, status

logger = structlog.get_logger(__name__)

TENANT_ID_HEADER = "X-Tenant-Id"


async def get_tenant_id_from_header(request: Request) -> UUID:
    """Extract the tenant ID from the `X-Tenant-Id` request header.

    Fails with a 400 when the header is absent or not a valid UUID.
    """
    raw = request.headers.get(TENANT_ID_HEADER)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Missing {TENANT_ID_HEADER} header",
        )
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {TENANT_ID_HEADER} header: {raw!r} is not a UUID",
        ) from None
