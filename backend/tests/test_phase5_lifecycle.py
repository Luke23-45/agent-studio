"""
Phase 5 implementation tests (P5-9 audit chain, P5-10 lifecycle, P5-11
compliance, P5-12 residency).

Covers: the tamper-evident hash-chain audit trail, tenant onboarding
automation + delegated-admin scoping + tenant metrics, compliance disclosure
marking/incident hook, and residency pinning/region at onboarding.
"""

import asyncio
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    ConversationRepository,
    EndUserRepository,
    EvidenceRepository,
    SurfaceRepository,
    TenantRepository,
)
from backend.app.infrastructure.db.manager import DatabaseConfig, DatabaseManager
from backend.app.infrastructure.db.models import AuditEventModel, Base


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(
        DatabaseConfig(database_url=f"sqlite+aiosqlite:///{tmp_path.as_posix()}/p5.db")
    )

    async def _init():
        await manager.initialize()
        async with manager._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_init())
    yield manager
    asyncio.run(manager.close())


def _seed_tenant(db, **kwargs):
    async def _run():
        data = {
            "id": str(uuid4()),
            "slug": "p5-co",
            "name": "P5 Co",
            "allowed_topics": ["general"],
            "blocked_topics": [],
            "escalation_threshold": 0.7,
            "knowledge_allowlist": [],
            "default_provider": "openai",
            "default_model": "gpt-4",
            "features": {},
            "guardrail_config": {},
            "guardrail_thresholds": {},
        }
        data.update({k: v for k, v in kwargs.items() if v is not None})
        return await TenantRepository(db).create(data)

    return asyncio.run(_run())


class TestAuditHashChain:
    """P5-9: tamper-evident append-only audit trail."""

    def test_chain_links_and_verifies(self, db):
        repo = AuditRepository(db)
        events = []
        for i in range(4):
            events.append(
                asyncio.run(
                    repo.add(
                        action=f"test.action.{i}",
                        resource_type="tenant",
                        resource_id=str(i),
                        tenant_id="t1",
                        actor_type="system",
                        details={"n": i},
                    )
                )
            )
        assert events[0]["prev_hash"] is None
        for row in events:
            assert row["event_hash"]
        for prev, cur in zip(events, events[1:]):
            assert cur["prev_hash"] == prev["event_hash"]
        assert asyncio.run(repo.verify_chain()) == (True, None)
        assert asyncio.run(repo.verify_chain(tenant_id="t1")) == (True, None)

    def test_tampered_details_detected(self, db):
        repo = AuditRepository(db)
        first = asyncio.run(
            repo.add(action="a.1", resource_type="tenant", tenant_id="t1")
        )
        asyncio.run(repo.add(action="a.2", resource_type="tenant", tenant_id="t1"))

        async def _tamper():
            async with db.get_session() as session:
                row = await session.get(AuditEventModel, first["id"])
                row.details = {"tampered": True}
                await session.flush()

        asyncio.run(_tamper())
        ok, broken = asyncio.run(repo.verify_chain())
        assert ok is False
        assert broken == first["id"]

    def test_legacy_rows_do_not_break_chain(self, db):
        async def _insert_legacy():
            async with db.get_session() as session:
                session.add(
                    AuditEventModel(
                        id=str(uuid4()),
                        tenant_id="legacy",
                        actor_type="system",
                        action="legacy.action",
                        resource_type="tenant",
                        details={},
                        created_at=datetime.now(timezone.utc),
                    )
                )
                await session.flush()

        asyncio.run(_insert_legacy())
        asyncio.run(
            AuditRepository(db).add(
                action="modern.action", resource_type="tenant", tenant_id="legacy"
            )
        )
        assert asyncio.run(AuditRepository(db).verify_chain()) == (True, None)

    def test_immutable_repo_has_no_mutation_surface(self, db):
        repo = AuditRepository(db)
        assert not hasattr(repo, "update")
        assert not hasattr(repo, "delete")

    def test_retention_column_persists(self, db):
        tenant = _seed_tenant(db, retention_days=90)
        assert tenant["retention_days"] == 90


