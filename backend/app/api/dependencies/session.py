"""
Connection-tier session-token resolution (Arch 6.4, P1-8).

``get_session_principal`` validates the ``Authorization: Bearer`` token
(expiry + revocation against the durable registry) and yields a
``SessionPrincipal`` scoped to exactly one tenant. Routes take the
requested tenant from the token — never from the client.

``get_conversation_principal`` accepts either surface (P1-10): API keys
(v1 contract, RBAC ``conversations:read``) or session tokens (customer
surfaces; no static-key path).
"""

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from backend.app.api.dependencies.auth import KEY_PREFIX, get_principal
from backend.app.session.tokens import (
    ExpiredSessionToken,
    InvalidSessionToken,
    RevokedSessionToken,
    SessionPrincipal,
    SessionTokenError,
    get_session_token_service,
)


@dataclass
class ConversationPrincipal:
    """Union principal for the conversation request path."""

    tenant_id: str | None
    end_user_id: str | None
    role: str
    is_session: bool
    surface_id: str | None = None


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer session token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return header[7:].strip()


async def get_session_principal(
    request: Request,
) -> SessionPrincipal:
    """Resolve the end-user session token (expiry + revocation checked)."""
    token = _bearer_token(request)
    try:
        return await get_session_token_service().resolve(token)
    except ExpiredSessionToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token expired",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except RevokedSessionToken:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session token revoked",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except (InvalidSessionToken, SessionTokenError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


async def get_conversation_principal(
    request: Request,
) -> ConversationPrincipal:
    """API-key auth (v1) or session-token auth (customer surfaces)."""
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""

    if token and not token.startswith(KEY_PREFIX):
        session = await get_session_principal(request)
        return ConversationPrincipal(
            tenant_id=session.tenant_id,
            end_user_id=session.end_user_id,
            role="end_user",
            is_session=True,
            surface_id=session.surface_id,
        )

    principal = await get_principal(request)
    if not principal.has_permission("conversations:read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{principal.role}' lacks permission 'conversations:read'",
        )
    return ConversationPrincipal(
        tenant_id=str(principal.tenant_id) if principal.tenant_id else None,
        end_user_id=None,
        role=principal.role,
        is_session=False,
    )
