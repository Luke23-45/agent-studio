"""
Tests for API key generation, hashing, and RBAC permissions.
"""

from backend.app.api.dependencies.auth import (
    KEY_PREFIX,
    ApiKeyPrincipal,
    generate_api_key,
    hash_api_key,
    ROLE_PERMISSIONS,
)


class TestApiKeyCrypto:
    def test_generate_api_key_format(self):
        raw, prefix, key_hash = generate_api_key()
        assert raw.startswith(KEY_PREFIX)
        assert prefix.startswith(KEY_PREFIX)
        assert len(prefix) == len(KEY_PREFIX) + 8
        assert len(key_hash) == 64  # sha256 hex

    def test_hash_is_stable_and_not_plaintext(self):
        raw = "nrv_live_test_secret_value"
        assert hash_api_key(raw) == hash_api_key(raw)
        assert raw not in hash_api_key(raw)

    def test_generated_key_matches_hash(self):
        raw, _, key_hash = generate_api_key()
        assert hash_api_key(raw) == key_hash


class TestRbac:
    def test_super_admin_has_all_permissions(self):
        principal = ApiKeyPrincipal(
            key_id="k1", name="admin", role="super_admin", tenant_id=None, scopes=[]
        )
        for permission in (
            "tenants:read", "tenants:write", "api_keys:manage", "audit:read",
        ):
            assert principal.has_permission(permission)

    def test_auditor_cannot_write_tenants(self):
        principal = ApiKeyPrincipal(
            key_id="k2", name="audit", role="auditor", tenant_id=None, scopes=[]
        )
        assert principal.has_permission("tenants:read")
        assert not principal.has_permission("tenants:write")

    def test_wildcard_scope_bypasses_role(self):
        principal = ApiKeyPrincipal(
            key_id="k3", name="dev", role="operator", tenant_id=None, scopes=["*"]
        )
        assert principal.has_permission("api_keys:manage")

    def test_roles_cover_expected_permissions(self):
        assert "tenants:write" in ROLE_PERMISSIONS["super_admin"]
        assert "audit:read" in ROLE_PERMISSIONS["auditor"]