class TestOnboarding:
    """P5-10: onboarding checklist automation."""

    def _run(self, db, tenant_id):
        from backend.app.application.tenant_lifecycle import TenantOnboardingService

        return asyncio.run(TenantOnboardingService(db=db).run(tenant_id))

    def _checklist(self, db, tenant_id):
        from backend.app.application.tenant_lifecycle import TenantOnboardingService

        return asyncio.run(TenantOnboardingService(db=db).checklist(tenant_id))

    def test_onboard_provisions_all_steps(self, db):
        tenant = _seed_tenant(db, region="eu-west-1")
        receipt = self._run(db, tenant["id"])

        assert receipt["tenant_id"] == tenant["id"]
        assert receipt["steps"]["default_surface"]["status"] == "created"
        assert receipt["steps"]["vector_namespace"]["namespace"] == (
            f"tenant_{tenant['id']}"
        )
        assert receipt["steps"]["object_prefix"]["prefix"] == f"tenant/{tenant['id']}/"
        assert receipt["steps"]["archive_prefix"]["prefix"].endswith("eu-west-1/")
        assert receipt["steps"]["operator_key"]["status"] == "created"
        assert receipt["operator_key"]["shown_once"] is True
        assert receipt["operator_key"]["key"].startswith("nrv_live_")

        surface = asyncio.run(
            SurfaceRepository(db).get_default(tenant["id"])
        )
        assert surface is not None and surface["name"] == "default"
        assert surface["tool_allowlist"] == [] and surface["knowledge_allowlist"] == []

        keys = asyncio.run(ApiKeyRepository(db).list_all())
        scoped = [k for k in keys if k["tenant_id"] == tenant["id"]]
        assert len(scoped) == 1 and scoped[0]["role"] == "operator"

        actions = {
            e["action"]
            for e in asyncio.run(AuditRepository(db).list_events(tenant_id=tenant["id"]))
        }
        assert "tenant.onboarding.surface_created" in actions
        assert "tenant.onboarding.completed" in actions

    def test_onboard_is_idempotent(self, db):
        tenant = _seed_tenant(db)
        first = self._run(db, tenant["id"])
        first_key_id = first["steps"]["operator_key"]["key_id"]
        second = self._run(db, tenant["id"])

        assert second["steps"]["default_surface"]["status"] == "already_configured"
        assert second["steps"]["operator_key"]["status"] == "already_provisioned"
        assert second["steps"]["operator_key"]["key_id"] == first_key_id
        assert "operator_key" not in second

        keys = asyncio.run(ApiKeyRepository(db).list_all())
        assert len([k for k in keys if k["tenant_id"] == tenant["id"]]) == 1

    def test_checklist_reports_state(self, db):
        tenant = _seed_tenant(db)
        before = self._checklist(db, tenant["id"])
        assert before["steps"]["default_surface"]["status"] == "missing"

        self._run(db, tenant["id"])
        after = self._checklist(db, tenant["id"])
        assert after["steps"]["default_surface"]["status"] == "configured"
        assert after["steps"]["operator_key"]["status"] == "provisioned"
        assert after["region"] == "eu-west-1" or after["region"] is None

    def test_onboard_missing_tenant_raises(self, db):
        from backend.app.application.tenant_lifecycle import TenantOnboardingService

        with pytest.raises(ValueError):
            asyncio.run(TenantOnboardingService(db=db).checklist(str(uuid4())))


