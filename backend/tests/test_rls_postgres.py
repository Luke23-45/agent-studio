"""
P5-6 live Postgres RLS tests (Arch §6.3.2).

Exercises real Postgres semantics SQLite cannot:

- a session pinned to tenant A (GUC ``app.tenant_id``) cannot see tenant B's
  rows even with a buggy, unfiltered query
- ``policy_rules`` is scoped through ``policy_sets`` (no tenant_id column)
- ``WITH CHECK`` blocks cross-tenant inserts at the database layer
- FORCE ROW LEVEL SECURITY stops the table owner from bypassing policies

Gated: the whole module skips when Postgres is unreachable
(``NERYVA_TEST_POSTGRES_URL``, default
``postgresql://postgres@localhost:5432/neryva_test``) or when ``asyncpg`` is
not installed. It never touches a shared schema: a random schema + role per
run are created and dropped in the fixture.
"""

import asyncio
import os
import uuid

import pytest

from backend.app.governance import rls

POSTGRES_URL = os.environ.get(
    "NERYVA_TEST_POSTGRES_URL",
    "postgresql://postgres@localhost:5432/neryva_test",
)


def _postgres_available() -> bool:
    """Probe the admin URL with a short timeout (pattern: test_hot_tier)."""
    try:
        import asyncpg
    except ImportError:
        return False
    try:
        conn = asyncio.run(asyncpg.connect(dsn=POSTGRES_URL, timeout=2))
        asyncio.run(conn.close())
        return True
    except Exception:
        return False


SCHEMA_TABLES = ("tenants", "policy_sets", "policy_rules", "messages")


class _RlsPostgresHarness:
    """Creates an isolated schema + app role, applies the migration DDL."""

    def __init__(self, admin_dsn: str):
        import asyncpg

        self.asyncpg = asyncpg
        self.admin_dsn = admin_dsn
        self.schema = f"rls_e2e_{uuid.uuid4().hex[:8]}"
        self.role = f"neryva_rls_app_{uuid.uuid4().hex[:8]}"
        self.password = "rls_test_password"
        parsed = asyncpg.parse_dsn(admin_dsn)
        self.admin_kwargs = {k: v for k, v in parsed.items() if v is not None}
        self.app_kwargs = {
            **self.admin_kwargs,
            "user": self.role,
            "password": self.password,
            "server_settings": {"search_path": self.schema},
        }
        self.database = parsed.get("database") or "postgres"

    def _run(self, coro):
        return asyncio.run(coro)

    def setup(self) -> None:
        self._run(self._setup())

    def teardown(self) -> None:
        self._run(self._teardown())

    async def _connect(self, kwargs=None):
        return await self.asyncpg.connect(**kwargs or self.admin_kwargs)

    async def _setup(self) -> None:
        conn = await self._connect()
        try:
            await conn.execute(f'CREATE SCHEMA "{self.schema}";')
            await conn.execute(
                f'CREATE ROLE "{self.role}" LOGIN PASSWORD \'{self.password}\';'
            )
            await conn.execute(
                f"GRANT CONNECT ON DATABASE {self.database} TO \"{self.role}\";"
            )
            await conn.execute(
                f'GRANT USAGE ON SCHEMA "{self.schema}" TO "{self.role}";'
            )
            for ddl in (
                f'CREATE TABLE "{self.schema}".tenants (id text PRIMARY KEY, name text NOT NULL);',
                f'CREATE TABLE "{self.schema}".policy_sets (id text PRIMARY KEY, tenant_id text NOT NULL);',
                f'CREATE TABLE "{self.schema}".policy_rules (id text PRIMARY KEY, policy_set_id text NOT NULL, name text NOT NULL);',
                f'CREATE TABLE "{self.schema}".messages (id text PRIMARY KEY, tenant_id text NOT NULL, content text NOT NULL);',
                f'CREATE TABLE "{self.schema}".owned (id text PRIMARY KEY, tenant_id text NOT NULL, content text NOT NULL) OWNER "{self.role}";',
            ):
                await conn.execute(ddl)
            await conn.execute(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{self.schema}" TO "{self.role}";'
            )
            # Same DDL the migration 0006 emits, plus FORCE on the owner table.
            for table in SCHEMA_TABLES + ("owned",):
                await conn.execute(rls.rls_enable_statement(table))
                for stmt in rls.rls_policy_statements(table):
                    await conn.execute(stmt)
            await conn.execute(rls.rls_enforce_statement("owned"))
            # Seed data (superuser bypasses RLS).
            await conn.execute(
                "INSERT INTO tenants (id, name) VALUES ($1, $2), ($3, $4)",
                "tenant-a", "Alpha", "tenant-b", "Beta",
            )
            await conn.execute(
                "INSERT INTO policy_sets (id, tenant_id) VALUES ($1, $2), ($3, $4)",
                "ps-a", "tenant-a", "ps-b", "tenant-b",
            )
            await conn.execute(
                "INSERT INTO policy_rules (id, policy_set_id, name) VALUES ($1, $2, $3), ($4, $5, $6)",
                "rule-a", "ps-a", "alpha rule", "rule-b", "ps-b", "beta rule",
            )
            await conn.execute(
                "INSERT INTO messages (id, tenant_id, content) VALUES ($1, $2, $3), ($4, $2, $5), ($6, $7, $8)",
                "msg-a1", "tenant-a", "alpha one", "msg-a2", "alpha two",
                "msg-b1", "tenant-b", "beta one",
            )
            await conn.execute(
                "INSERT INTO owned (id, tenant_id, content) VALUES ($1, $2, $3), ($4, $5, $6)",
                "own-a", "tenant-a", "owned alpha", "own-b", "tenant-b", "owned beta",
            )
        finally:
            await conn.close()

    async def _teardown(self) -> None:
        conn = await self._connect()
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE;')
            await conn.execute(f'DROP ROLE IF EXISTS "{self.role}";')
        finally:
            await conn.close()

    # -- query helpers ----------------------------------------------------

    async def _as(self, role_kwargs, tenant_id: str | None, query: str, *args):
        conn = await self._connect(role_kwargs)
        in_tx = False
        try:
            if tenant_id is not None:
                in_tx = True
                await conn.execute("BEGIN")
                await conn.execute(
                    "SELECT set_config($1, $2, true)", rls.TENANT_GUC, tenant_id
                )
            rows = await conn.fetch(query, *args)
            if in_tx:
                await conn.execute("COMMIT")
                in_tx = False
            return [dict(row) for row in rows]
        finally:
            if in_tx:
                await conn.execute("ROLLBACK")
            await conn.close()

    def app_query(self, tenant_id: str | None, query: str, *args):
        return self._run(self._as(self.app_kwargs, tenant_id, query, *args))

    def admin_query(self, query: str, *args):
        return self._run(self._as(self.admin_kwargs, None, query, *args))


