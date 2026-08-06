"""
RAG tenant-isolation leakage tests + citations/grounding (P1):

- Cross-tenant leakage: two tenants share ONE vector store (as in prod);
  identical documents must never cross tenant filters at any layer
  (RAGService -> RetrievalService -> orchestration `_retrieve_context`)
- knowledge_allowlist is enforced during retrieval (source allowlist)
- Citations are built from retrieved docs; faithfulness check runs on the
  generated response
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.adapters.llm import LLMProviderType
from backend.app.adapters.vectorstore import InMemoryVectorStore
from backend.app.application.orchestration import create_orchestration_service
from backend.app.application.retrieval import create_retrieval_service
from backend.app.domain.knowledge import KnowledgeDocument, KnowledgeQuery
from backend.app.domain.policy import PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.modules.grounding import FaithfulnessChecker
from backend.app.modules.rag import RAGService


class _ConstantEmbedder:
    """Everything embeds to the same vector so scores are identical;
    leakage shows up purely in the tenant/source filtering."""

    dim = 4

    def embed_text(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_text(t) for t in texts]


def _rag(store, tenant_id, embedder=None):
    return RAGService(
        vector_store=store,
        embedding_service=embedder or _ConstantEmbedder(),
        tenant_id=tenant_id,
    )


def _doc(tenant_id, source, content) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid4(),
        tenant_id=tenant_id,
        content=content,
        source=source,
    )


def _seed_documents(store, tenant_a, tenant_b):
    """Seed IDENTICAL content for both tenants — the strictest leak probe."""
    secret_a = _doc(
        tenant_a, "payroll-a.md",
        "Tenant A payroll: salaries are paid on the 1st of each month.",
    )
    secret_b = _doc(
        tenant_b, "payroll-b.md",
        "Tenant B payroll: salaries are paid on the 1st of each month.",
    )
    asyncio.run(_rag(store, tenant_a).add_documents([secret_a]))
    asyncio.run(_rag(store, tenant_b).add_documents([secret_b]))


class TestStoreAndRagIsolation:
    def test_store_filter_isolates_tenants(self):
        store = InMemoryVectorStore()
        a, b = uuid4(), uuid4()
        _seed_documents(store, a, b)
        from backend.app.adapters.vectorstore import VectorSearchConfig

        results = asyncio.run(
            store.search(
                [1.0, 0.0, 0.0, 0.0],
                VectorSearchConfig(top_k=10, filter_metadata={"tenant_id": str(a)}),
            )
        )
        assert len(results) == 1
        assert results[0].document.metadata["tenant_id"] == str(a)

    def test_rag_service_search_no_cross_tenant_leak(self):
        store = InMemoryVectorStore()
        a, b = uuid4(), uuid4()
        _seed_documents(store, a, b)

        service_a = _rag(store, a)
        results_a = asyncio.run(service_a.search(KnowledgeQuery(query_text="payroll")))
        assert len(results_a) == 1
        assert results_a[0].document.metadata["source"] == "payroll-a.md"

        service_b = _rag(store, b)
        results_b = asyncio.run(service_b.search(KnowledgeQuery(query_text="payroll")))
        assert len(results_b) == 1
        assert results_b[0].document.metadata["source"] == "payroll-b.md"

    def test_query_tenant_id_filter_when_service_unscoped(self):
        store = InMemoryVectorStore()
        a, b = uuid4(), uuid4()
        _seed_documents(store, a, b)
        service = _rag(store, None)
        results = asyncio.run(
            service.search(KnowledgeQuery(query_text="payroll", tenant_id=a))
        )
        assert len(results) == 1
        assert results[0].document.metadata["source"] == "payroll-a.md"


class TestAllowlistEnforcement:
    def test_allowed_sources_filters_results(self):
        store = InMemoryVectorStore()
        a = uuid4()
        rag = _rag(store, a)
        asyncio.run(
            rag.add_documents(
                [
                    _doc(a, "public.md", "Public pricing information."),
                    _doc(a, "secret.md", "Internal confidential strategy."),
                ]
            )
        )
        query = KnowledgeQuery(query_text="information", allowed_sources=["public.md"])
        results = asyncio.run(rag.search(query))
        assert len(results) == 1
        assert results[0].document.metadata["source"] == "public.md"

    def test_empty_allowlist_allows_all(self):
        store = InMemoryVectorStore()
        a = uuid4()
        rag = _rag(store, a)
        asyncio.run(
            rag.add_documents(
                [
                    _doc(a, "public.md", "Public pricing information."),
                    _doc(a, "secret.md", "Internal confidential strategy."),
                ]
            )
        )
        results = asyncio.run(rag.search(KnowledgeQuery(query_text="information")))
        assert len(results) == 2


class TestRetrievalServiceIsolation:
    def test_retrieve_isolates_tenants_and_cache(self):
        store = InMemoryVectorStore()
        a, b = uuid4(), uuid4()
        _seed_documents(store, a, b)
        retrieval_a = create_retrieval_service(_rag(store, a))
        retrieval_b = create_retrieval_service(_rag(store, b))

        result_a = asyncio.run(retrieval_a.retrieve("payroll", tenant_id=a))
        assert [r.document.metadata["source"] for r in result_a.results] == ["payroll-a.md"]

        # Same query text for B: the per-tenant cache must not bleed A's rows.
        result_b = asyncio.run(retrieval_b.retrieve("payroll", tenant_id=b))
        assert [r.document.metadata["source"] for r in result_b.results] == ["payroll-b.md"]
        assert result_b.total_results == 1

    def test_retrieve_post_filters_by_tenant(self):
        store = InMemoryVectorStore()
        a, b = uuid4(), uuid4()
        _seed_documents(store, a, b)
        # Deliberately disable store-level filtering: service-level filter is
        # the defense-in-depth second layer.
        retrieval = create_retrieval_service(_rag(store, None))
        result = asyncio.run(retrieval.retrieve("payroll", tenant_id=a))
        assert [r.document.metadata["tenant_id"] for r in result.results] == [str(a)]

    def test_retrieve_respects_allowed_sources(self):
        store = InMemoryVectorStore()
        a = uuid4()
        rag = _rag(store, a)
        asyncio.run(
            rag.add_documents(
                [
                    _doc(a, "public.md", "Public pricing information."),
                    _doc(a, "secret.md", "Internal confidential strategy."),
                ]
            )
        )
        retrieval = create_retrieval_service(rag)
        result = asyncio.run(
            retrieval.retrieve(
                "information", tenant_id=a, allowed_sources=["public.md"], skip_cache=True
            )
        )
        assert [r.document.metadata["source"] for r in result.results] == ["public.md"]


class TestOrchestrationRetrieval:
    def _service(self, retrieval_service=None, allowlist=None, tenant_id=None):
        tenant = TenantConfig(
            id=tenant_id or uuid4(), name="Acme", slug="acme",
            default_provider="openai", default_model="gpt-4",
            knowledge_allowlist=allowlist or [],
        )
        return tenant, create_orchestration_service(
            tenant_config=tenant,
            policy_set=PolicySet(id=uuid4(), tenant_id=tenant.id, name="default", version=1),
            llm_api_key="fake-key",
            retrieval_service=retrieval_service,
        )

    def _state(self, tenant):
        return {
            "tenant_id": tenant.id,
            "session_id": "s1",
            "user_message": "payroll question",
            "redacted_message": "payroll question",
            "context": {},
            "conversation_history": [],
            "retrieved_docs": [],
            "citations": [],
            "model_response": "Some answer",
            "validation_result": {},
            "policy_action": None,
            "confidence": 0.85,
            "handoff_required": False,
            "redact_attempts": 0,
            "budget_exceeded": False,
            "error": None,
        }

    def test_retrieve_context_only_own_tenant_docs(self):
        store = InMemoryVectorStore()
        a, b = uuid4(), uuid4()
        _seed_documents(store, a, b)
        tenant, service = self._service(
            retrieval_service=create_retrieval_service(_rag(store, a)),
            tenant_id=a,
        )
        state = self._state(tenant)
        result = asyncio.run(service._retrieve_context(state))
        sources = [d["metadata"]["source"] for d in result["retrieved_docs"]]
        assert sources == ["payroll-a.md"]
        citations = result["citations"]
        assert len(citations) == 1
        assert citations[0]["source"] == "payroll-a.md"
        assert citations[0]["score"] is not None

    def test_retrieve_context_enforces_allowlist(self):
        store = InMemoryVectorStore()
        a = uuid4()
        rag = _rag(store, a)
        asyncio.run(
            rag.add_documents(
                [
                    _doc(a, "public.md", "Public pricing information."),
                    _doc(a, "secret.md", "Internal confidential strategy."),
                ]
            )
        )
        tenant, service = self._service(
            retrieval_service=create_retrieval_service(rag),
            allowlist=["public.md"],
            tenant_id=a,
        )
        state = self._state(tenant)
        result = asyncio.run(service._retrieve_context(state))
        sources = [d["metadata"]["source"] for d in result["retrieved_docs"]]
        assert sources == ["public.md"]

    def test_retrieve_context_failure_degrades_to_empty(self):
        class _BoomRetrieval:
            async def retrieve(self, **_kwargs):
                raise RuntimeError("store down")

        tenant, service = self._service(retrieval_service=_BoomRetrieval())
        state = self._state(tenant)
        result = asyncio.run(service._retrieve_context(state))
        assert result["retrieved_docs"] == []
        assert result["citations"] == []


class TestFaithfulness:
    def test_grounded_response(self):
        sources = [
            {
                "content": "The refund policy allows returns within 30 days of purchase.",
                "score": 0.9,
                "metadata": {"source": "refunds.md"},
            }
        ]
        check = FaithfulnessChecker().check(
            "Refunds are allowed within 30 days of purchase.", sources
        )
        assert check.checked
        assert check.grounded
        assert check.score >= 0.12

    def test_ungrounded_response(self):
        sources = [
            {
                "content": "The refund policy allows returns within 30 days of purchase.",
                "score": 0.9,
                "metadata": {"source": "refunds.md"},
            }
        ]
        check = FaithfulnessChecker().check(
            "xylophone zebras are a quantum marketing trend", sources
        )
        assert check.checked
        assert not check.grounded
        assert check.matches == []

    def test_no_sources_means_not_checked(self):
        check = FaithfulnessChecker().check("anything", [])
        assert not check.checked

    def test_string_sources_supported(self):
        check = FaithfulnessChecker().check(
            "Returns are allowed within 30 days",
            ["The refund policy allows returns within 30 days."],
        )
        assert check.grounded

    def test_empty_response_skipped(self):
        check = FaithfulnessChecker().check("", [{"content": "x", "score": 1.0, "metadata": {}}])
        assert not check.checked


class _FakeAdapter:
    provider_type = LLMProviderType.OPENAI

    def __init__(self, content):
        self.content = content
        self.config = SimpleNamespace(model="gpt-4")

    async def chat(self, messages):
        return SimpleNamespace(content=self.content)


class TestOrchestrationFaithfulnessWiring:
    def _run(self, tenant, policy_set, retrieval_service, adapter):
        service = create_orchestration_service(
            tenant_config=tenant,
            policy_set=policy_set,
            llm_api_key="fake-key",
            retrieval_service=retrieval_service,
        )
        service._get_llm_adapter = lambda: adapter
        return asyncio.run(service.process_message("refund policy", "refund policy", session_id="s1"))

    def test_faithfulness_recorded_when_grounded(self):
        store = InMemoryVectorStore()
        a = uuid4()
        rag = _rag(store, a)
        asyncio.run(
            rag.add_documents(
                [
                    _doc(
                        a, "refunds.md",
                        "The refund policy allows returns within 30 days of purchase.",
                    )
                ]
            )
        )
        tenant = TenantConfig(id=a, name="Acme", slug="acme")
        policy_set = PolicySet(id=uuid4(), tenant_id=tenant.id, name="default", version=1)
        result = self._run(
            tenant,
            policy_set,
            create_retrieval_service(rag),
            _FakeAdapter("Returns are allowed within 30 days of purchase."),
        )
        faithfulness = result["context"]["faithfulness"]
        assert faithfulness["checked"] is True
        assert faithfulness["grounded"] is True
        assert len(result["citations"]) == 1
        assert result["citations"][0]["source"] == "refunds.md"

    def test_no_retrieval_skips_check(self):
        tenant = TenantConfig(id=uuid4(), name="Acme", slug="acme")
        policy_set = PolicySet(id=uuid4(), tenant_id=tenant.id, name="default", version=1)
        result = self._run(
            tenant, policy_set, None, _FakeAdapter("Just a normal answer.")
        )
        faithfulness = result["context"]["faithfulness"]
        assert faithfulness["checked"] is False
        assert faithfulness["grounded"] is True
