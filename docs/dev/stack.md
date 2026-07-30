# Neryva Development Stack v1.1

**Status:** Updated July 2026 with verified tool status and decision rationale.

This document turns the architecture from `docs/implementation/idea.md` into an implementation stack with specific versions, licensing notes, and replacement options verified against current (July 2026) tool status.

---

## Language Split

| Layer | Language | Why |
|---|---|---|
| Backend / control plane | **Python 3.13.x** | LangGraph, NeMo Guardrails, Guardrails AI, Presidio, RAGAS, Garak — all Python-native. The guardrail/agent ecosystem is strongest here. |
| Admin / product UI | **TypeScript** | Next.js 16, dashboard, tenant config screens, review workflows. Web UI lives here. |
| Internal dev tooling | **TypeScript (OpenCode)** | Provider testing, prompt iteration, MCP prototyping. Kept separate from production runtime. |

Python 3.14 is available but untested across the dependency matrix. Start on 3.13.x and upgrade after the stack is proven.

---

## Backend Stack (Python)

| Component | Choice | Version | License | Status (July 2026) |
|---|---|---|---|---|
| Web framework | **FastAPI** | ≥0.115 | MIT | ✅ Standard. Pydantic v2 native. |
| Validation | **Pydantic v2** | ≥2.10 | MIT | ✅ |
| Agent orchestration | **LangGraph (core)** | ≥0.3 | MIT | ✅ Consensus choice for production. 90K+ stars. Used at Uber, LinkedIn, Klarna. |
| Production server | **Custom FastAPI wrapper** around LangGraph core | — | — | ⚠️ `langgraph-api` server is Elastic 2.0 (needs commercial key). Build a thin FastAPI wrapper around the free LangGraph core instead. |
| Dialog guardrails | **NeMo Guardrails** | v0.22.0 (May 2025) | Apache 2.0 | ⚠️ v0.23.0 not yet released as of July 2026. Verify breakage risk. Colang DSL has a learning curve. |
| Output validation | **Guardrails AI** | ≥v0.10.0 (Apr 2026) | Apache 2.0 | ✅ 6.6K stars. 60+ community validators. Best for structured output enforcement. |
| Content safety classifier | **Llama Guard 4** (Meta) | 12B params | Custom (Meta) | ✅ Newest in the Llama Guard family. Used as classifier inside NeMo's content-safety rail. ~459ms per check. |
| PII detection | **Presidio** | v2.2.362 (Mar 2026) | MIT | ✅ 8.8K stars. OpenSSF Best Practices badge. Self-hosted. Custom recognizers for tenant-specific PII. |
| Observability | **Langfuse** | ≥v3.x | MIT core / EE modules | ✅ 27.2K stars. ClickHouse-affiliated since Jan 2026. OTel-native. Self-hostable. |
| Red-teaming (probes) | **Garak** | latest | MIT-style | ✅ ~8K stars. Individual-led (Leon Derczynski). Broad automated probe coverage. |
| Red-teaming (multi-turn) | **PyRIT** | latest | MIT | ✅ 4K stars. Microsoft. Crescendo-style multi-turn attacks. |
| RAG evaluation | **RAGAS** | ≥v0.2 | Apache 2.0 | ✅ Reference-free metrics for faithfulness, relevance. |
| Governance classifiers | **jina-embeddings-v2-small-en** (fine-tuned) | 33M params | Apache 2.0 | ✅ For off-topic detection. Trainable per tenant. ~3ms inference on CPU. |
| Governance classifiers | **stsb-roberta-base** (fine-tuned) | 110M params | MIT | ✅ Cross-encoder for high-precision topic relevance. ~8ms inference. |
| Governance fastpath | **Custom regex engine** | — | — | Implements Claude Code-style userPromptKeywords patterns for zero-cost sentiment/topic detection. |

### Guardrail Architecture: Two Complementary Tools

Do not choose between NeMo Guardrails and Guardrails AI — they solve different problems and compose together:

| Concern | Handled by |
|---|---|
| Dialog flow control (Colang) | NeMo Guardrails |
| Topic/safety rails | NeMo Guardrails + Llama Guard 4 |
| Structured output enforcement | Guardrails AI |
| PII detection | Presidio (wraps both) |
| Jailbreak detection | Lakera Guard or NeMo's jailbreak rail |

**Note:** LLM Guard (Protect AI) was **archived July 9, 2026** — do not use. It was listed in earlier research but is now explicitly unmaintained.

### LangGraph Server Licensing

The `langgraph` core library (MIT) and `langgraph-api` server (Elastic 2.0) are separate packages. For a multi-tenant startup shipping per-customer deployments:

