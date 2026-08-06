"""
End-user session-token API (Arch 6.4, P1-8).

``POST /session-tokens`` is the anonymous bootstrap: a per-device identity
mints a short-lived bearer token bound to (tenant, surface, end_user,
device, expiry, scopes). ``POST /session-tokens/revoke`` invalidates a
token; ``GET /session-tokens/me`` resolves the current token (device
continuity / session checks). These routes replace the legacy static
tenant-key-in-HTML flow: customer surfaces authenticate by session token
only.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.session import get_session_principal
from backend.app.infrastructure.db import get_database_manager
from backend.app.session.limits import get_end_user_limits
from backend.app.session.tokens import (
    SessionPrincipal,
    SessionTokenError,
    UnknownEndUserError,
    get_session_token_service,
)

router = APIRouter()


class MintSessionTokenRequest(BaseModel):
    tenant_id: str = Field(..., min_length=1)
    device_id: str = Field(..., min_length=1, description="Per-device identity (local storage)")
    surface_id: str | None = None
    end_user_id: str | None = Field(
        None, description="Previously minted end-user identity (device continuity)"
    )
    scopes: list[str] = Field(default_factory=list)


class RevokeSessionTokenRequest(BaseModel):
    token: str = Field(..., min_length=1)


@router.post(
    "/session-tokens",
    summary="Mint a short-lived end-user session token (anonymous bootstrap)",
)
async def mint_session_token(request: MintSessionTokenRequest) -> dict[str, Any]:
    service = get_session_token_service()
    try:
        result = await service.mint(
            request.tenant_id,
            device_id=request.device_id,
            end_user_id=request.end_user_id,
            surface_id=request.surface_id,
            scopes=request.scopes,
        )
    except UnknownEndUserError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None
    except SessionTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Session token service unavailable: {exc}",
        ) from None

    limits = get_end_user_limits()
    if not await limits.enter_session(
        request.tenant_id, result["end_user_id"]
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many concurrent sessions for this end user",
            headers={"Retry-After": str(limits.config.session_lease_seconds)},
        )
    return result


@router.post("/session-tokens/revoke", summary="Revoke a session token")
async def revoke_session_token(request: RevokeSessionTokenRequest) -> dict[str, Any]:
    service = get_session_token_service()
    revoked = await service.revoke(request.token)
    return {"revoked": revoked}


@router.get(
    "/session-tokens/me",
    summary="Resolve the current session token (device continuity)",
)
async def session_me(
    principal: SessionPrincipal = Depends(get_session_principal),
) -> dict[str, Any]:
    return {
        "tenant_id": principal.tenant_id,
        "end_user_id": principal.end_user_id,
        "surface_id": principal.surface_id,
        "device_id": principal.device_id,
        "scopes": principal.scopes,
        "expires_at": principal.expires_at,
    }