class TestComplianceHelpers:
    """P5-11: Art. 50 disclosure + retention posture helpers."""

    def test_default_disclosure(self):
        from backend.app.governance.compliance import (
            DEFAULT_DISCLOSURE_TEXT,
            bot_disclosure,
        )

        assert bot_disclosure() == DEFAULT_DISCLOSURE_TEXT
        assert "AI assistant" in DEFAULT_DISCLOSURE_TEXT

    def test_surface_override(self):
        from backend.app.governance.compliance import bot_disclosure

        assert (
            bot_disclosure({"bot_disclosure": "powered by Neryva AI"})
            == "powered by Neryva AI"
        )
        assert bot_disclosure({"disclosure": "X"}) == "X"
        assert bot_disclosure({"id": "s1"}) != "X"

    def test_mark_export_flag(self):
        from backend.app.governance.compliance import mark_export

        bundle = mark_export({"tenant": "x"})
        assert bundle["export_marked_ai_generated"] is True
        assert "tenant" in bundle and bundle["tenant"] == "x"

    def test_retention_days_default_and_override(self):
        from backend.app.governance.compliance import retention_days_for

        assert retention_days_for({}) == 30
        assert retention_days_for({"memory": {"expiry_days": 14}}) == 14

    def test_annex_iii_checklist_posture(self):
        """P5-11/D-7: Annex III items documented; re-verification standing."""
        from backend.app.governance.compliance import ComplianceService

        service = ComplianceService()
        checklist = service.checklist()["checklist"]

        assert checklist["deployer_documentation"] is True
        assert checklist["documented_adversarial_testing"] is True
        assert checklist["per_tenant_risk_assessment"] is False  # no artifact yet
        assert checklist["reverification_required_by"] == "2027-12-01"
        # Legal re-verification is a standing human action — never auto-cleared.
        assert "standing human action" in checklist["reverification_note"]

    def test_risk_assessment_artifact_flips_posture(self):
        from backend.app.governance.compliance import ComplianceService

        service = ComplianceService()
        record = service.record_risk_assessment(
            tenant_id="t1",
            artifact_url="s3://neryva/compliance/t1/assessment-v1.pdf",
            assessed_by="compliance-officer@neryva.example",
        )
        assert record["tenant_id"] == "t1"
        assert record["assessed_by"] == "compliance-officer@neryva.example"

        # Posture reflects the in-memory registry for that tenant only.
        assert service.checklist(tenant_id="t1")["checklist"]["per_tenant_risk_assessment"] is True
        assert service.checklist(tenant_id="t2")["checklist"]["per_tenant_risk_assessment"] is False

        # Durable-record path (audit rows passed by the route) flips it too.
        assert (
            service.checklist(
                tenant_id="t9",
                risk_assessment_records=[{"details": record}],
            )["checklist"]["per_tenant_risk_assessment"]
            is True
        )

    def test_resolve_region_validation(self):
        from backend.app.governance.residency import resolve_region

        assert resolve_region(None) == "eu-west-1"
        assert resolve_region("EU-WEST-1") == "eu-west-1"
        with pytest.raises(ValueError):
            resolve_region("mars-1")

    def test_archive_prefix_pins_region(self):
        from backend.app.governance.residency import archive_prefix

        assert archive_prefix("t1", "us-east-1") == "tenant/t1/archive/us-east-1/"
        assert archive_prefix("t1").endswith("/eu-west-1/")


