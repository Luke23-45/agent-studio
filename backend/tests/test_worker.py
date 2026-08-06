"""
Tests for the background worker (§3.4 deployment blocker):

- queue idempotency (dedupe at enqueue + processed-key skip)
- Redis-unavailable fallback to the in-memory queue
- handler registration (all 5 job types)
- real HTTP notification delivery against a local server
- email/SMS fail loudly when transports are not configured
- real embedding + indexing into the vector store
- retention cleanup (metadata + age filters)
- dead-letter sweep with requeue
- eval replay error contract
"""

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.app.adapters.vectorstore.provider import InMemoryVectorStore, VectorDocument
from backend.app.infrastructure.queue.manager import Job, QueueManager, init_queue
from backend.app.worker import handlers as worker_handlers


def _new_queue(**kwargs) -> QueueManager:
    kwargs.setdefault("redis_url", "redis://127.0.0.1:1")
    kwargs.setdefault("max_retries", 1)
    kwargs.setdefault("retry_min_delay", 0.01)
    return init_queue(**kwargs)


@pytest.fixture
def qm():
    q = _new_queue()
    yield q
    asyncio.run(q.close())


@pytest.fixture
def local_http_capture():
    """Tiny local HTTP server that records the last request body."""
    captured: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            captured["body"] = json.loads(self.rfile.read(length))
            captured["content_type"] = self.headers.get("Content-Type")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], captured
    server.shutdown()
    server.server_close()


class TestQueueIdempotency:
    def test_duplicate_enqueue_same_key_deduped(self, qm):
        asyncio.run(qm.initialize())
        job = Job(type="noop", payload={"a": 1})
        assert asyncio.run(qm.enqueue(job, idempotency_key="k1")) is True
        assert asyncio.run(qm.enqueue(job, idempotency_key="k1")) is True
        assert asyncio.run(qm.get_queue_length()) == 1

    def test_processed_key_skips_redelivery(self, qm):
        asyncio.run(qm.initialize())
        calls = []

        async def handler(payload):
            calls.append(payload)

        qm.register_handler("noop", handler)
        job = Job(type="noop", payload={"a": 1})
        asyncio.run(qm.enqueue(job, idempotency_key="k2"))
        first = asyncio.run(qm.dequeue())
        assert asyncio.run(qm.process_job(first)) is True
        second = asyncio.run(qm.dequeue())
        assert second is None
        assert len(calls) == 1

    def test_enqueue_without_key_not_deduped(self, qm):
        asyncio.run(qm.initialize())
        job = Job(type="noop", payload={})
        assert asyncio.run(qm.enqueue(job)) is True
        assert asyncio.run(qm.enqueue(job)) is True
        assert asyncio.run(qm.get_queue_length()) == 2


class TestQueueResilience:
    def test_redis_down_falls_back_to_memory(self, qm):
        # redis://127.0.0.1:1 is not listening; initialize must not raise
        asyncio.run(qm.initialize())
        assert qm._client is None
        assert asyncio.run(qm.enqueue(Job(type="noop"))) is True
        assert asyncio.run(qm.get_queue_length()) == 1

    def test_in_memory_dequeue_polls_when_empty(self, qm):
        asyncio.run(qm.initialize())
        t0 = time.monotonic()
        assert asyncio.run(qm.dequeue(timeout=0.05)) is None
        assert time.monotonic() - t0 >= 0.04