- **Phase 1:** Use LangGraph core + custom FastAPI server. Cost: $0.
- **Phase 2:** If LangGraph's managed platform becomes cost-effective vs. self-hosting, migrate. Get a quote from LangChain Inc before locking in.
- **Alternative:** AG2 (ag2ai/ag2, Apache 2.0, independently governed since Nov 2024) as fallback if LangGraph licensing math doesn't work.

---

## Frontend Stack (TypeScript)

| Component | Choice | Version | Notes |
|---|---|---|---|
| Framework | **Next.js 16** | ≥16.0 | Standard for React admin UIs in 2026. |
| Runtime (prod) | **Node.js 24 LTS** | 24.x | October 2026 LTS. |
| Runtime (dev) | **Node.js 26** | 26.x | OK for experimentation, not production. |
| Admin UI | Custom dashboard | — | Tenant config, agent editor, monitoring, escalation queue. |
| Customer chat widget | **Web component** (no iframe) | — | Embeddable via `<script>` tag. Communicates via SSE. |

---

## Data / Storage

| Component | Choice | Version | Notes |
|---|---|---|---|
| Primary database | **PostgreSQL 18** | 18.x | ✅ Stable. PG 19 still in beta. |
| Vector search (default) | **pgvector** | ≥v0.8 | ✅ Best for ≤5M vectors. Zero new infra. ACID with app data. |
| Vector search (scale) | **pgvectorscale** (Timescale) | latest | ✅ StreamingDiskANN extends pgvector to 50M+ vectors without dedicated infra. |
| Vector search (upgrade path) | **Qdrant** | ≥v1.12 | ⚠️ For >10M vectors or hybrid search needs. Rust-native, 99.2% recall at 1M. Self-host or managed cloud. |
| Object storage | S3-compatible | — | Documents, exports, eval artifacts. |
| Cache / queue | **Redis** | ≥v7 | Lightweight cache / job queue. |

### Vector Database Decision Flow

```
Already on Postgres? ──YES──→ ≤5M vectors? ──YES──→ pgvector (stop)
                                │
                                NO
                                ├──→ pgvectorscale (up to 50M)
                                └──→ >10M or hybrid search needed? ──→ Qdrant

Not on Postgres yet? ──→ Need zero-ops? ──YES──→ Pinecone Serverless
                              │
                              NO
                              └──→ Need max performance? ──→ Qdrant
```

---

## Internal / Dev Tooling

| Component | Role |
|---|---|
| **OpenCode** | Internal dev workbench: provider testing, prompt iteration, MCP prototyping, session forking. NOT the production runtime. |
| **GitHub Actions** | CI/CD |
| **Docker** | Container builds |
| **Terraform** | Infrastructure as code |

---

## OpenCode Role (Refined)

OpenCode's current feature set (multi-provider, custom models, tools/permissions/plugins, MCP support, headless API) makes it ideal as an **internal engineering tool**, not as the production runtime.

**Use it for:**
- Exercising real provider APIs without mixing that logic into the product
- Testing model behavior across providers (Claude vs GPT vs Gemini comparison)
- Prototyping new MCP servers and tool integrations
- Session forking for debugging

**Do NOT use it for:**
- The production customer agent runtime
- Tenant session storage
- Policy enforcement (that's Neryva's governance engine)

---

## Governance Classifier Layer (NEW)

One critical gap in the prior stack: there is no lightweight pre-filter before the heavy guardrails. Add this as a dedicated layer:

```
User Message
    │
    ▼
[L0: Regex Fastpath]  ← 0ms, $0. Catches obvious off-topic/angry.
    │ (uncertain)
    ▼
[L1: Classifier Models]  ← 3-8ms, ~$0.00001. Fine-tuned 33M-110M param models.
    │ (uncertain)
    ▼
[L2: Llama Guard 4 / NeMo]  ← 100-459ms, ~$0.0002. Heavy guardrails.
    │
    ▼
[L3: Main LLM via LangGraph]
```

The lightweight classifiers are fine-tuned per tenant on synthetic data (training cost: <$5 per tenant, one-time). They absorb ~90% of off-topic queries before the heavy stack runs.

---

## Tools NOT Chosen (with reasons)