@pytest.fixture(scope="module")
def rls_pg():
    if not _postgres_available():
        pytest.skip(
            "Postgres unreachable (NERYVA_TEST_POSTGRES_URL); skipping live RLS suite"
        )
    harness = _RlsPostgresHarness(POSTGRES_URL)
    harness.setup()
    yield harness
    harness.teardown()


def _ids(rows):
    return {row["id"] for row in rows}


class TestRlsLiveIsolation:
    def test_cross_tenant_scan_blocked(self, rls_pg):
        as_a = rls_pg.app_query("tenant-a", "SELECT id FROM messages")
        assert _ids(as_a) == {"msg-a1", "msg-a2"}
        as_b = rls_pg.app_query("tenant-b", "SELECT id FROM messages")
        assert _ids(as_b) == {"msg-b1"}

    def test_guc_unset_is_permissive(self, rls_pg):
        rows = rls_pg.app_query(None, "SELECT id FROM messages")
        assert _ids(rows) == {"msg-a1", "msg-a2", "msg-b1"}

    def test_tenants_scoped_by_id(self, rls_pg):
        assert _ids(rls_pg.app_query("tenant-a", "SELECT id FROM tenants")) == {"tenant-a"}
        assert _ids(rls_pg.app_query("tenant-b", "SELECT id FROM tenants")) == {"tenant-b"}

    def test_policy_rules_scoped_through_policy_sets(self, rls_pg):
        assert _ids(rls_pg.app_query("tenant-a", "SELECT id FROM policy_rules")) == {"rule-a"}
        assert _ids(rls_pg.app_query("tenant-b", "SELECT id FROM policy_rules")) == {"rule-b"}
        assert _ids(rls_pg.app_query(None, "SELECT id FROM policy_rules")) == {"rule-a", "rule-b"}

    def test_with_check_blocks_cross_tenant_insert(self, rls_pg):
        with pytest.raises(Exception) as exc:
            rls_pg.app_query(
                "tenant-a",
                "INSERT INTO messages (id, tenant_id, content) VALUES ($1, $2, $3)",
                "msg-x", "tenant-b", "sneaky",
            )
        assert "new row violates row-level security policy" in str(exc.value)
        rows = rls_pg.admin_query("SELECT id FROM messages WHERE id = 'msg-x'")
        assert rows == []

    def test_force_rls_applies_to_owner(self, rls_pg):
        # ``owned`` is owned by the app role and FORCE RLS is set — even the
        # owner cannot bypass the tenant policy (P5-6 defense-in-depth).
        as_a = rls_pg.app_query("tenant-a", "SELECT id FROM owned")
        assert _ids(as_a) == {"own-a"}
        as_b = rls_pg.app_query("tenant-b", "SELECT id FROM owned")
        assert _ids(as_b) == {"own-b"}
        rows = rls_pg.app_query(None, "SELECT id FROM owned")
        assert _ids(rows) == {"own-a", "own-b"}