class TestHandlerRegistration:
    def test_all_job_types_registered(self, qm):
        asyncio.run(qm.initialize())
        from backend.app.worker.handlers import build_handlers

        handlers = build_handlers()
        assert set(handlers) == {
            "ingestion.process",
            "ingestion.embed",
            "notification.send",
            "cleanup.run",
            "eval.replay",
            "redteam.run",
            "webhook.deliver",
            "summary.refresh",
            "tool_result.clear",
            "memory.extract",
        }
        assert qm._handlers == handlers

    def test_default_schedule_file_loads(self):
        from pathlib import Path

        from backend.app.worker.main import load_schedule

        schedule_file = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "worker"
            / "schedule.default.json"
        )
        schedule = load_schedule(str(schedule_file))
        names = [entry["name"] for entry in schedule]
        assert "retention-sweep-daily" in names
        assert "redteam-weekly" in names
        for entry in schedule:
            assert entry["job_type"] in {
                "cleanup.run",
                "redteam.run",
                "ingestion.process",
                "notification.send",
                "eval.replay",
            }

    def test_scheduler_enqueues_scheduled_jobs(self, qm):
        asyncio.run(qm.initialize())
        from backend.app.worker.main import scheduler_loop

        seen = []

        async def handler(payload):
            seen.append(payload)

        qm.register_handler("cleanup.run", handler)
        schedule = [
            {"name": "daily", "job_type": "cleanup.run", "interval_seconds": 3600, "payload": {"limit": 10}}
        ]

        async def run_short():
            exit_event = asyncio.Event()
            loop_task = asyncio.create_task(scheduler_loop(schedule, interval=0.01, exit_event=exit_event))
            await asyncio.sleep(0.05)
            exit_event.set()
            await loop_task

        asyncio.run(run_short())
        assert asyncio.run(qm.get_queue_length()) == 1
        assert asyncio.run(qm.dequeue()) is not None


class TestNotification:
    def test_webhook_delivered_over_http(self, qm, local_http_capture):
        asyncio.run(qm.initialize())
        port, captured = local_http_capture

        async def handler(payload):
            from backend.app.worker.handlers import handle_notification_send

            await handle_notification_send(payload)

        qm.register_handler("notification.send", handler)
        job = Job(
            type="notification.send",
            payload={
                "channel": "webhook",
                "to": f"http://127.0.0.1:{port}/hook",
                "subject": "Test alert",
                "body": "hello from the worker",
                "tenant_id": "t1",
                "session_id": "s1",
            },
        )
        assert asyncio.run(qm.enqueue(job)) is True
        assert asyncio.run(qm.process_job(asyncio.run(qm.dequeue()))) is True
        assert captured["content_type"] == "application/json"
        assert captured["body"]["body"] == "hello from the worker"
        assert captured["body"]["tenant_id"] == "t1"

    def test_email_fails_loudly_without_smtp(self):
        with pytest.raises(ValueError, match="SMTP not configured"):
            asyncio.run(
                worker_handlers.handle_notification_send(
                    {"channel": "email", "to": "ops@neryva.example", "body": "x"}
                )
            )

    def test_sms_fails_loudly_without_webhook(self):
        with pytest.raises(ValueError, match="SMS delivery requires"):
            asyncio.run(
                worker_handlers.handle_notification_send(
                    {"channel": "sms", "to": "+15550001111", "body": "x"}
                )
            )