| Tool | Reason rejected |
|---|---|
| **LangGraph API server** | Elastic 2.0 license. Commercial key needed for production self-hosting. Using core + custom wrapper instead. |
| **LLM Guard (Protect AI)** | **Archived July 9, 2026.** No longer maintained. |
| **Promptfoo** | Acquired by OpenAI (Mar 2026). Model-neutrality concern. Use Garak + PyRIT instead. |
| **CrewAI** | Role-based model hits ceiling at 6-12 months. Teams report needing to rewrite to LangGraph later. |
| **AutoGen / AG2** | Version fragmentation. Less deterministic workflows. Not ideal for auditable customer-service agents. |
| **Pinecone Serverless** | Vendor lock-in. No HNSW tuning knobs. 95% recall vs Qdrant's 99.2% at 1M vectors. More expensive at scale. |
| **Weaviate** | Schema rigidity. Resource-intensive. Lower QPS than Qdrant at equivalent hardware. |
| **Helicone** | Proxy approach has shallower trace depth. Less eval tooling than Langfuse. |
| **LangSmith** | Requires LangChain stack for auto-tracing. SaaS-only (no self-host). More expensive at scale than Langfuse. |
| **ChromaDB** | Not production-grade for multi-tenant RAG at scale. |
| **NeMo Guardrails alone** | Needs Guardrails AI alongside it for structured output validation. They are complementary, not alternatives. |

---

## Final Recommendation

Proceed with:

```
Backend:     Python 3.13.x + FastAPI + Pydantic v2
Orchestrate: LangGraph core (MIT) + custom FastAPI server wrapper
Guardrails:  NeMo Guardrails + Guardrails AI + Llama Guard 4
PII:         Presidio
Classifiers: jina-embeddings-v2-small-en (33M) + stsb-roberta-base (110M)
Observability: Langfuse (self-hosted)
Red-team:    Garak + PyRIT
Database:    PostgreSQL 18 + pgvector (with pgvectorscale for growth)
Frontend:    TypeScript + Next.js 16 + Node.js 24 LTS
Dev tool:    OpenCode (internal only)
```

All tool decisions are grounded in verified July 2026 status — licenses, community activity, and documented production deployments. The most consequential open decision is the LangGraph server licensing, which is deferred to Phase 2 by using a custom FastAPI wrapper in Phase 1.

## Primary Sources

- [Python downloads](https://www.python.org/downloads/)
- [FastAPI docs](https://fastapi.tiangolo.com/)
- [Pydantic v2 migration guide](https://docs.pydantic.dev/2.4/migration/)
- [LangGraph GitHub](https://github.com/langchain-ai/langgraph) — 90K+ stars, MIT/Elastic 2.0
- [LangGraph vs CrewAI vs AutoGen 2026 comparison](https://devops.gheware.com/blog/posts/langgraph-vs-crewai-vs-autogen-comparison-2026.html)
- [NeMo Guardrails GitHub](https://github.com/NVIDIA-NeMo/Guardrails) — v0.22.0 (May 2025), Apache 2.0
- [Guardrails AI GitHub](https://github.com/guardrails-ai/guardrails) — 6.6K stars, v0.10.0 (Apr 2026)
- [Guardrails AI vs NeMo Guardrails comparison 2026](https://genai.qa/blog/guardrails-ai-vs-nemo-guardrails/)
- [Llama Guard 4 on Hugging Face](https://huggingface.co/blog/llama-guard-4) — 12B safety classifier, 2026
- [Presidio GitHub](https://github.com/microsoft/presidio) — 8.8K stars, MIT, OpenSSF-badged
- [Langfuse GitHub](https://github.com/langfuse/langfuse) — 27.2K stars, MIT/EE, ClickHouse-affiliated
- [Langfuse vs LangSmith vs Helicone 2026 comparison](https://geodocs.dev/tools/langfuse-vs-langsmith-vs-helicone-agent-observability)
- [Garak GitHub](https://github.com/NVIDIA/garak) — ~8K stars, LLM probe framework
- [PyRIT GitHub](https://github.com/microsoft/PyRIT) — 4K stars, MIT, multi-turn red-teaming
- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [pgvectorscale (Timescale)](https://github.com/timescale/pgvectorscale) — StreamingDiskANN for large-scale pgvector
- [Vector DB comparison 2026](https://topreviewed.ai/blog/vector-database-comparison-2026-pinecone-vs-qdrant-vs-pgvector-vs-weaviate-at-scale)
- [Qdrant](https://qdrant.tech/) — 99.2% recall at 1M vectors, hybrid search
- [Next.js docs](https://nextjs.org/docs)
- [Node.js releases](https://nodejs.org/en/about/previous-releases)
- [PostgreSQL releases](https://www.postgresql.org/docs/release/)
- [OpenCode docs](https://opencode.ai/v2/docs/providers)
- [Off-topic guardrail paper (arXiv 2411.12946)](https://arxiv.org/html/2411.12946v1) — 33M-param model beats GPT-4 at off-topic detection
- [OWASP Top 10 for LLM Applications 2025 (v2.0)](https://genai.owasp.org/)
