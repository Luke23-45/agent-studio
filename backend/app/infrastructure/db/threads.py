"""
Thread store repository (Arch 7.1, P1-2).

The durable append-only thread log is the source of truth. Appends are
atomic per thread (seq assigned under a thread-row lock; the unique
(thread_id, seq) constraint is the last line of defense). Request-id
dedup: a retried append with the same request_id returns the original
message instead of double-writing. Every query is tenant-scoped (and
end-user scoped where the caller passes one).
"""

import structlog
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncGenerator, Sequence
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from backend.app.context import TOOL_RESULT_PLACEHOLDER

from .manager import DatabaseManager
from .models import (
    MessageModel,
    MessagePartModel,
    ThreadEventModel,
    ThreadModel,
)
from .replicas import ReplicaRouter, get_replica_router
from .repositories import _now, _row_to_dict

logger = structlog.get_logger(__name__)

TEXT_PART_TYPE = "text"
TOOL_RESULT_PART_TYPE = "tool_result"


class ThreadNotFoundError(Exception):
    pass


class DuplicateRequestError(Exception):
    """A message with this request_id already exists (deduped, not an error)."""


class MessageNotFoundError(Exception):
    """A message id does not exist within the thread."""


class ForkPointOutOfRange(Exception):
    """Fork point is beyond the thread's message length."""