class TestLifecycleServiceExportMarking:
    """P5-11 (exports marked) + P5-12 (archive prefix)."""

    def _tenant_with_data(self, db):
        tenant = _seed_tenant(db, region="us-west-2")
        conversation = asyncio.run(
            ConversationRepository(db).get_or_create(tenant["id"], "s-1")
        )
        asyncio.run(
            ConversationRepository(db).add_message(
                conversation["id"], "user", "hello", redacted_content="hello"
            )
        )
        asyncio.run(
            EvidenceRepository(db).add(
                {
                    "tenant_id": tenant["id"],
                    "conversation_id": conversation["id"],
                    "session_id": "s-1",
                    "direction": "input",
                    "decision": "ALLOW",
                    "allowed": True,
                    "input_hash": "a" * 64,
                    "violations": [],
                    "layers_evaluated": ["regex"],
                    "processing_time_ms": 1.0,
                    "metadata": {},
                }
            )
        )
        return tenant

    def test_export_marked_and_region_pinned(self, db):
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        tenant = self._tenant_with_data(db)
        bundle = asyncio.run(
            TenantLifecycleService(db=db).export_tenant_data(tenant["id"])
        )
        assert bundle["export_marked_ai_generated"] is True
        assert bundle["archive_prefix"] == f"tenant/{tenant['id']}/archive/us-west-2/"
        assert bundle["retention_days"] == 30

    def test_en_end_user_export_marked(self, db):
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        tenant = self._tenant_with_data(db)
        end_user = asyncio.run(
            EndUserRepository(db).create_authenticated(
                tenant["id"], external_id="c1", display_name="C1"
            )
        )
        bundle = asyncio.run(
            TenantLifecycleService(db=db).export_end_user_data(
                tenant["id"], end_user["id"]
            )
        )
        assert bundle["export_marked_ai_generated"] is True

    def test_offboard_retains_evidence_and_audit(self, db):
        from backend.app.application.tenant_lifecycle import TenantLifecycleService

        tenant = self._tenant_with_data(db)
        service = TenantLifecycleService(db=db)
        assert asyncio.run(service.offboard_tenant(tenant["id"])) is True

        assert asyncio.run(TenantRepository(db).get_by_id(tenant["id"])) is None
        evidence = asyncio.run(
            EvidenceRepository(db).list_by_tenant(tenant["id"], limit=100)
        )
        assert len(evidence) == 1  # retained per retention policy
        actions = {
            e["action"]
            for e in asyncio.run(AuditRepository(db).list_events(tenant_id=tenant["id"]))
        }
        assert "tenant.offboarded" in actions
        ok, _ = asyncio.run(AuditRepository(db).verify_chain())
        assert ok is True


