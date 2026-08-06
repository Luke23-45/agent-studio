"""
P1-9 surfaces tests:

- CRUD is tenant-scoped (cross-tenant reads return None)
- first created surface is the default; deactivating it denies resolution
- two surfaces on one tenant carry different configs (persona/rails)
- unconfigured surface resolves to None (default-deny, P0-4)
"""

import pytest

from backend.app.api.routes.surfaces import resolve_surface
from backend.app.infrastructure.db import SurfaceRepository, init_database
from backend.app.infrastructure.db.models import Base


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


@pytest.mark.asyncio
async def test_crud_is_tenant_scoped(db):
    repo = SurfaceRepository(db)
    sales = await repo.create(
        "tenant-a", "Sales", persona="sales rep",
        tool_allowlist=["search"],
    )
    await repo.create("tenant-a", "Support", persona="support rep")
    await repo.create("tenant-b", "Other", persona="other")

    assert [s["name"] for s in await repo.list_by_tenant("tenant-a")] == [
        "Sales", "Support",
    ]
    assert await repo.get("tenant-b", sales["id"]) is None

    updated = await repo.update("tenant-a", sales["id"], persona="new persona")
    assert updated["persona"] == "new persona"
    assert await repo.update("tenant-b", sales["id"], persona="x") is None

    assert await repo.delete("tenant-a", sales["id"]) is True
    assert await repo.get("tenant-a", sales["id"]) is None
    assert await repo.delete("tenant-a", sales["id"]) is False


@pytest.mark.asyncio
async def test_first_surface_is_default_and_deactivation_denies(db):
    repo = SurfaceRepository(db)
    first = await repo.create("tenant-a", "Sales", persona="sales")
    await repo.create("tenant-a", "Support", persona="support")

    resolved = await resolve_surface("tenant-a", None)
    assert resolved["id"] == first["id"]

    # Deactivating the default promotes the next active surface; once all
    # are inactive the request path denies (default-deny, P0-4).
    await repo.update("tenant-a", first["id"], active=False)
    promoted = await resolve_surface("tenant-a", None)
    assert promoted is not None and promoted["id"] != first["id"]

    for s in await repo.list_by_tenant("tenant-a"):
        await repo.update("tenant-a", s["id"], active=False)
    assert await resolve_surface("tenant-a", None) is None


@pytest.mark.asyncio
async def test_unconfigured_surface_resolves_none(db):
    repo = SurfaceRepository(db)
    surface = await repo.create("tenant-a", "Sales", persona="sales")
    await repo.update("tenant-a", surface["id"], active=False)

    assert await resolve_surface("tenant-a", surface["id"]) is None
    assert await resolve_surface("tenant-a", str(surface["id"]) + "x") is None


@pytest.mark.asyncio
async def test_two_surfaces_carry_different_rails(db):
    repo = SurfaceRepository(db)
    sales = await repo.create(
        "tenant-a", "Sales",
        persona="sales rep",
        tool_allowlist=["search"],
        model_pin="gpt-4o",
        budgets={"monthly_tokens": 1_000_000},
    )
    support = await repo.create(
        "tenant-a", "Support",
        persona="support rep",
        tool_allowlist=["ticket"],
        model_pin="claude-3-5-sonnet",
    )

    sales_resolved = await resolve_surface("tenant-a", sales["id"])
    support_resolved = await resolve_surface("tenant-a", support["id"])
    assert sales_resolved["tool_allowlist"] == ["search"]
    assert support_resolved["tool_allowlist"] == ["ticket"]
    assert sales_resolved["model_pin"] != support_resolved["model_pin"]
    assert sales_resolved["budgets"]["monthly_tokens"] == 1_000_000
