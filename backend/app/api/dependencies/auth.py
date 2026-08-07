"""
API authentication and authorization.

Implements scoped API keys (matrix item 1.1 / 2.2): only SHA-256 hashes are
stored, keys carry a role (RBAC, item 1.2) and an optional tenant binding.
A per-key in-memory token bucket provides API-level rate limiting (item 1.4).
"""

import asyncio
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Coroutine
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request, status

from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    OperatorSessionRepository,
    get_database_manager,
)
from backend.app.infrastructure.db.repositories import OPERATOR_TOKEN_PREFIX
from backend.app.infrastructure.patterns import RateLimitConfig, RateLimiter, RateLimitExceeded
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)

# Roles (RBAC). Order matters: later roles are more privileged.
ROLE_SUPER_ADMIN = "super_admin"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_OPERATOR = "operator"
ROLE_AUDITOR = "auditor"

ALL_ROLES = (ROLE_SUPER_ADMIN, ROLE_TENANT_ADMIN, ROLE_OPERATOR, ROLE_AUDITOR)

# Role -> allowed admin API surfaces
ROLE_PERMISSIONS: dict[str, set[str]] = {
    ROLE_SUPER_ADMIN: {
        "tenants:read", "tenants:write", "conversations:read",
        "conversations:write", "api_keys:manage", "audit:read",
        "escalations:read", "escalations:write",
        "webhooks:read", "webhooks:write",
        "knowledge:write", "evals:run",
        # P7-4 admin console surfaces
        "policies:read", "policies:write",
        "models:read", "models:write",
        "evals:read", "billing:read",
    },
    ROLE_TENANT_ADMIN: {
        "tenants:read", "conversations:read", "conversations:write",
        "escalations:read", "escalations:write",
        "webhooks:read", "webhooks:write",
        "knowledge:write", "evals:run",
        # P5-10 delegated admin: tenant-bound keys may manage their own
        # tenant's keys (enforced in the api-keys routes via assert_tenant_access).
        "api_keys:manage",
        # P7-4 admin console surfaces (scoped to the bound tenant)
        "policies:read", "policies:write",
        "models:read", "models:write",
        "evals:read", "billing:read",
    },
    ROLE_OPERATOR: {"conversations:read", "escalations:read", "escalations:write"},
    ROLE_AUDITOR: {"tenants:read", "audit:read"},
}

KEY_PREFIX = "nrv_live_"

# P7-4: privileged operator sessions minted by SSO / MFA-verified login.
# Resolved from "Authorization: Bearer <token>"; only the SHA-256 hash of
# the token is persisted (operator_sessions row). OPERATOR_TOKEN_PREFIX is
# imported from infrastructure/db/repositories.py (single source of truth).
MFA_PROOF_HEADER = "X-MFA-Proof"


def hash_api_key(api_key: str) -> str:
    """SHA-256 of the raw key. Only this is ever persisted."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Generate (raw_key, prefix, key_hash). The raw key is shown once."""
    raw = KEY_PREFIX + secrets.token_hex(24)
    return raw, raw[: len(KEY_PREFIX) + 8], hash_api_key(raw)


@dataclass
class ApiKeyPrincipal:
    key_id: str
    name: str
    role: str
    tenant_id: UUID | None
    scopes: list[str]

    def has_permission(self, permission: str) -> bool:
        return permission in ROLE_PERMISSIONS.get(self.role, set()) or "*" in self.scopes


def schedule_task(coro_factory: Callable[[], Coroutine]) -> None:
    """Fire-and-forget a coroutine, logging failures instead of crashing."""

    async def _runner() -> None:
        try:
            await coro_factory()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("background_task_failed", error=str(e))

    try:
        asyncio.get_running_loop().create_task(_runner())
    except RuntimeError:
        pass


_rate_limiter = RateLimiter(
    RateLimitConfig(
        max_requests=settings.RATE_LIMIT_MAX_REQUESTS,
        window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS,
    ),
    redis_url=settings.REDIS_URL,
)


async def initialize_rate_limiter() -> None:
    """Connect the shared rate limiter to Redis at app startup.

    Falls back to the per-process limiter when Redis is unavailable;
    the limiter never takes the API down.
    """
    await _rate_limiter.initialize()


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter


