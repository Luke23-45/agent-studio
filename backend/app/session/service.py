"""
Thread mutation service (Arch 7.1, P1-4).

Regenerate and edit are strictly append-only: a new message is appended
to the thread log with ``parent_message_id`` pointing at the original;
nothing is ever mutated in place. Both operations record their own
thread events and audit rows, so both attempts remain visible.

Generation of the new successor (filling its content) is wired in P1-10;
the appended message carries metadata ``status: pending_generation`` and
``generated_from``/``edited_from`` for that step.
"""

import structlog
from typing import Any
from uuid import uuid4

from backend.app.infrastructure.db import (
    AuditRepository,
    ConversationRepository,
    ThreadRepository,
)
from backend.app.infrastructure.db.threads import (
    ForkPointOutOfRange,
    MessageNotFoundError,
    ThreadNotFoundError,
)

logger = structlog.get_logger(__name__)


class ThreadMutationError(Exception):
    pass


class InvalidTargetError(ThreadMutationError):
    """The target message cannot be regenerated/edited (wrong role)."""


PENDING_GENERATION = "pending_generation"


class ThreadMutationService:
    def __init__(self, db: Any):
        self.db = db
        self.threads = ThreadRepository(db)
        self.audit = AuditRepository(db)

    async def regenerate(
        self,
        tenant_id: str,
        thread_id: str,
        message_id: str,
        *,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Append a regenerated assistant message parented to the original."""
        thread = await self.threads.get_thread(tenant_id, thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        original = await self.threads.get_message(tenant_id, message_id)
        if original is None:
            raise MessageNotFoundError(message_id)
        if original.get("role") != "assistant":
            raise InvalidTargetError(
                f"message {message_id} is {original.get('role')}, expected assistant"
            )

        replacement = await self.threads.append_message(
            tenant_id,
            thread_id,
            role="assistant",
            content="",
            redacted_content="",
            conversation_id=thread["conversation_id"],
            parent_message_id=original["id"],
            metadata={
                "status": PENDING_GENERATION,
                "regenerated_from": original["id"],
            },
        )
        await self.threads.append_event(
            tenant_id,
            thread_id,
            event_type="message.regenerated",
            payload={
                "original_message_id": original["id"],
                "new_message_id": replacement["id"],
            },
        )
        await self.audit.add(
            action="thread.regenerated",
            resource_type="thread",
            resource_id=thread_id,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            details={
                "original_message_id": original["id"],
                "new_message_id": replacement["id"],
            },
        )
        logger.info(
            "thread_message_regenerated",
            tenant_id=tenant_id,
            thread_id=thread_id,
            original_message_id=original["id"],
            new_message_id=replacement["id"],
        )
        return {
            "thread_id": thread_id,
            "original_message_id": original["id"],
            "new_message_id": replacement["id"],
            "message": replacement,
        }

    async def fork(
        self,
        tenant_id: str,
        thread_id: str,
        at_seq: int,
        *,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Fork a thread at a message boundary; original untouched."""
        thread = await self.threads.get_thread(tenant_id, thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)

        conversation = await ConversationRepository(self.db).get_or_create(
            tenant_id, f"fork-{uuid4()}"
        )
        try:
            new_thread = await self.threads.fork_thread(
                tenant_id,
                thread_id,
                at_seq,
                conversation_id=conversation["id"],
            )
        except ForkPointOutOfRange as exc:
            raise InvalidTargetError(str(exc)) from None

        await self.audit.add(
            action="thread.forked",
            resource_type="thread",
            resource_id=thread_id,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            details={"at_seq": at_seq, "new_thread_id": new_thread["id"]},
        )
        logger.info(
            "thread_forked",
            tenant_id=tenant_id,
            source_thread_id=thread_id,
            new_thread_id=new_thread["id"],
            at_seq=at_seq,
        )
        return {
            "source_thread_id": thread_id,
            "new_thread_id": new_thread["id"],
            "at_seq": at_seq,
            "thread": new_thread,
        }

    async def edit(
        self,
        tenant_id: str,
        thread_id: str,
        message_id: str,
        content: str,
        redacted_content: str | None = None,
        *,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        """Append an edited user message + pending successor, original untouched."""
        original = await self.threads.get_message(tenant_id, message_id)
        if original is None:
            raise MessageNotFoundError(message_id)
        if original.get("thread_id") != thread_id:
            raise ThreadNotFoundError(thread_id)
        thread = await self.threads.get_thread(tenant_id, thread_id)
        if thread is None:
            raise ThreadNotFoundError(thread_id)
        if original.get("role") != "user":
            raise InvalidTargetError(
                f"message {message_id} is {original.get('role')}, expected user"
            )
        if not content or not content.strip():
            raise InvalidTargetError("edited content must not be empty")

        edited_user = await self.threads.append_message(
            tenant_id,
            thread_id,
            role="user",
            content=content,
            redacted_content=redacted_content or content,
            conversation_id=thread["conversation_id"],
            parent_message_id=original.get("parent_message_id"),
            metadata={"status": "applied", "edited_from": original["id"]},
        )
        successor = await self.threads.append_message(
            tenant_id,
            thread_id,
            role="assistant",
            content="",
            redacted_content="",
            conversation_id=thread["conversation_id"],
            parent_message_id=edited_user["id"],
            metadata={
                "status": PENDING_GENERATION,
                "generated_from": edited_user["id"],
            },
        )
        await self.threads.append_event(
            tenant_id,
            thread_id,
            event_type="message.edited",
            payload={
                "original_message_id": original["id"],
                "new_user_message_id": edited_user["id"],
                "new_assistant_message_id": successor["id"],
            },
        )
        await self.audit.add(
            action="thread.message_edited",
            resource_type="thread",
            resource_id=thread_id,
            tenant_id=tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            details={
                "original_message_id": original["id"],
                "new_user_message_id": edited_user["id"],
                "new_assistant_message_id": successor["id"],
            },
        )
        logger.info(
            "thread_message_edited",
            tenant_id=tenant_id,
            thread_id=thread_id,
            original_message_id=original["id"],
            new_user_message_id=edited_user["id"],
        )
        return {
            "thread_id": thread_id,
            "original_message_id": original["id"],
            "new_user_message_id": edited_user["id"],
            "new_assistant_message_id": successor["id"],
            "message": successor,
        }