class TestIngestionEmbed:
    async def test_embed_and_index_writes_documents(self):
        store = InMemoryVectorStore()

        async def fake_embedder(texts):
            return [[0.1, 0.2, 0.3, 0.4]] * len(texts)

        from backend.app.application.ingestion.embeddings import embed_and_index

        ids = await embed_and_index(
            chunks=[
                {"id": "c0", "content": "first chunk"},
                {"id": "c1", "content": "second chunk"},
            ],
            vector_store=store,
            tenant_id="tenant-1",
            document_id="doc-1",
            source="notes.md",
            embedder=fake_embedder,
            embedding_dim=4,
        )
        assert len(ids) == 2
        doc = await store.get_document("doc-1:c0")
        assert doc is not None
        assert doc.metadata["tenant_id"] == "tenant-1"
        assert doc.metadata["document_id"] == "doc-1"
        assert doc.metadata["source"] == "notes.md"
        assert "stored_at" in doc.metadata
        assert doc.embedding == [0.1, 0.2, 0.3, 0.4]

    async def test_dimension_mismatch_raises(self):
        store = InMemoryVectorStore()

        async def wrong_dim(texts):
            return [[0.0, 0.0]] * len(texts)

        from backend.app.application.ingestion.embeddings import embed_and_index

        with pytest.raises(RuntimeError, match="dimension"):
            await embed_and_index(
                chunks=[{"id": "c0", "content": "x"}],
                vector_store=store,
                tenant_id="t",
                document_id="d",
                source="s",
                embedder=wrong_dim,
                embedding_dim=4,
            )

    def test_handler_fails_loudly_when_embedder_missing(self):
        # sentence-transformers is an optional dependency; without it the
        # embed job must fail (retry/DLQ), never silently succeed
        with pytest.raises(RuntimeError, match="sentence-transformers is not installed"):
            asyncio.run(
                worker_handlers.handle_ingestion_embed(
                    {
                        "chunks": [{"id": "c0", "content": "x"}],
                        "tenant_id": "t",
                        "document_id": "d",
                        "source": "s",
                    }
                )
            )

    def test_process_enqueues_embed_followup(self, qm):
        """ingestion.process chunks in the worker, then queues ingestion.embed."""
        asyncio.run(qm.initialize())
        embed_payloads = []

        async def embed_handler(payload):
            embed_payloads.append(payload)

        qm.register_handler("ingestion.embed", embed_handler)
        asyncio.run(
            worker_handlers.handle_ingestion_process(
                {
                    "content": "Refund policy: full refunds within 30 days. " * 30,
                    "source": "refunds.md",
                    "tenant_id": "00000000-0000-0000-0000-000000000001",
                }
            )
        )
        job = asyncio.run(qm.dequeue())
        assert job is not None
        assert job.type == "ingestion.embed"
        assert len(job.payload["chunks"]) > 1
        assert job.payload["document_id"]
        assert job.payload["tenant_id"] == "00000000-0000-0000-0000-000000000001"
        assert job.payload["source"] == "refunds.md"

    def test_process_embed_followup_is_idempotent(self, qm):
        """Re-running the same process job (same _idem) enqueues embed once."""
        asyncio.run(qm.initialize())
        qm.register_handler("ingestion.embed", lambda payload: None)
        payload = {
            "content": "Chunk me. " * 200,
            "source": "dup.md",
            "tenant_id": "00000000-0000-0000-0000-000000000002",
            "_idem": "ingest:t:dup.md",
        }
        asyncio.run(worker_handlers.handle_ingestion_process(payload))
        asyncio.run(worker_handlers.handle_ingestion_process(payload))
        assert asyncio.run(qm.get_queue_length()) == 1
        job = asyncio.run(qm.dequeue())
        assert job.type == "ingestion.embed"
        assert asyncio.run(qm.get_queue_length()) == 0


class TestCleanup:
    async def _seeded_store(self):
        store = InMemoryVectorStore()
        now = time.time()
        await store.add_documents(
            [
                VectorDocument(
                    id="a:0", content="a", embedding=[0.1] * 4,
                    metadata={"tenant_id": "t1", "document_id": "a", "stored_at": now - 7200},
                ),
                VectorDocument(
                    id="a:1", content="a", embedding=[0.1] * 4,
                    metadata={"tenant_id": "t1", "document_id": "a", "stored_at": now - 100},
                ),
                VectorDocument(
                    id="b:0", content="b", embedding=[0.1] * 4,
                    metadata={"tenant_id": "t2", "document_id": "b", "stored_at": now - 7200},
                ),
            ]
        )
        return store

    async def test_cleanup_by_tenant_and_age(self):
        from backend.app.application.cleanup.service import CleanupService

        store = await self._seeded_store()
        q = _new_queue()
        await q.initialize()
        service = CleanupService(vector_store=store, queue_manager=q)
        deleted = await service.cleanup_vectors(
            tenant_id="t1", older_than_seconds=3600, document_id="a"
        )
        # stored_at 9000 is "newer" than now - 3600; only 1000.0 is older
        assert deleted == 1
        assert len(store) == 2
        await q.close()

    async def test_cleanup_without_filter_and_age_raises(self):
        from backend.app.application.cleanup.service import CleanupService

        store = await self._seeded_store()
        q = _new_queue()
        await q.initialize()
        service = CleanupService(vector_store=store, queue_manager=q)
        with pytest.raises(ValueError, match="requires a metadata filter"):
            await service.cleanup_vectors(older_than_seconds=3600)
        await q.close()

    def test_dead_letter_sweep_requeues(self, qm):
        asyncio.run(qm.initialize())

        async def failing_handler(payload):
            raise RuntimeError("boom")

        qm.register_handler("failing", failing_handler)
        job = Job(type="failing", payload={}, max_retries=1)
        asyncio.run(qm.enqueue(job))
        first = asyncio.run(qm.dequeue())
        assert asyncio.run(qm.process_job(first)) is False
        assert asyncio.run(qm.get_dead_letter_count()) == 1

        from backend.app.application.cleanup.service import CleanupService

        service = CleanupService(vector_store=InMemoryVectorStore(), queue_manager=qm)
        report = asyncio.run(service.sweep_dead_letter(requeue=True))
        assert report["drained"] == 1
        assert report["requeued"] == 1
        assert asyncio.run(qm.get_dead_letter_count()) == 0
        assert asyncio.run(qm.get_queue_length()) == 1

    def test_cleanup_run_sweeps_without_filters(self, qm, monkeypatch):
        """Scheduled sweep-only cleanup.run: vectors skipped, DLQ swept."""
        asyncio.run(qm.initialize())

        async def failing_handler(payload):
            raise RuntimeError("boom")

        qm.register_handler("failing", failing_handler)
        job = Job(type="failing", payload={}, max_retries=1)
        asyncio.run(qm.enqueue(job))
        first = asyncio.run(qm.dequeue())
        assert asyncio.run(qm.process_job(first)) is False
        assert asyncio.run(qm.get_dead_letter_count()) == 1

        monkeypatch.setattr(
            "backend.app.adapters.vectorstore.create_vector_store_from_settings",
            lambda: InMemoryVectorStore(),
        )
        asyncio.run(
            worker_handlers.handle_cleanup_run(
                {"limit": 100, "requeue_dead_letter": True}
            )
        )
        assert asyncio.run(qm.get_dead_letter_count()) == 0
        assert asyncio.run(qm.get_queue_length()) == 1


