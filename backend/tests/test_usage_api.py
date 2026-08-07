"""
P7-4 — Usage & billing API over the spend ledger (Arch 10, P3-5).

HTTP acceptance on an isolated app:

- super_admin sees platform totals, per-tenant breakdowns, per-tenant
  per-model/surface/day aggregates, quota windows and event feed
- tenant-bound principals are scoped to their own tenant (summary totals,
  quota, events) and get 403 on other tenants' data
- auditors lack billing:read and are rejected on all usage endpoints
"""

import asyncio
from datetime import datetime, timezone

import pytest

from backend.app.api.dependencies.auth import generate_api_key, hash_api_key
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    QuotaStateRepository,
    SpendEventRepository,
    get_database_manager,
)
from backend.app.infrastructure.db.models import QuotaStateModel
from backend.app.settings.env import settings


class TestUsageApi:
    def _isolated_app(self, tmp_path):
        original = {
            "auth": settings.AUTH_ENABLED,
            "db_url": settings.DATABASE_URL,
            "cfg_path": settings.TENANT_CONFIG_PATH,
            "queue_url": settings.REDIS_URL,
        }

        def restore():
            settings.AUTH_ENABLED = original["auth"]
            settings.DATABASE_URL = original["db_url"]
            settings.TENANT_CONFIG_PATH = original["cfg_path"]
            settings.REDIS_URL = original["queue_url"]

        settings.AUTH_ENABLED = True
        settings.DATABASE_URL = f"sqlite+aiosqlite:///{tmp_path.as_posix()}/usage.db"
        settings.TENANT_CONFIG_PATH = str(tmp_path / "tenant_configs")
        settings.REDIS_URL = "redis://127.0.0.1:1"  # unreachable -> in-memory fallback

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

    def _create_key(self, role: str, tenant_id=None) -> str:
        db = get_database_manager()
        raw, prefix, key_hash = generate_api_key()
        asyncio.run(
            ApiKeyRepository(db).create(
                name=f"usage-{role}",
                key_hash=key_hash,
                prefix=prefix,
                role=role,
                tenant_id=tenant_id,
            )
        )
        return raw

    def _seed(self):
        db = get_database_manager()
        repo = SpendEventRepository(db)
        asyncio.run(
            repo.add(
                {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "surface_id": "chat",
                    "provider": "openai",
                    "model": "gpt-4",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cached_tokens": 10,
                    "usd": 0.5,
                    "created_at": datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc),
                }
            )
        )
        asyncio.run(
            repo.add(
                {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "surface_id": "chat",
                    "provider": "openai",
                    "model": "gpt-4",
                    "input_tokens": 200,
                    "output_tokens": 100,
                    "cached_tokens": 0,
                    "usd": 1.0,
                    "created_at": datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
                }
            )
        )
        asyncio.run(
            repo.add(
                {
                    "tenant_id": "11111111-1111-1111-1111-111111111111",
                    "surface_id": "widget",
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet",
                    "input_tokens": 300,
                    "output_tokens": 150,
                    "cached_tokens": 40,
                    "usd": 2.0,
                    "created_at": datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
                }
            )
        )
        asyncio.run(
            repo.add(
                {
                    "tenant_id": "22222222-2222-2222-2222-222222222222",
                    "surface_id": "chat",
                    "provider": "openai",
                    "model": "gpt-4o",
                    "input_tokens": 50,
                    "output_tokens": 25,
                    "cached_tokens": 5,
                    "usd": 0.25,
                    "created_at": datetime(2026, 7, 3, 9, 0, tzinfo=timezone.utc),
                }
            )
        )

        async def _seed_windows():
            await QuotaStateRepository(db).record_spend(
                scope_type="tenant",
                tenant_id="11111111-1111-1111-1111-111111111111",
                surface_id=None,
                end_user_id=None,
                window_started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                spent_usd=1.5,
            )
            await QuotaStateRepository(db).record_spend(
                scope_type="surface",
                tenant_id="11111111-1111-1111-1111-111111111111",
                surface_id="widget",
                end_user_id=None,
                window_started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                spent_usd=2.0,
            )
            async with db.get_session() as session:
                session.add(
                    QuotaStateModel(
                        id="qw-1",
                        scope_type="tenant",
                        tenant_id="11111111-1111-1111-1111-111111111111",
                        surface_id=None,
                        end_user_id=None,
                        window_started_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
                        reserved_usd=10.0,
                        spent_usd=0.0,
                        limit_usd=50.0,
                    )
                )
            await QuotaStateRepository(db).record_spend(
                scope_type="tenant",
                tenant_id="22222222-2222-2222-2222-222222222222",
                surface_id=None,
                end_user_id=None,
                window_started_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
                spent_usd=0.25,
            )

        asyncio.run(_seed_windows())

    def test_platform_summary_and_tenant_detail(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key("super_admin")
                headers = {"X-API-Key": key}
                self._seed()

                summary = client.get("/api/v1/usage/summary", headers=headers)
                assert summary.status_code == 200, summary.text
                body = summary.json()
                assert body["totals"]["calls"] == 4
                assert body["totals"]["usd"] == pytest.approx(3.75, abs=1e-6)
                assert body["totals"]["inputTokens"] == 650
                assert body["totals"]["outputTokens"] == 325
                assert body["totals"]["cachedTokens"] == 55
                assert {p["tenantId"] for p in body["perTenant"]} == {"11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"}
                by_tenant = {p["tenantId"]: p for p in body["perTenant"]}
                assert by_tenant["11111111-1111-1111-1111-111111111111"]["calls"] == 3
                assert by_tenant["22222222-2222-2222-2222-222222222222"]["calls"] == 1
                assert body["since"] is None and body["until"] is None

                detail = client.get("/api/v1/usage/tenants/11111111-1111-1111-1111-111111111111", headers=headers)
                assert detail.status_code == 200, detail.text
                detail_body = detail.json()
                assert detail_body["totals"]["calls"] == 3
                assert detail_body["totals"]["usd"] == pytest.approx(3.5, abs=1e-6)
                assert {m["key"] for m in detail_body["perModel"]} == {
                    "gpt-4",
                    "claude-3-5-sonnet",
                }
                assert {s["key"] for s in detail_body["perSurface"]} == {"chat", "widget"}
                assert {d["key"] for d in detail_body["perDay"]} == {
                    "2026-07-01",
                    "2026-07-02",
                }
        finally:
            restore()

    def test_usage_since_filter_and_quota_and_events(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key("super_admin")
                headers = {"X-API-Key": key}
                self._seed()

                filtered = client.get(
                    "/api/v1/usage/summary",
                    params={"since": "2026-07-02T00:00:00Z"},
                    headers=headers,
                )
                assert filtered.status_code == 200, filtered.text
                assert filtered.json()["totals"]["calls"] == 2

                quota = client.get("/api/v1/usage/quota", headers=headers)
                assert quota.status_code == 200, quota.text
                windows = quota.json()["windows"]
                assert len(windows) == 4
                limited = [w for w in windows if w["limitUsd"] is not None]
                assert len(limited) == 1
                limited = limited[0]
                assert limited["tenantId"] == "11111111-1111-1111-1111-111111111111"
                assert limited["scopeType"] == "tenant"
                assert limited["limitUsd"] == pytest.approx(50.0, abs=1e-6)
                assert limited["spentUsd"] == pytest.approx(0.0, abs=1e-6)
                assert limited["reservedUsd"] == pytest.approx(10.0, abs=1e-6)
                tenant_window = next(
                    w
                    for w in windows
                    if w["tenantId"] == "11111111-1111-1111-1111-111111111111"
                    and w["scopeType"] == "tenant"
                    and w["windowStartedAt"].startswith("2026-07-01")
                )
                assert tenant_window["spentUsd"] == pytest.approx(1.5, abs=1e-6)
                assert tenant_window["limitUsd"] is None

                events = client.get(
                    "/api/v1/usage/events", params={"limit": 10}, headers=headers
                )
                assert events.status_code == 200, events.text
                rows = events.json()["events"]
                assert len(rows) == 4
                assert rows[0]["tenantId"] == "22222222-2222-2222-2222-222222222222"  # newest first
                assert rows[0]["usd"] == pytest.approx(0.25, abs=1e-6)
                assert len({e["id"] for e in rows}) == 4
        finally:
            restore()

    def test_tenant_bound_principal_is_scoped(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key("tenant_admin", tenant_id="11111111-1111-1111-1111-111111111111")
                headers = {"X-API-Key": key}
                self._seed()

                summary = client.get("/api/v1/usage/summary", headers=headers)
                assert summary.status_code == 200, summary.text
                body = summary.json()
                assert body["totals"]["calls"] == 3
                assert body["perTenant"] == []

                denied = client.get("/api/v1/usage/tenants/22222222-2222-2222-2222-222222222222", headers=headers)
                assert denied.status_code == 403, denied.text

                quota = client.get("/api/v1/usage/quota", headers=headers)
                assert quota.status_code == 200, quota.text
                assert {w["tenantId"] for w in quota.json()["windows"]} == {"11111111-1111-1111-1111-111111111111"}

                events = client.get("/api/v1/usage/events", headers=headers)
                assert events.status_code == 200, events.text
                assert {e["tenantId"] for e in events.json()["events"]} == {"11111111-1111-1111-1111-111111111111"}
        finally:
            restore()

    def test_auditor_rejected_from_usage(self, tmp_path):
        client, restore = self._isolated_app(tmp_path)
        try:
            with client:
                key = self._create_key("auditor")
                headers = {"X-API-Key": key}
                for path in ("/api/v1/usage/summary", "/api/v1/usage/quota", "/api/v1/usage/events"):
                    resp = client.get(path, headers=headers)
                    assert resp.status_code == 403, (path, resp.text)
                detail = client.get("/api/v1/usage/tenants/11111111-1111-1111-1111-111111111111", headers=headers)
                assert detail.status_code == 403, detail.text
        finally:
            restore()