class ThreadRepository:
    def __init__(self, db: DatabaseManager, *, read_router: ReplicaRouter | None = None):
        self.db = db
        # P8-3: pure history reads route to read replicas when configured;
        # writes and correctness-critical reads always use the primary.
        self.read_router = read_router if read_router is not None else get_replica_router()
        # Fallback reads (no replicas / unhealthy / RYW window) must land on
        # the SAME database this repository writes to.
        self.read_router.bind_primary(db)

    @asynccontextmanager
    async def _read_session(
        self, tenant_id: str, thread_id: str | None = None
    ) -> AsyncGenerator[Any, None]:
        """Session for pure history reads (replica-routed, P8-3).

        ``(tenant_id, thread_id)`` are read-your-writes keys: a thread this
        process wrote within the window is read back from the primary so
        the caller never observes replica lag on its own writes.
        """
        async with self.read_router.get_read_session(tenant_id, thread_id) as session:
            yield session

    # ---- thread lifecycle -------------------------------------------------

    async def create_thread(
        self,
        tenant_id: str,
        *,
        conversation_id: str,
        surface_id: str | None = None,
        end_user_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a thread with its first durable event (thread.created)."""
        thread_id = str(uuid4())
        async with self.db.get_session() as session:
            session.add(
                ThreadModel(
                    id=thread_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    surface_id=surface_id,
                    end_user_id=end_user_id,
                    status="active",
                    summary_version=0,
                    archived=False,
                )
            )
            session.add(
                ThreadEventModel(
                    id=str(uuid4()),
                    thread_id=thread_id,
                    tenant_id=tenant_id,
                    seq=1,
                    event_type="thread.created",
                    request_id=request_id,
                    payload={"conversation_id": conversation_id},
                )
            )
            await session.flush()
            # RYW: tenant key keeps thread listings consistent; conversation
            # key covers conversation-scoped list reads; thread id covers
            # single-thread history reads.
            self.read_router.mark_write(tenant_id, conversation_id, thread_id)
        logger.info(
            "thread_created", tenant_id=tenant_id, thread_id=thread_id
        )
        return await self.get_thread(tenant_id, thread_id)  # type: ignore[return-value]

    async def get_thread(self, tenant_id: str, thread_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.tenant_id == tenant_id,
                    ThreadModel.id == thread_id,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def get_or_create_for_conversation(
        self,
        tenant_id: str,
        conversation_id: str,
        *,
        surface_id: str | None = None,
        end_user_id: str | None = None,
    ) -> dict[str, Any]:
        """Bind a thread to an existing conversation (create if absent)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.tenant_id == tenant_id,
                    ThreadModel.conversation_id == conversation_id,
                )
            )
            row = result.scalar_one_or_none()
            if row:
                return _row_to_dict(row)
        return await self.create_thread(
            tenant_id,
            conversation_id=conversation_id,
            surface_id=surface_id,
            end_user_id=end_user_id,
        )

    async def list_threads(
        self,
        tenant_id: str,
        *,
        end_user_id: str | None = None,
        limit: int = 50,
        before: str | None = None,
    ) -> list[dict[str, Any]]:
        """List threads for a tenant, optionally scoped to one end user.

        Tenant-keyed read-your-writes: a thread created by this process
        within the window is read back from the primary, so fresh threads
        always appear in listings.
        """
        async with self._read_session(tenant_id) as session:
            stmt = select(ThreadModel).where(ThreadModel.tenant_id == tenant_id)
            if end_user_id:
                stmt = stmt.where(ThreadModel.end_user_id == end_user_id)
            if before:
                before_row = await session.get(ThreadModel, before)
                if before_row:
                    stmt = stmt.where(ThreadModel.created_at < before_row.created_at)
            stmt = stmt.order_by(ThreadModel.created_at.desc()).limit(limit)
            result = await session.execute(stmt)
            return [_row_to_dict(r) for r in result.scalars()]

    async def delete_by_end_user(self, tenant_id: str, end_user_id: str) -> int:
        """Hard-delete threads for one end user (DSR erasure)."""
        async with self.db.get_session() as session:
            result = await session.execute(
                delete(ThreadModel).where(
                    ThreadModel.tenant_id == tenant_id,
                    ThreadModel.end_user_id == end_user_id,
                )
            )
            # RYW: post-erasure reads stay on the primary so a lagging
            # replica never resurrects erased rows.
            self.read_router.mark_write(tenant_id)
            return result.rowcount or 0

    # ---- append path ------------------------------------------------------

    async def append_message(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        role: str,
        content: str,
        redacted_content: str | None = None,
        conversation_id: str,
        request_id: str | None = None,
        parent_message_id: str | None = None,
        surface_id: str | None = None,
        end_user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        extra_parts: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Atomically append a message + parts + event; dedups on request_id.

        The first part is the canonical text part (part_index 0); additional
        parts (tool_use, reasoning, citation, ...) follow in order. Returns
        the message dict with a ``deduped`` marker when request_id matched
        an existing message (no double write).
        """
        async with self.db.get_session() as session:
            if request_id:
                result = await session.execute(
                    select(MessageModel).where(
                        MessageModel.tenant_id == tenant_id,
                        MessageModel.request_id == request_id,
                    )
                )
                existing = result.scalar_one_or_none()
                if existing:
                    return {**_row_to_dict(existing), "deduped": True}

            thread = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.id == thread_id,
                    ThreadModel.tenant_id == tenant_id,
                ).with_for_update()
            )
            thread_row = thread.scalar_one_or_none()
            if thread_row is None:
                raise ThreadNotFoundError(f"Thread not found: {thread_id}")

            next_seq = (await self._max_message_seq(session, thread_id)) + 1
            event_seq = await self._max_event_seq(session, thread_id)

            message = MessageModel(
                id=str(uuid4()),
                conversation_id=conversation_id,
                tenant_id=tenant_id,
                thread_id=thread_id,
                seq=next_seq,
                request_id=request_id,
                parent_message_id=parent_message_id,
                surface_id=surface_id,
                end_user_id=end_user_id,
                role=role,
                content=content,
                redacted_content=redacted_content or content,
                metadata_json=metadata or {},
            )
            session.add(message)
            await session.flush()

            parts = [
                {
                    "part_type": TEXT_PART_TYPE,
                    "content": {"text": content},
                    "redacted_content": {"text": redacted_content or content},
                }
            ]
            for extra in extra_parts or []:
                parts.append(
                    {
                        "part_type": extra["part_type"],
                        "content": extra.get("content", {}),
                        "redacted_content": extra.get(
                            "redacted_content", extra.get("content", {})
                        ),
                    }
                )
            for index, part in enumerate(parts):
                session.add(
                    MessagePartModel(
                        id=str(uuid4()),
                        message_id=message.id,
                        thread_id=thread_id,
                        tenant_id=tenant_id,
                        part_type=part["part_type"],
                        part_index=index,
                        content=part["content"],
                        redacted_content=part["redacted_content"],
                    )
                )

            session.add(
                ThreadEventModel(
                    id=str(uuid4()),
                    thread_id=thread_id,
                    tenant_id=tenant_id,
                    seq=event_seq,
                    event_type="message.appended",
                    request_id=request_id,
                    payload={
                        "message_id": message.id,
                        "role": role,
                        "parent_message_id": parent_message_id,
                    },
                )
            )
            await session.flush()
            row = _row_to_dict(message)
            row["deduped"] = False
            self.read_router.mark_write(tenant_id, thread_id, message.id)
            return row

    @staticmethod
    async def _max_message_seq(session: Any, thread_id: str) -> int:
        result = await session.execute(
            select(func.max(MessageModel.seq)).where(
                MessageModel.thread_id == thread_id
            )
        )
        return result.scalar() or 0

    @staticmethod
    async def _max_event_seq(session: Any, thread_id: str) -> int:
        result = await session.execute(
            select(func.max(ThreadEventModel.seq)).where(
                ThreadEventModel.thread_id == thread_id
            )
        )
        return (result.scalar() or 0) + 1

    async def append_event(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        event_type: str,
        payload: dict[str, Any],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a non-message event (compaction, fork, removal, ...)."""
        async with self.db.get_session() as session:
            thread = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.id == thread_id,
                    ThreadModel.tenant_id == tenant_id,
                ).with_for_update()
            )
            if thread.scalar_one_or_none() is None:
                raise ThreadNotFoundError(f"Thread not found: {thread_id}")
            result = await session.execute(
                select(func.max(ThreadEventModel.seq)).where(
                    ThreadEventModel.thread_id == thread_id
                )
            )
            next_seq = (result.scalar() or 0) + 1
            event = ThreadEventModel(
                id=str(uuid4()),
                thread_id=thread_id,
                tenant_id=tenant_id,
                seq=next_seq,
                event_type=event_type,
                request_id=request_id,
                payload=payload,
            )
            session.add(event)
            await session.flush()
            self.read_router.mark_write(tenant_id, thread_id)
            return _row_to_dict(event)

    # ---- read paths -------------------------------------------------------

    async def get_message(self, tenant_id: str, message_id: str) -> dict[str, Any] | None:
        async with self.db.get_session() as session:
            result = await session.execute(
                select(MessageModel).where(
                    MessageModel.tenant_id == tenant_id,
                    MessageModel.id == message_id,
                )
            )
            row = result.scalar_one_or_none()
            return _row_to_dict(row) if row else None

    async def list_messages(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Cursor pagination over the durable log (ascending seq).

        Returns ``{messages, has_more}``; the next page starts at
        ``messages[-1].seq``.
        """
        async with self._read_session(tenant_id, thread_id) as session:
            stmt = (
                select(MessageModel)
                .where(
                    MessageModel.thread_id == thread_id,
                    MessageModel.tenant_id == tenant_id,
                )
                .order_by(MessageModel.seq.asc())
                .limit(limit + 1)
            )
            if after_seq is not None:
                stmt = stmt.where(MessageModel.seq > after_seq)
            result = await session.execute(stmt)
            rows = [_row_to_dict(r) for r in result.scalars()]
            has_more = len(rows) > limit
            return {"messages": rows[:limit], "has_more": has_more}

    async def read_tail(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        from_seq: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Newest-first tail (oldest of the returned set first in list order)."""
        async with self._read_session(tenant_id, thread_id) as session:
            stmt = (
                select(MessageModel)
                .where(
                    MessageModel.thread_id == thread_id,
                    MessageModel.tenant_id == tenant_id,
                )
                .order_by(MessageModel.seq.desc())
                .limit(limit)
            )
            if from_seq is not None:
                stmt = stmt.where(MessageModel.seq <= from_seq)
            result = await session.execute(stmt)
            rows = [_row_to_dict(r) for r in result.scalars()]
            rows.reverse()
            return rows

    async def list_threads_stale_for_compaction(
        self,
        *,
        since_seq_delta: int,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Threads whose newest message outgrew the compaction boundary (P2-5).

        Only threads with an existing summary (``summary_position`` set) are
        candidates — the background refresh keeps running summaries current.
        Returns thread rows ordered by most-outgrown first, with the latest
        message seq attached (``latest_seq``) for observability.
        """
        async with self.db.get_session() as session:
            latest = (
                select(
                    MessageModel.thread_id,
                    func.max(MessageModel.seq).label("latest_seq"),
                )
                .group_by(MessageModel.thread_id)
                .subquery()
            )
            stmt = (
                select(ThreadModel, latest.c.latest_seq)
                .join(latest, latest.c.thread_id == ThreadModel.id)
                .where(
                    ThreadModel.summary_position.is_not(None),
                    ThreadModel.status == "active",
                    ThreadModel.archived.is_(False),
                    (latest.c.latest_seq - ThreadModel.summary_position)
                    >= since_seq_delta,
                )
                .order_by(
                    (latest.c.latest_seq - ThreadModel.summary_position).desc()
                )
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [
                {**_row_to_dict(row), "latest_seq": latest_seq}
                for row, latest_seq in result.all()
            ]

    async def set_summary(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        summary_block: dict[str, Any],
        summary_position: int,
        boundary_message_id: str,
        request_id: str | None = None,
        degraded: bool = False,
    ) -> dict[str, Any]:
        """Atomic compaction checkpoint swap (Arch 8.2, P2-3).

        Updates the thread's summary block (version+1) and appends a
        compaction event + a ``compaction`` part on the boundary message in
        ONE transaction: the prior checkpoint stays active if anything
        fails (compaction never lands in a partial state). ``degraded``
        (P2-4 lossy-truncation fallback) marks the event + part for
        operators and auditing.
        """
        async with self.db.get_session() as session:
            thread = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.id == thread_id,
                    ThreadModel.tenant_id == tenant_id,
                ).with_for_update()
            )
            row = thread.scalar_one_or_none()
            if row is None:
                raise ThreadNotFoundError(f"Thread not found: {thread_id}")

            new_version = (row.summary_version or 0) + 1
            row.summary_block = summary_block
            row.summary_position = summary_position
            row.summary_version = new_version
            await session.flush()

            event_seq = (await self._max_event_seq(session, thread_id)) + 1
            event = ThreadEventModel(
                id=str(uuid4()),
                thread_id=thread_id,
                tenant_id=tenant_id,
                seq=event_seq,
                event_type="compaction",
                request_id=request_id,
                payload={
                    "summary_version": new_version,
                    "position": summary_position,
                    "boundary_message_id": boundary_message_id,
                    "degraded": degraded,
                },
            )
            session.add(event)

            part_index_result = await session.execute(
                select(func.max(MessagePartModel.part_index)).where(
                    MessagePartModel.message_id == boundary_message_id
                )
            )
            part = MessagePartModel(
                id=str(uuid4()),
                message_id=boundary_message_id,
                thread_id=thread_id,
                tenant_id=tenant_id,
                part_type="compaction",
                part_index=(part_index_result.scalar() or 0) + 1,
                content={"summary": summary_block.get("payload", {}), "degraded": degraded},
                redacted_content={"summary": summary_block.get("content", ""), "degraded": degraded},
            )
            session.add(part)
            await session.flush()
            self.read_router.mark_write(tenant_id, thread_id)

            return {
                "summary_version": new_version,
                "summary_position": summary_position,
                "event": _row_to_dict(event),
                "part": _row_to_dict(part),
            }

    async def fork_thread(
        self,
        tenant_id: str,
        source_thread_id: str,
        at_seq: int,
        *,
        conversation_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Fork a thread at a message boundary (Arch 7.1, P1-5).

        Copies every message with ``seq <= at_seq`` (and their parts) into a
        new thread, remapping parent links to the copied ids. The source
        thread is untouched apart from an appended ``thread.forked`` event.
        Everything runs in one transaction. ``at_seq=0`` yields an empty
        fork (thread + fork events only).
        """
        new_thread_id = str(uuid4())
        async with self.db.get_session() as session:
            source = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.id == source_thread_id,
                    ThreadModel.tenant_id == tenant_id,
                ).with_for_update()
            )
            source_row = source.scalar_one_or_none()
            if source_row is None:
                raise ThreadNotFoundError(f"Thread not found: {source_thread_id}")

            messages_result = await session.execute(
                select(MessageModel)
                .where(
                    MessageModel.thread_id == source_thread_id,
                    MessageModel.tenant_id == tenant_id,
                    MessageModel.seq <= at_seq,
                )
                .order_by(MessageModel.seq.asc())
            )
            source_messages = list(messages_result.scalars())
            if at_seq > 0 and not source_messages:
                raise ForkPointOutOfRange(
                    f"fork point {at_seq} exceeds thread length"
                )
            max_seq = await self._max_message_seq(session, source_thread_id)
            if at_seq > max_seq:
                raise ForkPointOutOfRange(
                    f"fork point {at_seq} exceeds thread length {max_seq}"
                )

            session.add(
                ThreadModel(
                    id=new_thread_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    surface_id=source_row.surface_id,
                    end_user_id=source_row.end_user_id,
                    status="active",
                    summary_version=0,
                    archived=False,
                )
            )
            session.add(
                ThreadEventModel(
                    id=str(uuid4()),
                    thread_id=new_thread_id,
                    tenant_id=tenant_id,
                    seq=1,
                    event_type="thread.created",
                    request_id=request_id,
                    payload={
                        "conversation_id": conversation_id,
                        "forked_from": source_thread_id,
                    },
                )
            )
            session.add(
                ThreadEventModel(
                    id=str(uuid4()),
                    thread_id=new_thread_id,
                    tenant_id=tenant_id,
                    seq=2,
                    event_type="thread.forked",
                    request_id=request_id,
                    payload={"source_thread_id": source_thread_id, "at_seq": at_seq},
                )
            )

            id_map: dict[str, str] = {}
            for message in source_messages:
                new_id = str(uuid4())
                id_map[message.id] = new_id
                new_parent = (
                    id_map.get(message.parent_message_id) if message.parent_message_id else None
                )
                metadata = dict(message.metadata_json or {})
                metadata.update(
                    {
                        "forked_from": source_thread_id,
                        "forked_at_seq": at_seq,
                    }
                )
                session.add(
                    MessageModel(
                        id=new_id,
                        conversation_id=conversation_id,
                        tenant_id=tenant_id,
                        thread_id=new_thread_id,
                        seq=message.seq,
                        request_id=None,
                        parent_message_id=new_parent,
                        surface_id=message.surface_id,
                        end_user_id=message.end_user_id,
                        role=message.role,
                        content=message.content,
                        redacted_content=message.redacted_content,
                        metadata_json=metadata,
                        created_at=message.created_at,
                    )
                )
                parts_result = await session.execute(
                    select(MessagePartModel).where(
                        MessagePartModel.tenant_id == tenant_id,
                        MessagePartModel.message_id == message.id,
                    )
                )
                for part in parts_result.scalars():
                    session.add(
                        MessagePartModel(
                            id=str(uuid4()),
                            message_id=new_id,
                            thread_id=new_thread_id,
                            tenant_id=tenant_id,
                            part_type=part.part_type,
                            part_index=part.part_index,
                            content=part.content,
                            redacted_content=part.redacted_content,
                        )
                    )

            event_seq = (
                await session.execute(
                    select(func.max(ThreadEventModel.seq)).where(
                        ThreadEventModel.thread_id == source_thread_id
                    )
                )
            ).scalar() or 0
            session.add(
                ThreadEventModel(
                    id=str(uuid4()),
                    thread_id=source_thread_id,
                    tenant_id=tenant_id,
                    seq=event_seq + 1,
                    event_type="thread.forked",
                    request_id=request_id,
                    payload={"new_thread_id": new_thread_id, "at_seq": at_seq},
                )
            )
            await session.flush()
            self.read_router.mark_write(tenant_id, new_thread_id)
            self.read_router.mark_write(tenant_id, source_thread_id)

        logger.info(
            "thread_forked",
            tenant_id=tenant_id,
            source_thread_id=source_thread_id,
            new_thread_id=new_thread_id,
            at_seq=at_seq,
        )
        return await self.get_thread(tenant_id, new_thread_id)  # type: ignore[return-value]

    async def list_parts(
        self, tenant_id: str, message_id: str
    ) -> list[dict[str, Any]]:
        async with self._read_session(tenant_id, message_id) as session:
            result = await session.execute(
                select(MessagePartModel)
                .where(
                    MessagePartModel.tenant_id == tenant_id,
                    MessagePartModel.message_id == message_id,
                )
                .order_by(MessagePartModel.part_index.asc())
            )
            return [_row_to_dict(r) for r in result.scalars()]

    async def list_tool_result_seqs(
        self, tenant_id: str, thread_id: str
    ) -> set[int]:
        """Seqs of messages carrying a live (non-cleared) tool_result part.

        P2-7 render-time marker: the route flags ``has_tool_payload`` on
        turns from this set so the assembler can substitute the placeholder
        instead of the raw payload when clearing is enabled. Cleared parts
        (``content["cleared"]``) are excluded -- they no longer bloat
        context.
        """
        async with self._read_session(tenant_id, thread_id) as session:
            result = await session.execute(
                select(MessageModel.seq, MessagePartModel.content)
                .join(
                    MessagePartModel,
                    MessagePartModel.message_id == MessageModel.id,
                )
                .where(
                    MessagePartModel.tenant_id == tenant_id,
                    MessagePartModel.thread_id == thread_id,
                    MessagePartModel.part_type == TOOL_RESULT_PART_TYPE,
                )
            )
            return {
                seq
                for seq, content in result.all()
                if not (content or {}).get("cleared")
            }

    async def clear_tool_results(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        keep_recent_turns: int = 0,
        placeholder: str = TOOL_RESULT_PLACEHOLDER,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Sub-transcript op (Arch 8.3, P2-7): reclaim stale tool payloads.

        Replaces the content of every tool_result part at or before
        ``latest_seq - keep_recent_turns`` with a cleared marker -- the
        payload is dropped but ``tool_use_id`` survives so the tool can be
        re-fetched on demand; the ``tool_use`` parts are untouched (the
        record remains, only the payload is reclaimed). Idempotent:
        already-cleared parts are skipped and no event is written when
        nothing was cleared. One transaction: the thread row is locked,
        parts updated, and a ``tool_result.clear`` event appended
        atomically (the prior state stays active if anything fails).
        """
        async with self.db.get_session() as session:
            thread = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.id == thread_id,
                    ThreadModel.tenant_id == tenant_id,
                ).with_for_update()
            )
            if thread.scalar_one_or_none() is None:
                raise ThreadNotFoundError(f"Thread not found: {thread_id}")

            latest_seq = await self._max_message_seq(session, thread_id)
            threshold = latest_seq - keep_recent_turns
            result = await session.execute(
                select(MessagePartModel, MessageModel.seq)
                .join(MessageModel, MessagePartModel.message_id == MessageModel.id)
                .where(
                    MessagePartModel.tenant_id == tenant_id,
                    MessagePartModel.thread_id == thread_id,
                    MessagePartModel.part_type == TOOL_RESULT_PART_TYPE,
                    MessageModel.seq <= threshold,
                )
            )
            eligible = [
                part
                for part, _seq in result.all()
                if not (part.content or {}).get("cleared")
            ]
            if not eligible:
                return {"cleared": 0, "messages_affected": 0}

            affected: set[str] = set()
            for part in eligible:
                tool_use_id = (part.content or {}).get("tool_use_id")
                part.content = {"tool_use_id": tool_use_id, "cleared": True}
                part.redacted_content = {
                    "tool_use_id": tool_use_id,
                    "cleared": True,
                    "placeholder": placeholder,
                }
                affected.add(part.message_id)
            await session.flush()

            event_seq = (await self._max_event_seq(session, thread_id)) + 1
            event = ThreadEventModel(
                id=str(uuid4()),
                thread_id=thread_id,
                tenant_id=tenant_id,
                seq=event_seq,
                event_type="tool_result.clear",
                request_id=request_id,
                payload={
                    "cleared": len(eligible),
                    "messages_affected": len(affected),
                    "keep_recent_turns": keep_recent_turns,
                },
            )
            session.add(event)
            await session.flush()
            self.read_router.mark_write(tenant_id, thread_id)

            return {
                "cleared": len(eligible),
                "messages_affected": len(affected),
                "event": _row_to_dict(event),
            }

    async def list_events(
        self,
        tenant_id: str,
        thread_id: str,
        *,
        after_seq: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self._read_session(tenant_id, thread_id) as session:
            stmt = (
                select(ThreadEventModel)
                .where(
                    ThreadEventModel.thread_id == thread_id,
                    ThreadEventModel.tenant_id == tenant_id,
                )
                .order_by(ThreadEventModel.seq.asc())
                .limit(limit)
            )
            if after_seq is not None:
                stmt = stmt.where(ThreadEventModel.seq > after_seq)
            result = await session.execute(stmt)
            return [_row_to_dict(r) for r in result.scalars()]

    async def list_threads_archivable(
        self,
        tenant_id: str,
        *,
        older_than_days: int,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Threads eligible for the cold-tier archive (P1-7).

        Returns non-archived threads whose newest activity is older than
        the tenant's retention window, newest-first. Archive eligibility is
        derived from the thread ``created_at`` (the log is immutable, so a
        thread's lifetime is its creation timestamp).
        """
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        async with self._read_session(tenant_id, tenant_id) as session:
            stmt = (
                select(ThreadModel)
                .where(
                    ThreadModel.tenant_id == tenant_id,
                    ThreadModel.archived.is_(False),
                    ThreadModel.created_at < cutoff,
                )
                .order_by(ThreadModel.created_at.asc())
                .limit(limit)
            )
            result = await session.execute(stmt)
            return [_row_to_dict(r) for r in result.scalars()]

    async def dump_thread(
        self, tenant_id: str, thread_id: str
    ) -> dict[str, Any] | None:
        """Full durable payload of a thread (P1-7 archive bundle).

        Thread row + all messages + all parts + all events, serialized as
        plain dicts for the object-storage archive. Returns None when the
        thread does not belong to the tenant. Parts carry both ``content``
        and ``redacted_content`` so the archive preserves the access-
        controlled raw payload for DSR/legal re-reads.
        """
        async with self._read_session(tenant_id, thread_id) as session:
            thread = (
                await session.execute(
                    select(ThreadModel).where(
                        ThreadModel.id == thread_id,
                        ThreadModel.tenant_id == tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if thread is None:
                return None
            messages = (
                await session.execute(
                    select(MessageModel)
                    .where(
                        MessageModel.thread_id == thread_id,
                        MessageModel.tenant_id == tenant_id,
                    )
                    .order_by(MessageModel.seq.asc())
                )
            ).scalars()
            parts = (
                await session.execute(
                    select(MessagePartModel)
                    .where(
                        MessagePartModel.thread_id == thread_id,
                        MessagePartModel.tenant_id == tenant_id,
                    )
                    .order_by(MessagePartModel.part_index.asc())
                )
            ).scalars()
            events = (
                await session.execute(
                    select(ThreadEventModel)
                    .where(
                        ThreadEventModel.thread_id == thread_id,
                        ThreadEventModel.tenant_id == tenant_id,
                    )
                    .order_by(ThreadEventModel.seq.asc())
                )
            ).scalars()
            return {
                "thread": _row_to_dict(thread),
                "messages": [_row_to_dict(m) for m in messages],
                "parts": [_row_to_dict(p) for p in parts],
                "events": [_row_to_dict(e) for e in events],
                "archived_at": None,
                "checksum_algorithm": "sha256",
            }

    async def set_archived(
        self,
        tenant_id: str,
        thread_id: str,
        archived: bool,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Flip the cold-tier marker with a durable event (P1-7).

        Appends ``thread.archive`` / ``thread.restore`` to the thread's
        event log in the same transaction as the flag flip; the flag and
        the event never diverge.
        """
        async with self.db.get_session() as session:
            thread = await session.execute(
                select(ThreadModel).where(
                    ThreadModel.id == thread_id,
                    ThreadModel.tenant_id == tenant_id,
                ).with_for_update()
            )
            row = thread.scalar_one_or_none()
            if row is None:
                raise ThreadNotFoundError(f"Thread not found: {thread_id}")

            row.archived = archived
            await session.flush()

            event_seq = (await self._max_event_seq(session, thread_id)) + 1
            event = ThreadEventModel(
                id=str(uuid4()),
                thread_id=thread_id,
                tenant_id=tenant_id,
                seq=event_seq,
                event_type="thread.archive" if archived else "thread.restore",
                request_id=request_id,
                payload={"archived": archived},
            )
            session.add(event)
            await session.flush()
            self.read_router.mark_write(tenant_id, thread_id)

            return {"archived": archived, "event": _row_to_dict(event)}
