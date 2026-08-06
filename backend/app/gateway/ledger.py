"""
Cost ledger (Arch 10, P3-5).

Append-only USD spend capture. The gateway executor calls
``CostLedger.record()`` after every completed generation; the write is
enqueued as a ``cost_ledger.write`` job so the request path never blocks.
The worker handler persists one ``spend_events`` row (tenant, surface,
end-user, model, provider, tokens, USD) and upserts the durable
``quota_state`` rows for every budget level on the request's path (P3-6).

Enqueue failure falls back to a direct repository write — a spend event is
never dropped silently (codebase convention: degrade visibly, never
silent). Aggregation consumers (billing, dashboards, anomaly alerts) query
``sum_usd`` over the append-only table; quota reconstruction uses the
``quota_state`` rows.
"""

from __future__ import annotations

import datetime
import structlog
from dataclasses import asdict, dataclass
from typing import Any

from backend.app.infrastructure.queue.manager import Job, QueueManager

logger = structlog.get_logger(__name__)

JOB_COST_LEDGER_WRITE = "cost_ledger.write"


@dataclass
class UsageRecord:
    """One completed generation's spend, mirroring ``SpendEventModel``
    (minus id/created_at, filled at write time)."""

    tenant_id: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    usd: float = 0.0
    surface_id: str | None = None
    end_user_id: str | None = None
    conversation_id: str | None = None
    session_id: str | None = None
    request_id: str = ""


class CostLedger:
    """Append-only spend capture (Arch 10, P3-5)."""

    def __init__(self, queue: QueueManager | None = None, db: Any = None):
        self._queue = queue
        self._db = db
        self._repo: Any = None

    async def record(self, record: UsageRecord) -> None:
        """Enqueue the write; fall back to a direct DB write on enqueue
        failure; raise when no write path exists (never silent)."""
        payload = asdict(record)
        if self._queue is not None:
            try:
                ok = await self._queue.enqueue(
                    Job(
                        type=JOB_COST_LEDGER_WRITE,
                        payload=payload,
                        trace_id=record.request_id,
                    )
                )
                if ok:
                    return
            except Exception as e:
                logger.error("cost_ledger_enqueue_failed", error=str(e))
        if self._db is not None:
            from backend.app.infrastructure.db.repositories import SpendEventRepository

            await SpendEventRepository(self._db).add(payload)
            return
        raise RuntimeError(
            "cost ledger has no write path: configure queue or db "
            "(enqueue failed and no repository fallback)"
        )

    # -- aggregation consumers --------------------------------------------

    async def sum_usd(
        self,
        *,
        tenant_id: str,
        year: int,
        month: int,
        surface_id: str | None = None,
        end_user_id: str | None = None,
    ) -> float:
        """Total USD for a tenant (optionally scoped to surface/end-user)
        within a calendar month (UTC) — billing/dashboard consumer."""
        if self._db is None:
            raise RuntimeError("cost ledger has no db for aggregation")
        from sqlalchemy import func, select

        from backend.app.infrastructure.db.models import SpendEventModel

        start = datetime.datetime(year, month, 1, tzinfo=datetime.timezone.utc)
        end = start + datetime.timedelta(days=32)
        end = datetime.datetime(end.year, end.month, 1, tzinfo=datetime.timezone.utc)

        stmt = select(func.coalesce(func.sum(SpendEventModel.usd), 0.0)).where(
            SpendEventModel.tenant_id == tenant_id,
            SpendEventModel.created_at >= start,
            SpendEventModel.created_at < end,
        )
        if surface_id is not None:
            stmt = stmt.where(SpendEventModel.surface_id == surface_id)
        if end_user_id is not None:
            stmt = stmt.where(SpendEventModel.end_user_id == end_user_id)

        async with self._db.get_session() as session:
            return float((await session.execute(stmt)).scalar_one())
