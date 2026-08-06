"""Backfill legacy conversations/messages into the thread store (migration 0004).

For every conversation that has no thread yet:
  1. Creates a durable `threads` row (Arch 7.1), linked to the conversation.
  2. Assigns each message `tenant_id`, `thread_id`, and a per-thread `seq`
     (ordered by created_at), leaving request_id / parent_message_id /
     surface_id / end_user_id NULL (unknown for legacy data).
  3. Creates one `text` part per message from content / redacted_content
     (redacted = existing redacted_content, falling back to raw content;
     the P5-4 policy completes redaction for new writes).

Idempotent: conversations that already have a thread (or any message with a
thread_id) are skipped. Safe to re-run.

Usage:
    python -m backend.scripts.backfill_threads_0004
"""
import asyncio
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.app.infrastructure.db import init_database
from backend.app.infrastructure.db.models import (
    ConversationModel,
    MessageModel,
    MessagePartModel,
    ThreadModel,
)
from backend.app.settings.env import settings


async def backfill() -> None:
    db = init_database(settings.DATABASE_URL, echo=False)
    await db.initialize()
    await db.run_migrations()

    threads_created = 0
    messages_updated = 0
    parts_created = 0

    async with db.get_session_factory()() as session:
        conversations = (
            await session.execute(select(ConversationModel).order_by(ConversationModel.created_at))
        ).scalars().all()

        for conversation in conversations:
            existing = (
                await session.execute(
                    select(ThreadModel.id).where(
                        ThreadModel.conversation_id == conversation.id
                    )
                )
            ).first()
            if existing is not None:
                continue

            messages = (
                await session.execute(
                    select(MessageModel)
                    .where(MessageModel.conversation_id == conversation.id)
                    .order_by(MessageModel.created_at, MessageModel.id)
                )
            ).scalars().all()

            if not messages:
                continue
            if any(m.thread_id is not None for m in messages):
                continue

            thread = ThreadModel(
                id=str(uuid.uuid4()),
                tenant_id=conversation.tenant_id,
                conversation_id=conversation.id,
                status=conversation.status,
                summary_version=0,
                archived=False,
            )
            session.add(thread)

            for index, message in enumerate(messages, start=1):
                message.tenant_id = conversation.tenant_id
                message.thread_id = thread.id
                message.seq = index
                part = MessagePartModel(
                    id=str(uuid.uuid4()),
                    message_id=message.id,
                    thread_id=thread.id,
                    tenant_id=conversation.tenant_id,
                    part_type="text",
                    part_index=0,
                    content={"text": message.content},
                    redacted_content={
                        "text": message.redacted_content
                        if message.redacted_content is not None
                        else message.content
                    },
                )
                session.add(part)
                messages_updated += 1
                parts_created += 1

            threads_created += 1

        await session.commit()

    print(
        f"backfill complete: threads_created={threads_created} "
        f"messages_updated={messages_updated} parts_created={parts_created}"
    )
    await db.close()


if __name__ == "__main__":
    asyncio.run(backfill())
