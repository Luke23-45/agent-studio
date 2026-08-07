"""
P7-4 — Operator authentication: OIDC SSO + per-key MFA (TOTP).

SSO (authorization-code flow):

    GET  /auth/oidc/authorize    -> 302 to the IdP (state+nonce stored)
    GET  /auth/oidc/callback     -> exchange code, verify id_token,
                                    store a one-time exchange code, 302 to
                                    the frontend with ?code=...
    POST /auth/oidc/exchange     -> one-time code -> operator session token
    GET  /auth/oidc/status       -> {enabled} for the login page

The real bearer token never appears in a URL (it is minted server-side on
``/auth/oidc/exchange`` from a 60-second one-time code). Operator sessions
are rows in ``operator_sessions`` (only the SHA-256 hash is stored) and are
accepted by ``get_principal`` as ``Authorization: Bearer nry_ops_...``.

MFA (TOTP, self-service for API-key principals):

    GET    /auth/mfa/status   -> {enabled, sessionBased}
    POST   /auth/mfa/setup    -> generate pending secret (encrypted at rest)
    POST   /auth/mfa/confirm  -> verify a live code, activate the secret
    POST   /auth/mfa/proof    -> verify a live code, mint a short-lived
                                 proof token (X-MFA-Proof) that unlocks
                                 privileged actions
    DELETE /auth/mfa          -> disable (requires a valid proof)

Privileged routes gate on ``require_mfa_proof`` (auth.py): keys with MFA
enabled must present a valid proof header; OIDC sessions inherit IdP MFA.
"""

import secrets
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    MFA_PROOF_HEADER,
    ApiKeyPrincipal,
    get_principal,
    require_operator,
    sign_mfa_proof,
    verify_mfa_proof,
)
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    OperatorSessionRepository,
    get_database_manager,
)
from backend.app.infrastructure.keys.crypto import KeyUnavailableError, decrypt_secret, encrypt_secret
from backend.app.modules.security.totp import (
    generate_secret,
    otpauth_uri,
    verify_totp,
)
from backend.app.modules.sso.oidc import (
    OidcClient,
    OidcError,
    map_oidc_role,
)
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["Operator Auth (SSO/MFA)"])

_STATE_TTL_SECONDS = 600
_CODE_TTL_SECONDS = 60

# In-memory stores. Single-process: adequate for the console; a
# multi-worker deployment should move these to Redis (state/code are
# short-lived and single-use, so loss only forces a retry).
_state_store: dict[str, dict[str, Any]] = {}
_code_store: dict[str, dict[str, Any]] = {}

_client = OidcClient()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _expired(entry: dict[str, Any]) -> bool:
    return entry["expires_at"] < time.time()


def _prune_stores() -> None:
    for store in (_state_store, _code_store):
        for key in [k for k, v in store.items() if _expired(v)]:
            store.pop(key, None)


def _oidc_client() -> OidcClient:
    return _client


# ---------------------------------------------------------------------------
# SSO (OIDC)
# ---------------------------------------------------------------------------


@router.get("/oidc/status")
async def oidc_status() -> dict[str, bool]:
    """Whether SSO is enabled on this deployment (drives the login page)."""
    return {"enabled": bool(settings.OIDC_ENABLED and settings.OIDC_DISCOVERY_URL)}


@router.get("/oidc/authorize")
async def oidc_authorize() -> RedirectResponse:
    """Start the authorization-code flow; redirects to the IdP."""
    client = _oidc_client()
    try:
        state = secrets.token_urlsafe(24)
        nonce = secrets.token_urlsafe(24)
        url = await client.authorization_url(state, nonce)
    except OidcError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e)) from e
    _prune_stores()
    _state_store[state] = {
        "nonce": nonce,
        "expires_at": time.time() + _STATE_TTL_SECONDS,
    }
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/oidc/callback")
async def oidc_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> RedirectResponse:
    """IdP callback: exchange the code, verify the id_token, hand the
    frontend a one-time exchange code (never the bearer token)."""
    base = settings.OIDC_FRONTEND_REDIRECT
    if error:
        logger.warning("oidc_callback_error", error=error, description=error_description)
        return RedirectResponse(f"{base}?error={urlencode({'error': error})}")

    entry = _state_store.pop(state or "", None)
    if entry is None:
        logger.warning("oidc_callback_unknown_state")
        return RedirectResponse(f"{base}?error=invalid_state")
    if _expired(entry):
        return RedirectResponse(f"{base}?error=state_expired")
    if not code:
        return RedirectResponse(f"{base}?error=missing_code")

    client = _oidc_client()
    try:
        body, id_token = await client.exchange_code(code)
        claims = await client.verify_id_token(id_token, expected_nonce=entry["nonce"])
    except OidcError as e:
        logger.warning("oidc_callback_exchange_failed", error=str(e))
        return RedirectResponse(f"{base}?error=exchange_failed")

    role = map_oidc_role(claims.roles)
    if role is None:
        logger.warning("oidc_callback_role_denied", sub=claims.sub)
        return RedirectResponse(f"{base}?error=role_denied")

    exchange_code = secrets.token_urlsafe(32)
    _prune_stores()
    _code_store[exchange_code] = {
        "claims": {
            "sub": claims.sub,
            "name": claims.name or claims.email or claims.sub,
            "email": claims.email,
            "role": role,
            "tenant_id": None,
        },
        "expires_at": time.time() + _CODE_TTL_SECONDS,
    }
    return RedirectResponse(f"{base}?{urlencode({'code': exchange_code})}")


class OidcExchangeRequest(BaseModel):
    code: str = Field(..., min_length=16, description="One-time exchange code")


