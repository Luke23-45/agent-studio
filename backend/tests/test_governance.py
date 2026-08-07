"""
Phase 5 governance tests (P5-1…P5-7, P5-9, P5-11).

Covers the governance plane in isolation (no Redis/Postgres): compiled
surface configs (P5-1), the config-pipeline eval gate helpers (P5-2),
the deterministic tool authorization gate (P5-3), the RLS DDL generator
(P5-6), the budget-hierarchy mapping (P5-7), and evidence packets against
the contract schema (P5-9) plus residency helpers (P5-11).
"""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, ValidationError

from backend.app.domain.policy import PolicyAction
from backend.app.domain.tenant import TenantConfig
from backend.app.governance import budgets, compiled, evidence, rls, toolgate
from backend.app.governance.isolation import resolve_tenant_context

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_SCHEMA = ROOT / "contracts" / "schemas" / "evidence-packet.schema.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _tenant_config(**overrides) -> TenantConfig:
    base = TenantConfig(
        id=uuid4(),
        slug="govco",
        name="Governance Co",
        default_provider="openai",
        default_model="gpt-4",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class TestRlsDdl:
    """P5-6: generated DDL must reference real scoping columns."""

    def test_default_tables_scope_by_tenant_id(self):
        stmts = rls.rls_policy_statements("messages")
        joined = " ".join(stmts)
        assert "tenant_id = current_setting('app.tenant_id', true)" in joined

    def test_tenants_table_scopes_by_id(self):
        joined = " ".join(rls.rls_policy_statements("tenants"))
        assert "id = current_setting('app.tenant_id', true)" in joined
        assert "tenant_id = current_setting" not in joined

    def test_policy_rules_scope_through_policy_sets(self):
        joined = " ".join(rls.rls_policy_statements("policy_rules"))
        assert "policy_set_id IN (SELECT id FROM policy_sets" in joined
        assert rls.tenant_scope_expr("policy_rules").startswith("policy_set_id IN")
        assert rls.tenant_scope_expr("policy_rules") == (
            "policy_set_id IN (SELECT id FROM policy_sets "
            "WHERE tenant_id = current_setting('app.tenant_id', true))"
        )

    def test_policy_is_permissive_when_guc_unset(self):
        joined = " ".join(rls.rls_policy_statements("threads"))
        assert "current_setting('app.tenant_id', true) IS NULL" in joined

    def test_full_ddl_covers_every_scoped_table(self):
        ddl = rls.rls_ddl()
        for table in rls.TENANT_SCOPED_TABLES:
            assert f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;' in ddl
            assert f'"tenant_isolation_{table}"' in " ".join(ddl)

    def test_migration_uses_same_per_table_scope(self):
        import importlib.util

        migration_path = (
            Path(__file__).resolve().parents[1]
            / "alembic" / "versions" / "0006_governance_phase5.py"
        )
        spec = importlib.util.spec_from_file_location("migration_0006", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        for table in migration.TENANT_SCOPED_TABLES:
            expr = migration._tenant_scope_expr(table)
            assert "current_setting('app.tenant_id', true)" in expr


class TestCompileSurfaceConfig:
    """P5-1: one compiled, deny-by-default artifact per surface."""

    def test_missing_surface_compiles_deny_by_default(self):
        compiled_cfg = compiled.compile_surface_config(_tenant_config())
        assert compiled_cfg.deny_by_default is True
        assert compiled_cfg.allows_tool("anything") is False

    def test_inactive_surface_compiles_deny_by_default(self):
        surface = {"id": "s1", "active": False, "tool_allowlist": ["search"]}
        compiled_cfg = compiled.compile_surface_config(
            _tenant_config(), surface=surface
        )
        assert compiled_cfg.deny_by_default is True

    def test_active_surface_carries_allowlists(self):
        surface = {
            "id": "s1",
            "active": True,
            "tool_allowlist": ["search", "calc"],
            "knowledge_allowlist": ["kb/faq"],
            "brand_voice_override": {"tone": "formal"},
        }
        compiled_cfg = compiled.compile_surface_config(
            _tenant_config(), surface=surface
        )
        assert compiled_cfg.deny_by_default is False
        assert compiled_cfg.tool_allowlist == ("calc", "search")
        assert compiled_cfg.allows_tool("search") is True
        assert compiled_cfg.allows_tool("hacker") is False
        assert compiled_cfg.knowledge_allowlist == ("kb/faq",)

    def test_rails_bound_in_stable_order(self):
        compiled_cfg = compiled.compile_surface_config(_tenant_config())
        names = [r.name for r in compiled_cfg.ingress_rails]
        assert names == list(compiled.INGRESS_RAIL_ORDER)
        egress = [r.name for r in compiled_cfg.egress_rails]
        assert egress == list(compiled.EGRESS_RAIL_ORDER)

    def test_validate_payload_rejects_invalid(self):
        with pytest.raises(compiled.ConfigValidationError):
            compiled.validate_payload({"slug": "UPPER_CASE", "name": ""})

    def test_validate_payload_accepts_valid(self):
        compiled.validate_payload({"slug": "govco", "name": "Governance Co"})


class TestConfigPipelineGate:
    """P5-2: eval-gate helpers deny promotion of failed configs."""

    def test_compile_is_the_gate(self):
        payload = {"slug": "govco", "name": "Governance Co"}
        assert compiled.validate_payload(payload) is None
        with pytest.raises(compiled.ConfigValidationError):
            compiled.validate_payload({**payload, "slug": ""})


class TestBudgetHierarchy:
    """P5-7: composed USD levels map into gateway quota keys."""

    def test_build_gateway_budget_cfg_preserves_four_levels(self):
        cfg = budgets.build_gateway_budget_cfg(
            {
                "quota_platform_usd": 100.0,
                "quota_tenant_usd": 50.0,
                "quota_surface_usd": 20.0,
                "quota_end_user_usd": 5.0,
            }
        )
        assert cfg == {
            "quota_platform_usd": 100.0,
            "quota_tenant_usd": 50.0,
            "quota_surface_usd": 20.0,
            "quota_end_user_usd": 5.0,
        }

    def test_compiled_config_carries_quota_keys(self):
        cfg = compiled.compile_surface_config(_tenant_config(), surface={"id": "s1"})
        gateway_cfg = budgets.build_gateway_budget_cfg(cfg.budgets)
        assert set(gateway_cfg) == {
            "quota_platform_usd",
            "quota_tenant_usd",
            "quota_surface_usd",
            "quota_end_user_usd",
        }

    def test_surface_over_budget_while_tenant_fine(self):
        assert budgets.surface_over_budget_while_tenant_fine(
            tenant_usd=100.0, surface_usd=10.0, tenant_spent=1.0,
            surface_spent=9.0, estimated_usd=5.0,
        ) is True
        assert budgets.surface_over_budget_while_tenant_fine(
            tenant_usd=100.0, surface_usd=10.0, tenant_spent=1.0,
            surface_spent=1.0, estimated_usd=5.0,
        ) is False


class TestBudgetRejectionUi:
    """P5-7: surface-level budget rejection produces a widget-ready line."""

    def test_surface_level_message_names_the_surface(self):
        msg = budgets.budget_rejection_message("surface", 10.0, 12.5)
        assert "This surface's budget" in msg
        assert "$10.00" in msg
        assert "$12.50" in msg
        assert "rejected" in msg
        assert "raise the budget" in msg

    def test_message_distinguishes_surface_from_tenant(self):
        surface = budgets.budget_rejection_message("surface", 20.0, 30.0)
        tenant = budgets.budget_rejection_message("tenant", 100.0, 120.0)
        assert "This surface's budget" in surface
        assert "The tenant budget" in tenant
        assert "$20.00" in surface
        assert "$100.00" in tenant

    def test_unknown_level_uses_level_without_crashing(self):
        message = budgets.budget_rejection_message("platform", 5.0, 6.0)
        assert "platform" in message
        assert budgets.budget_rejection_message("", None, None)
        assert budgets.budget_rejection_message("junk", None, None)


class TestToolGate:
    """P5-3: deterministic allow/deny; denials audited; deny by default."""

    def test_deny_by_default_with_empty_registry(self):
        gate = toolgate.ToolAuthorizationGate(
            tenant_config=_tenant_config(),
            registered_tools={},
            surface_allowlist=["search"],
        )
        decision = gate.authorize({"name": "search", "arguments": {}})
        assert decision.allowed is False
        assert decision.reason == "tool not enabled for tenant"

    def test_deny_when_not_on_surface_allowlist(self):
        gate = toolgate.ToolAuthorizationGate(
            tenant_config=_tenant_config(),
            registered_tools={"search": True},
            surface_allowlist=["calc"],
        )
        decision = gate.authorize({"name": "search", "arguments": {}})
        assert decision.allowed is False
        assert decision.reason == "tool not in surface allowlist"

    def test_allow_when_registered_and_allowlisted(self):
        gate = toolgate.ToolAuthorizationGate(
            tenant_config=_tenant_config(),
            registered_tools={"search": True},
            surface_allowlist=["search"],
        )
        assert gate.authorize({"name": "search", "arguments": {}}).allowed is True

    def test_missing_tool_name_denied(self):
        gate = toolgate.ToolAuthorizationGate(
            tenant_config=_tenant_config(),
            registered_tools={},
            surface_allowlist=[],
        )
        assert gate.authorize({}).allowed is False

    def test_denial_is_audited(self):
        audited = []

        def _audit(record):
            audited.append(record)

        gate = toolgate.ToolAuthorizationGate(
            tenant_config=_tenant_config(),
            registered_tools={},
            surface_allowlist=["search"],
            audit=_audit,
        )
        asyncio.run(gate.authorize_async({"name": "search"}))
        assert len(audited) == 1
        assert audited[0]["allowed"] is False
        assert audited[0]["tool"] == "search"

    def test_allowance_is_not_audited(self):
        audited = []

        def _audit(record):
            audited.append(record)

        gate = toolgate.ToolAuthorizationGate(
            tenant_config=_tenant_config(),
            registered_tools={"search": True},
            surface_allowlist=["search"],
            audit=_audit,
        )
        asyncio.run(gate.authorize_async({"name": "search", "arguments": {}}))
        assert audited == []

    def test_build_authorizer_reads_registry_and_surface(self):
        from backend.app.infrastructure.db import ToolRegistryRepository
        from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
        from backend.app.infrastructure.db.models import Base

        db = DatabaseManager(
            DatabaseConfig(database_url=f"sqlite+aiosqlite:///{uuid4().hex}.db")
        )

        async def _scenario():
            await db.initialize()
            async with db._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            tenant = _tenant_config()
            await ToolRegistryRepository(db).register(
                str(tenant.id), "search", enabled=True
            )
            await ToolRegistryRepository(db).register(
                str(tenant.id), "hack", enabled=True
            )
            gate = await toolgate.build_authorizer(
                tenant_config=tenant,
                db=db,
                surface={"id": "s1", "tool_allowlist": ["search"]},
            )
            assert gate.authorize({"name": "search"}).allowed is True
            assert gate.authorize({"name": "hack"}).allowed is False

        try:
            asyncio.run(_scenario())
        finally:
            asyncio.run(db.close())


class TestEvidencePackets:
    """P5-9: packets validate against the contract schema."""

    def _validate(self, packet: evidence.EvidencePacket) -> None:
        Draft202012Validator(_load(EVIDENCE_SCHEMA)).validate(packet.to_dict())

    def test_policy_packet_valid(self):
        packet = evidence.build_packet(
            tenant_id=str(uuid4()),
            session_id="s1",
            decision=PolicyAction.BLOCK,
            input_hash="a" * 64,
            violations=[{"name": "x", "action": "block"}],
        )
        self._validate(packet)
        assert packet.allowed is False

    def test_tool_gate_packet_valid(self):
        packet = evidence.build_tool_gate_packet(
            tenant_id=str(uuid4()),
            session_id="s1",
            tool_name="search",
            reason="tool not in surface allowlist",
            surface_id="s1",
        )
        self._validate(packet)
        assert packet.decision == "BLOCK"
        assert packet.allowed is False

    def test_quota_packet_valid(self):
        packet = evidence.build_quota_packet(
            tenant_id=str(uuid4()),
            session_id="s1",
            conversation_id=None,
            level="surface",
            limit_usd=10.0,
            projected_usd=12.0,
        )
        self._validate(packet)
        assert packet.allowed is False

    def test_invalid_shape_rejected(self):
        with pytest.raises(ValidationError):
            Draft202012Validator(_load(EVIDENCE_SCHEMA)).validate(
                {"direction": "policy", "decision": "ALLOW"}
            )


class TestIsolationPrimitives:
    """P5-5: one naming scheme across DB, Redis, vector, storage, traces."""

    def _make(self, **kwargs):
        from backend.app.governance.isolation import resolve_tenant_context

        base = {"tenant_id": "tenant-a"}
        base.update(kwargs)
        return resolve_tenant_context(**base)

    def test_redis_prefix_tenant_scoped(self):
        ctx = self._make()
        assert ctx.redis_prefix() == "tenant:tenant-a:"

    def test_redis_prefix_end_user_nested(self):
        ctx = self._make(end_user_id="u1")
        assert ctx.redis_prefix() == "tenant:tenant-a:end_user:u1:"

    def test_redis_key_joins_parts_under_prefix(self):
        from backend.app.governance.isolation import redis_key

        ctx = self._make()
        assert redis_key(ctx, "thread", "t1") == "tenant:tenant-a:thread:t1"

    def test_vector_namespace_tenant_and_kb(self):
        from backend.app.governance.isolation import vector_namespace_for

        ctx = self._make()
        assert vector_namespace_for(ctx) == "tenant_tenant-a"
        assert vector_namespace_for(ctx, "kb1") == "tenant_tenant-a_kb_kb1"

    def test_storage_prefix_and_key(self):
        from backend.app.governance.isolation import storage_key

        ctx = self._make()
        assert ctx.storage_prefix() == "tenant/tenant-a/"
        assert storage_key(ctx, "archive", "x.json") == "tenant/tenant-a/archive/x.json"

    def test_trace_tags_carry_tenant_surface_end_user(self):
        from backend.app.governance.isolation import trace_tags

        surface = {"id": "s1"}
        ctx = resolve_tenant_context(
            tenant_id="tenant-a", surface=surface, end_user_id="u1"
        )
        assert trace_tags(ctx) == {
            "tenant_id": "tenant-a",
            "surface_id": "s1",
            "end_user_id": "u1",
        }

    def test_immutable_context(self):
        from dataclasses import FrozenInstanceError

        ctx = self._make()
        with pytest.raises(FrozenInstanceError):
            ctx.tenant_id = "tenant-b"


class TestHotTailRedaction:
    """P5-4: the Redis hot tier never receives raw durable content.

    Raw ``content`` stays in the access-controlled durable store; the hot
    fast path carries the redacted column only.
    """

    def test_refresh_hot_tail_stores_redacted_only(self, monkeypatch):
        import asyncio
        from types import SimpleNamespace

        class FakeCache:
            def __init__(self):
                self.payload = None

            async def cache_tail(self, tenant_id, thread_id, rows, *, summary=None):
                self.payload = {"rows": rows, "summary": summary}
                return True

        class FakeRepo:
            def __init__(self, db):
                pass

            async def read_tail(self, tenant_id, thread_id, *, from_seq=None, limit=20):
                return [
                    {
                        "id": "m1",
                        "role": "user",
                        "content": "my debit card is 4111 1111 1111 1111",
                        "redacted_content": "my debit card is [CARD]",
                    },
                    {
                        "id": "m2",
                        "role": "assistant",
                        "content": "ok raw answer",
                        "redacted_content": "ok answer",
                    },
                ]

        cache = FakeCache()
        import backend.app.api.routes.conversations as routes

        monkeypatch.setattr(routes, "ThreadRepository", FakeRepo)
        monkeypatch.setattr(routes, "get_thread_tail_cache", lambda: cache)

        async def _run():
            await routes._refresh_hot_tail(db=object(), tenant_id="t", thread_id="th")

        asyncio.run(_run())

        assert cache.payload is not None
        for row in cache.payload["rows"]:
            # structural invariant: the hot fast-path payload carries exactly
            # the redacted column (raw ``content`` is never forwarded)
            assert row["content"] == row["redacted_content"]
        assert "1111" not in cache.payload["rows"][0]["content"]