class TestEscalationNotifications:
    def _service(self, **kwargs):
        from uuid import uuid4

        from backend.app.domain.policy import PolicyAction
        from backend.app.domain.tenant import TenantConfig
        from backend.app.modules.escalation import create_escalation_service

        tenant = TenantConfig(id=uuid4(), name="Acme", slug="acme")
        return create_escalation_service(tenant, **kwargs)

    async def _handoff(self, service):
        from backend.app.domain.policy import PolicyAction

        return await service.create_handoff(
            session_id="s1",
            user_message="I need a human",
            confidence=0.2,
            reason="low confidence",
            policy_action=PolicyAction.ESCALATE,
            conversation_history=[],
        )

    def test_handoff_enqueues_notification(self, qm):
        asyncio.run(qm.initialize())
        service = self._service(
            notification_channel="webhook",
            notification_target="https://ops.example/hook",
        )
        asyncio.run(self._handoff(service))
        job = asyncio.run(qm.dequeue())
        assert job is not None
        assert job.type == "notification.send"
        assert job.payload["to"] == "https://ops.example/hook"
        assert job.payload["metadata"]["category"] == "escalation"
        assert job.payload["session_id"] == "s1"

    def test_two_handoffs_enqueue_two_notifications(self, qm):
        asyncio.run(qm.initialize())
        service = self._service(
            notification_channel="webhook",
            notification_target="https://ops.example/hook",
        )
        asyncio.run(self._handoff(service))
        asyncio.run(self._handoff(service))
        assert asyncio.run(qm.get_queue_length()) == 2
        first = asyncio.run(qm.dequeue())
        second = asyncio.run(qm.dequeue())
        assert first.id != second.id
        assert first.payload["metadata"]["handoff_id"] != second.payload["metadata"]["handoff_id"]

    def test_handoff_without_target_enqueues_nothing(self, qm):
        asyncio.run(qm.initialize())
        service = self._service(notification_channel="email", notification_target=None)
        asyncio.run(self._handoff(service))
        assert asyncio.run(qm.get_queue_length()) == 0

    def test_handoff_without_queue_skipped_silently(self):
        from backend.app.domain.policy import PolicyAction

        service = self._service(
            notification_channel="webhook",
            notification_target="https://ops.example/hook",
        )
        result = asyncio.run(
            service.create_handoff(
                session_id="s1",
                user_message="help",
                confidence=0.1,
                reason="low",
                policy_action=PolicyAction.ESCALATE,
                conversation_history=[],
            )
        )
        assert result.success is True


class TestEvalReplay:
    def test_missing_conversation_raises_replay_error(self, tmp_path):
        from backend.app.application.eval_replay.service import EvalReplayService

        class _Repo:
            async def get_by_session(self, tenant_id, session_id):
                return None

        service = EvalReplayService(db=None, repository_factory=lambda r: _Repo())
        with pytest.raises(Exception, match="Conversation not found"):
            asyncio.run(
                service.replay_conversation(tenant_id="t1", session_id="s1")
            )