class TestApiLifecycle:
    """API-level wiring for onboarding, metrics, compliance, residency,
    and delegated-admin scoping."""

    def _isolated_app(self, tmp_path):
        from starlette.testclient import TestClient

        import backend.app.api.routes.conversations as conversations_module
        from backend.app.main import app
        from backend.app.settings.env import settings

        original = {
            "auth": settings.AUTH_ENABLED,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "keys": [
                settings.OPENAI_API_KEY,
                settings.ANTHROPIC_API_KEY,
                settings.GOOGLE_API_KEY,
                settings.AZURE_API_KEY,
                settings.CUSTOM_LLM_API_KEY,
            ],
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]
            settings.OPENAI_API_KEY, settings.ANTHROPIC_API_KEY, settings.GOOGLE_API_KEY = (
                original["keys"][:3]
            )
            settings.AZURE_API_KEY, settings.CUSTOM_LLM_API_KEY = original["keys"][3:]
            conversations_module._rag_services.clear()
            app.dependency_overrides.clear()

        settings.AUTH_ENABLED = False
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/p5-api.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        for attr in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "AZURE_API_KEY",
            "CUSTOM_LLM_API_KEY",
        ):
            setattr(settings, attr, None)

        from backend.app.infrastructure.db import init_database
        from backend.app.infrastructure.db.models import Base

        async def _setup_schema():
            manager = init_database(settings.DATABASE_URL)
            await manager.initialize()
            async with manager._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            await manager.close()

        asyncio.run(_setup_schema())
        # Re-init reads the same global; the app lifespan re-initializes it on
        # startup, so we only need the URL override to take effect.
        return TestClient(app), restore

    def _provision_tenant(self, client, slug, **extra):
        body = {
            "slug": slug,
            "name": "P5 Co",
            "allowed_topics": ["general"],
            "blocked_topics": [],
            "escalation_threshold": 0.5,
        }
        body.update(extra)
        resp = client.post("/api/v1/tenants", json=body)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def test_onboarding_api_and_checklist(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant = self._provision_tenant(client, "p5onboard")
                onboard = client.post(f"/api/v1/tenants/{tenant['id']}/onboard")
                assert onboard.status_code == 200, onboard.text
                steps = onboard.json()["steps"]
                assert steps["default_surface"]["status"] == "created"
                assert steps["operator_key"]["status"] == "created"

                checklist = client.get(f"/api/v1/tenants/{tenant['id']}/onboarding")
                assert checklist.status_code == 200
                assert checklist.json()["steps"]["default_surface"]["status"] == "configured"

                surface = client.get(
                    f"/api/v1/tenants/{tenant['id']}/surfaces"
                )
                assert surface.status_code == 200
                assert len(surface.json()) == 1
        finally:
            restore()

    def test_tenant_metrics_endpoint(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant = self._provision_tenant(client, "p5metrics")
                resp = client.get(f"/api/v1/tenants/{tenant['id']}/metrics")
                assert resp.status_code == 200, resp.text
                body = resp.json()
                assert body["conversations"] == 0
                assert body["messages"] == 0
                assert "end_users" in body and "guardrail_evidence" in body
        finally:
            restore()

    def test_compliance_posture_and_incident(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant = self._provision_tenant(client, "p5comp")
                posture = client.get(f"/api/v1/tenants/{tenant['id']}/compliance")
                assert posture.status_code == 200, posture.text
                body = posture.json()
                assert "AI assistant" in body["disclosure"]["ai_disclosure"]
                assert body["posture"]["checklist"]["disclosure_art50"] is True

                incident = client.post(
                    f"/api/v1/tenants/{tenant['id']}/compliance/incidents",
                    json={"severity": "high", "description": "model produced PII"},
                )
                assert incident.status_code == 200, incident.text
                assert incident.json()["severity"] == "high"

                audit = client.get(
                    f"/api/v1/audit/events?tenant_id={tenant['id']}"
                )
                actions = {e["action"] for e in audit.json()}
                assert "compliance.incident_reported" in actions
        finally:
            restore()

    def test_region_pinned_and_deployment_shape(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                tenant = self._provision_tenant(
                    client, "p5region", region="ap-southeast-1"
                )
                assert tenant["region"] == "ap-southeast-1"

                shape = client.put(
                    f"/api/v1/tenants/{tenant['id']}/deployment-shape",
                    json={"dedicated": True},
                )
                assert shape.status_code == 200, shape.text
                assert shape.json()["shape"] == "dedicated"
                assert shape.json()["dedicated_deployment"]["database"]["dedicated"] is True

                gdpr = client.get(f"/api/v1/tenants/{tenant['id']}/gdpr/export")
                assert gdpr.status_code == 200, gdpr.text
                bundle = gdpr.json()
                assert bundle["export_marked_ai_generated"] is True
                assert bundle["archive_prefix"] == (
                    f"tenant/{tenant['id']}/archive/ap-southeast-1/"
                )
        finally:
            restore()

    def test_delegated_admin_scoping(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        from backend.app.api.dependencies.auth import (
            ApiKeyPrincipal,
            get_principal,
        )
        from backend.app.main import app

        try:
            with client:
                t1 = self._provision_tenant(client, "p5admin1")
                t2 = self._provision_tenant(client, "p5admin2")

                # Simulate a tenant_admin key bound to t1.
                def _as_tenant_admin():
                    return ApiKeyPrincipal(
                        key_id="k1",
                        name="delegated",
                        role="tenant_admin",
                        tenant_id=UUID(t1["id"]),
                        scopes=[],
                    )

                app.dependency_overrides[get_principal] = _as_tenant_admin

                ok = client.post(
                    "/api/v1/api-keys",
                    json={"name": "op1", "role": "operator"},
                )
                assert ok.status_code == 200, ok.text
                assert ok.json()["tenant_id"] == t1["id"]

                cross = client.post(
                    "/api/v1/api-keys",
                    json={"name": "x", "role": "operator", "tenant_id": t2["id"]},
                )
                assert cross.status_code == 403

                escalate = client.post(
                    "/api/v1/api-keys",
                    json={"name": "sa", "role": "super_admin", "tenant_id": t1["id"]},
                )
                assert escalate.status_code == 403

                listing = client.get("/api/v1/api-keys")
                assert listing.status_code == 200
                assert all(k["tenant_id"] == t1["id"] for k in listing.json())
        finally:
            restore()