async def _resolve_operator_session(request: Request) -> ApiKeyPrincipal | None:
    """Resolve a privileged operator session from ``Authorization: Bearer``.

    Only tokens with the operator prefix are considered; any other bearer
    value is ignored so customer session-token surfaces are untouched.
    Returns None when no operator token is present.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token.startswith(OPERATOR_TOKEN_PREFIX):
        return None

    token_hash = hash_api_key(token)
    try:
        await _rate_limiter.acquire_or_raise(token_hash)
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
            headers={"Retry-After": str(int(settings.RATE_LIMIT_WINDOW_SECONDS))},
        )

    db = get_database_manager()
    record = await OperatorSessionRepository(db).get_by_hash(token_hash)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator session",
        )
    if record["revoked_at"] is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator session revoked",
        )
    expires = record["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator session expired",
        )

    return ApiKeyPrincipal(
        key_id=record["id"],
        name=record["name"],
        role=record["role"],
        tenant_id=UUID(record["tenant_id"]) if record["tenant_id"] else None,
        scopes=record["scopes"],
    )


async def get_principal(request: Request) -> ApiKeyPrincipal:
    """Resolve the calling API key into a principal."""
    if not settings.AUTH_ENABLED:
        return ApiKeyPrincipal(
            key_id="dev",
            name="development",
            role=ROLE_SUPER_ADMIN,
            tenant_id=None,
            scopes=["*"],
        )

    raw_key = request.headers.get(settings.API_KEY_HEADER)
    if not raw_key:
        operator = await _resolve_operator_session(request)
        if operator is not None:
            return operator
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing {settings.API_KEY_HEADER} header",
        )

    key_hash = hash_api_key(raw_key)

    try:
        await _rate_limiter.acquire_or_raise(key_hash)
    except RateLimitExceeded as e:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(e),
            headers={"Retry-After": str(int(settings.RATE_LIMIT_WINDOW_SECONDS))},
        )

    db = get_database_manager()
    keys = ApiKeyRepository(db)
    record = await keys.get_by_hash(key_hash)

    if not record:
        # Durable audit: awaited, not fire-and-forget, so failed-auth events
        # survive process exit (ISO-42001 A.9 evidence).
        await AuditRepository(db).add(
            action="auth.failure",
            resource_type="api_key",
            actor_type="api_key",
            details={"reason": "unknown_key"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )

    if record["revoked"]:
        await AuditRepository(db).add(
            action="auth.failure",
            resource_type="api_key",
            resource_id=record["id"],
            actor_type="api_key",
            actor_id=record["id"],
            details={"reason": "revoked"},
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key revoked",
        )

    if record["expires_at"]:
        expires = record["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires < datetime.now(timezone.utc):
            await AuditRepository(db).add(
                action="auth.failure",
                resource_type="api_key",
                resource_id=record["id"],
                actor_type="api_key",
                actor_id=record["id"],
                details={"reason": "expired"},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key expired",
            )

    schedule_task(lambda: keys.touch_usage(record["id"]))

    return ApiKeyPrincipal(
        key_id=record["id"],
        name=record["name"],
        role=record["role"],
        tenant_id=UUID(record["tenant_id"]) if record["tenant_id"] else None,
        scopes=record["scopes"],
    )


def require_permission(permission: str):
    """Dependency factory enforcing an RBAC permission."""

    async def _dependency(principal: ApiKeyPrincipal = Depends(get_principal)) -> ApiKeyPrincipal:
        if not principal.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{principal.role}' lacks permission '{permission}'",
            )
        return principal

    return _dependency


def assert_tenant_access(principal: ApiKeyPrincipal, tenant_id: UUID) -> None:
    """Enforce tenant scoping: tenant-bound keys can only reach their own tenant."""
    if principal.tenant_id is not None and principal.tenant_id != tenant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API key is scoped to a different tenant",
        )


def require_operator(principal: ApiKeyPrincipal = Depends(get_principal)) -> ApiKeyPrincipal:
    """Operator-or-above gate (harness and internal operator surfaces)."""
    if principal.role not in (ROLE_SUPER_ADMIN, ROLE_TENANT_ADMIN, ROLE_OPERATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{principal.role}' cannot access operator surfaces",
        )
    return principal


# ---------------------------------------------------------------------------
# MFA (TOTP) proof tokens (P7-4)
# ---------------------------------------------------------------------------


def _mfa_signing_key() -> bytes:
    """HMAC key for MFA proof tokens: env value or dev key-file fallback."""
    if settings.MFA_SIGNING_KEY:
        return settings.MFA_SIGNING_KEY.encode("utf-8")
    path = settings.MFA_SIGNING_KEY_FILE
    try:
        with open(path, "rb") as fh:
            key = fh.read().strip()
    except OSError:
        key = secrets.token_bytes(32)
        with open(path, "wb") as fh:
            fh.write(key)
    if len(key) < 16:
        raise RuntimeError("MFA signing key file must contain at least 16 bytes")
    return key


def sign_mfa_proof(key_id: str, expires_at: datetime) -> str:
    """HMAC-SHA256 proof binding a key id to an expiry instant."""
    key = _mfa_signing_key()
    payload = f"{key_id}|{int(expires_at.timestamp())}".encode("utf-8")
    signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return f"{key_id}.{int(expires_at.timestamp())}.{signature}"


def verify_mfa_proof(proof: str, key_id: str) -> bool:
    """Validate a proof token: shape, binding, freshness and signature."""
    try:
        claimed_key, exp_part, signature = proof.split(".")
        if claimed_key != key_id:
            return False
        expires_at = datetime.fromtimestamp(int(exp_part), tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return False
    now = datetime.now(timezone.utc)
    if not (now <= expires_at <= now + timedelta(seconds=settings.MFA_PROOF_TTL_SECONDS)):
        return False
    expected = sign_mfa_proof(key_id, expires_at)
    return hmac.compare_digest(expected, proof)


async def require_mfa_proof(
    request: Request, principal: ApiKeyPrincipal = Depends(get_principal)
) -> ApiKeyPrincipal:
    """Second-factor gate for privileged actions.

    Applies only to API-key principals whose key has MFA enabled (OIDC
    sessions inherit IdP MFA and always pass). Keys without MFA enrolled
    are untouched so the gate can be rolled out gradually.
    """
    db = get_database_manager()
    record = await ApiKeyRepository(db).get_by_id(principal.key_id)
    if record is None or not record["mfa_enabled"]:
        return principal

    proof = request.headers.get(MFA_PROOF_HEADER, "")
    if not proof:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mfa_required",
        )
    if not verify_mfa_proof(proof, principal.key_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mfa_required",
        )
    return principal