@router.post("/oidc/exchange")
async def oidc_exchange(payload: OidcExchangeRequest) -> dict[str, Any]:
    """Exchange a one-time code for an operator session bearer token."""
    entry = _code_store.pop(payload.code, None)
    if entry is None or _expired(entry):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired exchange code",
        )
    claims = entry["claims"]
    db = get_database_manager()
    raw_token, row = await OperatorSessionRepository(db).create(
        name=claims["name"],
        role=claims["role"],
        tenant_id=claims.get("tenant_id"),
        idp_sub=claims["sub"],
        auth_method="oidc",
    )
    await AuditRepository(db).add(
        action="auth.oidc_login",
        resource_type="operator_session",
        resource_id=row["id"],
        actor_type="operator_session",
        actor_id=row["id"],
        details={"idp_sub": claims["sub"], "role": claims["role"]},
    )
    return {
        "token": raw_token,
        "principal": {
            "keyId": row["id"],
            "name": row["name"],
            "role": row["role"],
            "tenantId": row["tenant_id"],
            "scopes": row["scopes"],
            "authEnabled": settings.AUTH_ENABLED,
        },
        "expiresAt": row["expires_at"].isoformat(),
    }


# ---------------------------------------------------------------------------
# MFA (TOTP) — self-service for API-key principals
# ---------------------------------------------------------------------------


@router.get("/mfa/status")
async def mfa_status(
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> dict[str, bool]:
    record = await ApiKeyRepository(get_database_manager()).get_by_id(principal.key_id)
    if record is None:
        return {"enabled": False, "sessionBased": True}
    return {"enabled": bool(record["mfa_enabled"]), "sessionBased": False}


class MfaSetupResponse(BaseModel):
    secret: str
    otpauthUri: str
    pending: bool = True


@router.post("/mfa/setup")
async def mfa_setup(
    principal: ApiKeyPrincipal = Depends(require_operator),
) -> MfaSetupResponse:
    """Generate a pending TOTP secret (encrypted at rest). Confirm to activate."""
    db = get_database_manager()
    record = await ApiKeyRepository(db).get_by_id(principal.key_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is managed by the identity provider for this session",
        )
    secret = generate_secret()
    try:
        encrypted = encrypt_secret(secret)
    except KeyUnavailableError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Secret encryption unavailable (master key not provisioned)",
        ) from e
    await ApiKeyRepository(db).update_mfa(
        principal.key_id, secret=encrypted, enabled=False
    )
    account = f"{principal.name} ({principal.key_id[:8]})"
    return MfaSetupResponse(
        secret=secret,
        otpauthUri=otpauth_uri(secret, account, settings.MFA_ISSUER),
    )


class MfaConfirmRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=8)


@router.post("/mfa/confirm")
async def mfa_confirm(
    payload: MfaConfirmRequest,
    principal: ApiKeyPrincipal = Depends(require_operator),
) -> dict[str, bool]:
    """Verify a live code against the pending secret and activate MFA."""
    db = get_database_manager()
    record = await ApiKeyRepository(db).get_by_id(principal.key_id)
    if record is None or not record["mfa_secret"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No pending MFA enrollment; call /auth/mfa/setup first",
        )
    try:
        pending = decrypt_secret(record["mfa_secret"])
    except Exception as e:
        logger.error("mfa_secret_decrypt_failed", key_id=principal.key_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored MFA secret cannot be decrypted (master key rotated?)",
        ) from e
    if not verify_totp(pending, payload.code):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid TOTP code"
        )
    await ApiKeyRepository(db).update_mfa(
        principal.key_id, secret=record["mfa_secret"], enabled=True
    )
    await AuditRepository(db).add(
        action="mfa.enabled",
        resource_type="api_key",
        resource_id=principal.key_id,
        actor_type="api_key",
        actor_id=principal.key_id,
    )
    return {"enabled": True}


class MfaProofRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=8)


@router.post("/mfa/proof")
async def mfa_proof(
    payload: MfaProofRequest,
    principal: ApiKeyPrincipal = Depends(require_operator),
) -> dict[str, Any]:
    """Verify a live code and mint a short-lived proof for privileged ops."""
    db = get_database_manager()
    record = await ApiKeyRepository(db).get_by_id(principal.key_id)
    if record is None or not record["mfa_secret"] or not record["mfa_enabled"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MFA is not enabled for this key",
        )
    try:
        secret = decrypt_secret(record["mfa_secret"])
    except Exception as e:
        logger.error("mfa_secret_decrypt_failed", key_id=principal.key_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored MFA secret cannot be decrypted (master key rotated?)",
        ) from e
    if not verify_totp(secret, payload.code):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid TOTP code"
        )
    from datetime import timedelta

    expires = _now() + timedelta(seconds=settings.MFA_PROOF_TTL_SECONDS)
    return {
        "proof": sign_mfa_proof(principal.key_id, expires),
        "expiresAt": expires.isoformat(),
    }


@router.delete("/mfa")
async def mfa_disable(
    request: Request,
    principal: ApiKeyPrincipal = Depends(require_operator),
) -> dict[str, bool]:
    """Disable MFA (requires a valid proof when already enabled)."""
    db = get_database_manager()
    repo = ApiKeyRepository(db)
    record = await repo.get_by_id(principal.key_id)
    if record is None or not record["mfa_enabled"]:
        await repo.update_mfa(principal.key_id, secret=None, enabled=False)
        return {"enabled": False}
    proof = request.headers.get(MFA_PROOF_HEADER, "")
    if not proof or not verify_mfa_proof(proof, principal.key_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mfa_required",
        )
    await repo.update_mfa(principal.key_id, secret=None, enabled=False)
    await AuditRepository(db).add(
        action="mfa.disabled",
        resource_type="api_key",
        resource_id=principal.key_id,
        actor_type="api_key",
        actor_id=principal.key_id,
    )
    return {"enabled": False}
