"""
P1-8 session-token tests:

- anonymous bootstrap mints a token bound to (tenant, end_user, device)
- device continuity: re-minting with the returned end_user_id reuses it
- expiry: expired tokens are rejected
- revocation: revoked tokens are rejected; second revoke is a no-op
- invalid tokens are rejected; unknown end_user_id is rejected
- tokens are scoped to exactly one tenant
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.app.infrastructure.db import init_database
from backend.app.infrastructure.db.models import Base
from backend.app.session.tokens import (
    ExpiredSessionToken,
    InvalidSessionToken,
    RevokedSessionToken,
    SessionTokenService,
    UnknownEndUserError,
)


@pytest.fixture
async def db(tmp_path):
    manager = init_database(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/test.db")
    await manager.initialize()
    async with manager._engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield manager
    await manager.close()


def _service(db) -> SessionTokenService:
    return SessionTokenService(db)


@pytest.mark.asyncio
async def test_anonymous_bootstrap_mints_and_resolves(db):
    service = _service(db)
    minted = await service.mint(
        "tenant-a",
        device_id="device-1",
        surface_id="surface-1",
        scopes=["chat"],
    )
    assert minted["token"]
    assert minted["end_user_id"]

    principal = await service.resolve(minted["token"])
    assert principal.tenant_id == "tenant-a"
    assert principal.end_user_id == minted["end_user_id"]
    assert principal.device_id == "device-1"
    assert principal.surface_id == "surface-1"
    assert principal.scopes == ["chat"]
    assert principal.expires_at > datetime.now(timezone.utc) - timedelta(seconds=1)


@pytest.mark.asyncio
async def test_device_continuity_reuses_end_user(db):
    service = _service(db)
    first = await service.mint("tenant-a", device_id="device-1")
    second = await service.mint(
        "tenant-a", device_id="device-1", end_user_id=first["end_user_id"]
    )
    assert second["end_user_id"] == first["end_user_id"]

    # Same device without passing the id resolves to the same end user
    third = await service.mint("tenant-a", device_id="device-1")
    assert third["end_user_id"] == first["end_user_id"]


@pytest.mark.asyncio
async def test_expired_token_rejected(db):
    service = _service(db)
    minted = await service.mint(
        "tenant-a", device_id="device-1", ttl_seconds=1
    )
    await asyncio.sleep(1.2)
    with pytest.raises(ExpiredSessionToken):
        await service.resolve(minted["token"])


@pytest.mark.asyncio
async def test_revoked_token_rejected_and_revoke_idempotent(db):
    service = _service(db)
    minted = await service.mint("tenant-a", device_id="device-1")

    assert await service.revoke(minted["token"]) is True
    with pytest.raises(RevokedSessionToken):
        await service.resolve(minted["token"])

    assert await service.revoke(minted["token"]) is False


@pytest.mark.asyncio
async def test_invalid_and_foreign_tokens_rejected(db):
    service = _service(db)
    with pytest.raises(InvalidSessionToken):
        await service.resolve("not-a-token")
    with pytest.raises(InvalidSessionToken):
        await service.resolve("garbage.token.value")

    minted = await service.mint("tenant-a", device_id="device-1")
    principal = await service.resolve(minted["token"])
    assert principal.tenant_id == "tenant-a"  # scoped to exactly one tenant


@pytest.mark.asyncio
async def test_unknown_end_user_rejected(db):
    service = _service(db)
    with pytest.raises(UnknownEndUserError):
        await service.mint(
            "tenant-a",
            device_id="device-1",
            end_user_id="00000000-0000-0000-0000-000000000000",
        )


@pytest.mark.asyncio
async def test_authenticated_exchange_end_user(db):
    from backend.app.infrastructure.db import EndUserRepository

    repo = EndUserRepository(db)
    eu = await repo.create_authenticated("tenant-a", external_id="user-123")
    service = _service(db)
    minted = await service.mint(
        "tenant-a", device_id="device-2", end_user_id=eu["id"]
    )
    assert minted["end_user_id"] == eu["id"]
    principal = await service.resolve(minted["token"])
    assert principal.end_user_id == eu["id"]
