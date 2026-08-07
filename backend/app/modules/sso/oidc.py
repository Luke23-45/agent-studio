"""
OIDC SSO client (P7-4) — authorization-code flow for operator sessions.

Discovery document is fetched lazily and cached; ``id_token`` verification
supports RS256 (JWKS) and HS256 (client-secret HMAC) — the two algorithms
used by mainstream IdPs (Keycloak, Auth0, Entra ID, Okta). All other
algorithms are rejected explicitly rather than silently accepted.

Secrets (client_secret) come from settings only; nothing is logged.
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import structlog
from httpx import AsyncClient

from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)

_JWKS_CACHE_TTL_SECONDS = 600
_CLOCK_SKEW_SECONDS = 300


class OidcError(RuntimeError):
    """Base class for OIDC flow failures (safe to surface as HTTP errors)."""


class OidcDisabledError(OidcError):
    pass


class OidcDiscoveryError(OidcError):
    pass


class OidcExchangeError(OidcError):
    pass


class OidcVerificationError(OidcError):
    pass


@dataclass
class IdTokenClaims:
    sub: str
    email: str | None
    name: str | None
    roles: list[str]


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    import base64

    return base64.urlsafe_b64decode(value + pad)


def _extract_roles(claims: dict[str, Any], claim_name: str) -> list[str]:
    raw = claims.get(claim_name)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [r.strip() for r in raw.split(",") if r.strip()]
    if isinstance(raw, list):
        return [str(r) for r in raw if r]
    return []


def map_oidc_role(roles: list[str]) -> str | None:
    """Highest-privilege Neryva role from OIDC_ROLE_MAP; None -> deny."""
    priority = {"super_admin": 4, "tenant_admin": 3, "operator": 2, "auditor": 1}
    best: str | None = None
    for idp_role, neryva_role in settings.OIDC_ROLE_MAP.items():
        if idp_role in roles and (
            best is None
            or priority.get(neryva_role, 0) > priority.get(best, 0)
        ):
            best = neryva_role
    return best


class OidcClient:
    """Discovery, authorization-URL building, code exchange, id_token checks."""

    def __init__(self) -> None:
        self._client: AsyncClient | None = None
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0

    async def _http(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(
                timeout=settings.OIDC_HTTP_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        return self._client

    def _require_configured(self) -> None:
        if not settings.OIDC_ENABLED or not settings.OIDC_DISCOVERY_URL:
            raise OidcDisabledError("OIDC SSO is not configured on this deployment")
        if not settings.OIDC_CLIENT_ID:
            raise OidcDisabledError("OIDC_CLIENT_ID is not set")

    async def discovery(self) -> dict[str, Any]:
        self._require_configured()
        if self._discovery is None:
            response = await (await self._http()).get(settings.OIDC_DISCOVERY_URL)
            if response.status_code != 200:
                raise OidcDiscoveryError(
                    f"OIDC discovery failed (HTTP {response.status_code})"
                )
            document = response.json()
            required = (
                "issuer",
                "authorization_endpoint",
                "token_endpoint",
                "jwks_uri",
            )
            missing = [k for k in required if k not in document]
            if missing:
                raise OidcDiscoveryError(
                    f"OIDC discovery document missing: {', '.join(missing)}"
                )
            self._discovery = document
        return self._discovery

    async def authorization_url(self, state: str, nonce: str) -> str:
        document = await self.discovery()
        params = {
            "response_type": "code",
            "client_id": settings.OIDC_CLIENT_ID,
            "redirect_uri": settings.OIDC_REDIRECT_URI or "",
            "scope": settings.OIDC_SCOPES,
            "state": state,
            "nonce": nonce,
        }
        return f"{document['authorization_endpoint']}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> tuple[dict[str, Any], str]:
        """Exchange an authorization code; returns (id_token_claims, id_token)."""
        document = await self.discovery()
        if not settings.OIDC_CLIENT_SECRET:
            raise OidcExchangeError("OIDC_CLIENT_SECRET is not set")
        response = await (await self._http()).post(
            document["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.OIDC_REDIRECT_URI or "",
                "client_id": settings.OIDC_CLIENT_ID,
                "client_secret": settings.OIDC_CLIENT_SECRET,
            },
        )
        if response.status_code != 200:
            raise OidcExchangeError(
                f"OIDC token endpoint returned HTTP {response.status_code}"
            )
        body = response.json()
        id_token = body.get("id_token")
        if not id_token:
            raise OidcExchangeError("OIDC token response missing id_token")
        return body, id_token

    async def verify_id_token(
        self,
        id_token: str,
        *,
        expected_nonce: str | None,
        expected_issuer: str | None = None,
    ) -> IdTokenClaims:
        """Verify signature, expiry, issuer, audience and nonce of an id_token."""
        parts = id_token.split(".")
        if len(parts) != 3:
            raise OidcVerificationError("malformed id_token")
        header_json, payload_json, signature = parts

        try:
            header = json.loads(_b64url_decode(header_json))
            payload = json.loads(_b64url_decode(payload_json))
        except (ValueError, json.JSONDecodeError) as e:
            raise OidcVerificationError("id_token not JSON") from e

        signed_content = f"{header_json}.{payload_json}".encode("utf-8")
        algorithm = header.get("alg")
        if algorithm == "RS256":
            await self._verify_rs256(signature, signed_content, header.get("kid"))
        elif algorithm == "HS256":
            self._verify_hs256(signature, signed_content)
        else:
            raise OidcVerificationError(
                f"unsupported id_token alg '{algorithm}' (support: RS256, HS256)"
            )

        now = time.time()
        exp = payload.get("exp")
        iat = payload.get("iat")
        if not isinstance(exp, (int, float)) or exp < now:
            raise OidcVerificationError("id_token expired")
        if not isinstance(iat, (int, float)) or iat > now + _CLOCK_SKEW_SECONDS:
            raise OidcVerificationError("id_token issued in the future")

        issuer = expected_issuer or (await self.discovery())["issuer"]
        if payload.get("iss") != issuer:
            raise OidcVerificationError("id_token issuer mismatch")

        audience = payload.get("aud")
        if isinstance(audience, str):
            audience = [audience]
        if settings.OIDC_CLIENT_ID not in (audience or []):
            raise OidcVerificationError("id_token audience mismatch")

        if expected_nonce is not None and payload.get("nonce") != expected_nonce:
            raise OidcVerificationError("id_token nonce mismatch")

        sub = payload.get("sub")
        if not sub:
            raise OidcVerificationError("id_token missing sub")
        return IdTokenClaims(
            sub=str(sub),
            email=payload.get("email"),
            name=payload.get("name"),
            roles=_extract_roles(payload, settings.OIDC_ROLE_CLAIM),
        )

    # -- signature primitives -------------------------------------------------

    async def _jwks(self) -> dict[str, Any]:
        if (
            self._jwks is None
            or time.time() - self._jwks_fetched_at > _JWKS_CACHE_TTL_SECONDS
        ):
            document = await self.discovery()
            response = await (await self._http()).get(document["jwks_uri"])
            if response.status_code != 200:
                raise OidcVerificationError(
                    f"JWKS fetch failed (HTTP {response.status_code})"
                )
            self._jwks = response.json()
            self._jwks_fetched_at = time.time()
        return self._jwks

    async def _verify_rs256(
        self, signature: str, signed_content: bytes, kid: str | None
    ) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        try:
            jwks = await self._jwks()
        except OidcVerificationError:
            raise
        key = None
        for candidate in jwks.get("keys", []):
            if candidate.get("kty") != "RSA":
                continue
            if kid is not None and candidate.get("kid") != kid:
                continue
            if "n" not in candidate or "e" not in candidate:
                continue
            n = int.from_bytes(_b64url_decode(candidate["n"]), "big")
            e = int.from_bytes(_b64url_decode(candidate["e"]), "big")
            key = rsa.RSAPublicNumbers(e, n).public_key()
            break
        if key is None:
            raise OidcVerificationError("no matching RSA JWK for id_token kid")
        try:
            key.verify(
                _b64url_decode(signature),
                signed_content,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as e:
            raise OidcVerificationError("id_token signature invalid") from e

    def _verify_hs256(self, signature: str, signed_content: bytes) -> None:
        secret = settings.OIDC_CLIENT_SECRET
        if not secret:
            raise OidcVerificationError("HS256 id_token requires OIDC_CLIENT_SECRET")
        expected = hmac.new(
            secret.encode("utf-8"), signed_content, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, _b64url_decode(signature)):
            raise OidcVerificationError("id_token signature invalid")
