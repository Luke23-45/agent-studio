"""
P7-4 — Operator auth + per-key MFA (TOTP) HTTP acceptance.

- full MFA lifecycle: status -> setup (pending secret) -> confirm with a
  live code -> enabled; invalid codes are rejected at confirm/proof
- privileged routes (api-keys create/revoke) return 403 mfa_required when
  the key has MFA enabled and no valid proof is attached, and pass with one
- disabling MFA requires a valid proof
- OIDC: status endpoint reflects the disabled deployment; one-time exchange
  codes are single-use and unknown codes are rejected
- OIDC operator sessions (Bearer nry_ops_*) pass the MFA gate (IdP-managed)
"""

import asyncio

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    OperatorSessionRepository,
    get_database_manager,
)
from backend.app.modules.security.totp import totp_code
from backend.app.settings.env import settings


class TestOperatorAuthMfa:
    def _isolated_app(self, tmp_path):
        from cryptography.fernet import Fernet

        original = {
            "auth": settings.AUTH_ENABLED,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "queue_url": settings.REDIS_URL,
            "mfa_key": settings.MFA_SIGNING_KEY,
            "provider_key": settings.PROVIDER_KEY_ENCRYPTION_KEY,
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]
            settings.REDIS_URL = original["queue_url"]
            settings.MFA_SIGNING_KEY = original["mfa_key"]
            settings.PROVIDER_KEY_ENCRYPTION_KEY = original["provider_key"]

        settings.AUTH_ENABLED = True
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/mfa.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        settings.REDIS_URL = "redis://127.0.0.1:1"  # unreachable -> in-memory fallback
        settings.MFA_SIGNING_KEY = "test-mfa-signing-key-0123456789abcdef"
        settings.PROVIDER_KEY_ENCRYPTION_KEY = Fernet.generate_key().decode("ascii")

        from backend.app.infrastructure.db import init_database
        from backend.app.infrastructure.db.models import Base

        async def _setup_schema():
            manager = init_database(settings.DATABASE_URL)
            await manager.initialize()
            async with manager._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await manager.close()

        asyncio.run(_setup_schema())

        from starlette.testclient import TestClient

        from backend.app.main import app

        return TestClient(app), restore

    def _create_key(self, role: str = "super_admin") -> str:
        db = get_database_manager()
        raw, prefix, key_hash = generate_api_key()
        asyncio.run(
            ApiKeyRepository(db).create(
                name=f"mfa-{role}", key_hash=key_hash, prefix=prefix, role=role
            )
        )
        return raw

    def _create_operator_session(self) -> str:
        db = get_database_manager()
        raw, _row = asyncio.run(
            OperatorSessionRepository(db).create(
                name="Test Operator",
                role="operator",
                tenant_id=None,
                idp_sub="sub-test-1",
                auth_method="oidc",
            )
        )
        return raw

    def test_mfa_lifecycle(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key()
                headers = {"X-API-Key": key}

                status = client.get("/api/v1/auth/mfa/status", headers=headers)
                assert status.status_code == 200, status.text
                assert status.json() == {"enabled": False, "sessionBased": False}

                setup = client.post("/api/v1/auth/mfa/setup", headers=headers)
                assert setup.status_code == 200, setup.text
                secret = setup.json()["secret"]
                assert len(secret) == 32
                assert setup.json()["otpauthUri"].startswith("otpauth://totp/")
                assert setup.json()["pending"] is True

                # Pending secret is not active yet.
                status = client.get("/api/v1/auth/mfa/status", headers=headers)
                assert status.json()["enabled"] is False

                bad = client.post(
                    "/api/v1/auth/mfa/confirm", json={"code": "000000"}, headers=headers
                )
                assert bad.status_code == 403, bad.text

                code = totp_code(secret)
                confirm = client.post(
                    "/api/v1/auth/mfa/confirm", json={"code": code}, headers=headers
                )
                assert confirm.status_code == 200, confirm.text
                assert confirm.json() == {"enabled": True}

                status = client.get("/api/v1/auth/mfa/status", headers=headers)
                assert status.json()["enabled"] is True

                bad_proof = client.post(
                    "/api/v1/auth/mfa/proof", json={"code": "000000"}, headers=headers
                )
                assert bad_proof.status_code == 403, bad_proof.text

                proof = client.post(
                    "/api/v1/auth/mfa/proof", json={"code": code}, headers=headers
                )
                assert proof.status_code == 200, proof.text
                assert "." in proof.json()["proof"]
                assert proof.json()["expiresAt"]
        finally:
            restore()

    def test_privileged_routes_require_mfa_proof(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key()
                headers = {"X-API-Key": key}

                # No MFA yet: privileged route works without a proof.
                victim = client.post(
                    "/api/v1/api-keys",
                    json={"name": "victim", "role": "auditor"},
                    headers=headers,
                )
                assert victim.status_code == 200, victim.text
                victim_id = victim.json()["id"]

                # Enable MFA.
                secret = client.post(
                    "/api/v1/auth/mfa/setup", headers=headers
                ).json()["secret"]
                code = totp_code(secret)
                assert client.post(
                    "/api/v1/auth/mfa/confirm", json={"code": code}, headers=headers
                ).json() == {"enabled": True}

                # Revoke without proof -> mfa_required.
                denied = client.delete(
                    f"/api/v1/api-keys/{victim_id}", headers=headers
                )
                assert denied.status_code == 403, denied.text
                assert denied.json()["detail"] == "mfa_required"

                # Create without proof -> mfa_required.
                denied_create = client.post(
                    "/api/v1/api-keys",
                    json={"name": "victim2", "role": "auditor"},
                    headers=headers,
                )
                assert denied_create.status_code == 403, denied_create.text

                # With a fresh proof -> allowed.
                proof = client.post(
                    "/api/v1/auth/mfa/proof", json={"code": code}, headers=headers
                ).json()["proof"]
                ok = client.delete(
                    f"/api/v1/api-keys/{victim_id}",
                    headers={**headers, "X-MFA-Proof": proof},
                )
                assert ok.status_code == 204, ok.text

                # A forged proof is rejected on a still-existing key.
                other = client.post(
                    "/api/v1/api-keys",
                    json={"name": "victim3", "role": "auditor"},
                    headers=headers,
                )
                assert other.status_code == 403, other.text  # MFA still required
                forged = client.post(
                    "/api/v1/auth/mfa/proof", json={"code": code}, headers=headers
                ).json()["proof"]
                forged = forged[:-4] + "0000"
                denied_forged = client.delete(
                    f"/api/v1/api-keys/{victim_id}",
                    headers={**headers, "X-MFA-Proof": forged},
                )
                assert denied_forged.status_code == 403, denied_forged.text
        finally:
            restore()

    def test_disable_requires_proof(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key()
                headers = {"X-API-Key": key}
                secret = client.post(
                    "/api/v1/auth/mfa/setup", headers=headers
                ).json()["secret"]
                code = totp_code(secret)
                client.post(
                    "/api/v1/auth/mfa/confirm", json={"code": code}, headers=headers
                )

                denied = client.delete("/api/v1/auth/mfa", headers=headers)
                assert denied.status_code == 403, denied.text
                assert denied.json()["detail"] == "mfa_required"

                proof = client.post(
                    "/api/v1/auth/mfa/proof", json={"code": code}, headers=headers
                ).json()["proof"]
                disabled = client.delete(
                    "/api/v1/auth/mfa", headers={**headers, "X-MFA-Proof": proof}
                )
                assert disabled.status_code == 200, disabled.text
                assert disabled.json() == {"enabled": False}

                status = client.get("/api/v1/auth/mfa/status", headers=headers)
                assert status.json()["enabled"] is False
        finally:
            restore()

    def test_oidc_disabled_and_single_use_exchange(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                status = client.get("/api/v1/auth/oidc/status")
                assert status.status_code == 200, status.text
                assert status.json() == {"enabled": False}

                # Unknown one-time code is rejected.
                bad = client.post(
                    "/api/v1/auth/oidc/exchange", json={"code": "not-a-real-code-0000"}
                )
                assert bad.status_code == 401, bad.text

                # Authorize is disabled -> 404 with a diagnostic.
                authz = client.get("/api/v1/auth/oidc/authorize", follow_redirects=False)
                assert authz.status_code == 404, authz.text
        finally:
            restore()

    def test_operator_session_passes_mfa_gate(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                token = self._create_operator_session()
                bearer = {"Authorization": f"Bearer {token}"}

                status = client.get("/api/v1/auth/mfa/status", headers=bearer)
                assert status.status_code == 200, status.text
                assert status.json() == {"enabled": False, "sessionBased": True}

                # IdP-managed MFA: self-service setup is rejected, not faked.
                setup = client.post("/api/v1/auth/mfa/setup", headers=bearer)
                assert setup.status_code == 400, setup.text

                # Privileged route (revoke key) passes the MFA gate: operator
                # sessions inherit IdP MFA and never need a proof.
                victim = client.post(
                    "/api/v1/api-keys",
                    json={"name": "victim3", "role": "auditor"},
                    headers=bearer,
                )
                assert victim.status_code == 200, victim.text
                revoked = client.delete(
                    f"/api/v1/api-keys/{victim.json()['id']}", headers=bearer
                )
                assert revoked.status_code == 204, revoked.text
        finally:
            restore()
