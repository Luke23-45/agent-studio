"""
End-user session tokens (Arch 6.4, P1-8).

Anonymous bootstrap mints a short-lived, opaque bearer token bound to
(tenant, end_user, surface, device, expiry, scopes). The token string is
a Fernet-encrypted payload (jti + claims); the durable ``session_tokens``
row is the revocation source of truth — resolution decrypts, checks
expiry, then consults the row's ``revoked_at`` on every request. Tokens
are scoped to exactly one tenant; identity never crosses tenants.
"""

import json
import structlog
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from backend.app.infrastructure.db import (
    EndUserRepository,
    SessionTokenRepository,
)
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)


class SessionTokenError(Exception):
    pass


class InvalidSessionToken(SessionTokenError):
    pass


class ExpiredSessionToken(SessionTokenError):
    pass


class RevokedSessionToken(SessionTokenError):
    pass


class UnknownEndUserError(SessionTokenError):
    pass


@dataclass
class SessionPrincipal:
    token_id: str
    tenant_id: str
    end_user_id: str
    surface_id: str | None
    device_id: str | None
    scopes: list[str]
    expires_at: datetime


def _fernet() -> Any:
    from cryptography.fernet import Fernet

    return Fernet


@lru_cache(maxsize=1)
def _token_key() -> bytes:
    if settings.SESSION_TOKEN_ENCRYPTION_KEY:
        import base64

        try:
            raw = settings.SESSION_TOKEN_ENCRYPTION_KEY.encode("ascii")
            if len(base64.urlsafe_b64decode(raw)) != 32:
                raise ValueError
            return raw
        except Exception as e:
            raise SessionTokenError(
                "SESSION_TOKEN_ENCRYPTION_KEY is not a valid Fernet key"
            ) from e

    key_file = Path(settings.SESSION_TOKEN_ENCRYPTION_KEY_FILE)
    if key_file.exists():
        return key_file.read_bytes().strip()

    if settings.is_production:
        raise SessionTokenError(
            "SESSION_TOKEN_ENCRYPTION_KEY is not set; refusing to auto-generate"
            " a token key in production"
        )

    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32))
    key_file.write_bytes(key)
    logger.warning(
        "session_token_key_generated_dev_fallback",
        path=str(key_file),
        hint="set SESSION_TOKEN_ENCRYPTION_KEY in production",
    )
    return key


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionTokenService:
    def __init__(self, db: Any):
        self.db = db
        self.end_users = EndUserRepository(db)
        self.tokens = SessionTokenRepository(db)

    async def mint(
        self,
        tenant_id: str,
        *,
        device_id: str | None = None,
        end_user_id: str | None = None,
        surface_id: str | None = None,
        scopes: list[str] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Mint a short-lived session token (anonymous bootstrap).

        With no ``end_user_id`` the end user is keyed by the per-device
        anonymous identity; the minted end_user_id is returned so the
        client can persist it (device continuity).
        """
        if end_user_id:
            end_user = await self.end_users.get_by_id(tenant_id, end_user_id)
            if end_user is None:
                raise UnknownEndUserError(
                    f"end user {end_user_id} does not exist for tenant {tenant_id}"
                )
        else:
            if not device_id:
                raise UnknownEndUserError(
                    "device_id is required for anonymous bootstrap"
                )
            end_user = await self.end_users.get_or_create_anonymous(
                tenant_id, f"anon:{device_id}"
            )

        ttl = ttl_seconds or settings.SESSION_TOKEN_TTL_SECONDS
        jti = str(uuid4())
        issued_at = _now()
        expires_at = issued_at + timedelta(seconds=ttl)
        payload = {
            "jti": jti,
            "tenant_id": tenant_id,
            "end_user_id": end_user["id"],
            "surface_id": surface_id,
            "device_id": device_id,
            "scopes": scopes or [],
            "iat": issued_at.isoformat(),
            "exp": expires_at.isoformat(),
        }
        token = _fernet()(_token_key()).encrypt(
            json.dumps(payload).encode("utf-8")
        ).decode("ascii")

        await self.tokens.create(
            jti,
            tenant_id,
            end_user["id"],
            surface_id=surface_id,
            device_id=device_id,
            scopes=scopes or [],
            expires_at=expires_at,
        )
        logger.info(
            "session_token_minted",
            tenant_id=tenant_id,
            end_user_id=end_user["id"],
            surface_id=surface_id,
            device_id=device_id,
        )
        return {
            "token": token,
            "expires_at": expires_at,
            "end_user_id": end_user["id"],
        }

    async def resolve(self, token: str) -> SessionPrincipal:
        """Decrypt + validate a bearer token (expiry, revocation)."""
        try:
            raw = _fernet()(_token_key()).decrypt(token.encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except Exception as e:
            raise InvalidSessionToken("token is not a valid session token") from e

        expires_at = datetime.fromisoformat(payload["exp"])
        if expires_at <= _now():
            raise ExpiredSessionToken("session token expired")

        row = await self.tokens.get(payload["jti"])
        if row is None:
            raise InvalidSessionToken("token not found in registry")
        if row["revoked_at"] is not None:
            raise RevokedSessionToken("session token revoked")
        if row["tenant_id"] != payload["tenant_id"]:
            raise InvalidSessionToken("token claims do not match registry")

        return SessionPrincipal(
            token_id=payload["jti"],
            tenant_id=payload["tenant_id"],
            end_user_id=payload["end_user_id"],
            surface_id=payload.get("surface_id"),
            device_id=payload.get("device_id"),
            scopes=payload.get("scopes", []),
            expires_at=expires_at,
        )

    async def revoke(self, token: str) -> bool:
        """Revoke a token (permanently invalidates the bearer string)."""
        try:
            principal = await self.resolve(token)
        except SessionTokenError:
            return False
        revoked = await self.tokens.revoke(principal.token_id)
        logger.info(
            "session_token_revoked",
            tenant_id=principal.tenant_id,
            token_id=principal.token_id,
        )
        return revoked


_service: Optional[SessionTokenService] = None


def init_session_token_service(db: Any) -> SessionTokenService:
    global _service
    _service = SessionTokenService(db)
    return _service


def get_session_token_service() -> SessionTokenService:
    if _service is None:
        raise RuntimeError("Session token service not initialized")
    return _service
