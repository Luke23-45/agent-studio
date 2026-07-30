# Neryva Development Stack v1.2

**Status:** Updated July 2026 with verified tool status and implementation rationale.

This document turns the architecture in [docs/implementation/idea.md](/C:/Users/Hellx/Documents/Programming/python/Project/Neryva/neryva_studio/docs/implementation/idea.md) into an implementation stack with concrete versions, licensing notes, and replacement options verified against current tool status.

---

## Language Split

| Layer | Language | Why |
|---|---|---|
| Backend / control plane | **Python 3.13.x** | LangGraph, NeMo Guardrails, Guardrails AI, Presidio, RAGAS, Garak are Python-native. The guardrail and agent ecosystem is strongest here. |
| Admin / product UI | **TypeScript** | React 19, Vite, TanStack Router, dashboard, tenant config screens, review workflows. The admin surface is a SPA, so SSR is unnecessary. |
| Internal dev tooling | **OpenCode sessions** | Provider testing, prompt iteration, MCP prototyping, session forking. Kept separate from the production runtime. |

Python 3.14 is available, but the safer move is to start on 3.13.x and upgrade after the dependency matrix is proven.

---

## Backend Stack

| Component | Choice | Version | License | Status (July 2026) |
|---|---|---|---|---|
| Web framework | **FastAPI** | >=0.115 | MIT | Standard. Pydantic v2 native. |
| Validation | **Pydantic v2** | >=2.10 | MIT | Current production line. |
| Agent orchestration | **LangGraph (core)** | >=1.2 | MIT | Very active. Production server package is separate from core. |
| Production server | **Custom FastAPI wrapper** around LangGraph core | - | - | `langgraph-api` exists, but the core + custom wrapper path keeps production licensing simpler. |
| Dialog guardrails | **NeMo Guardrails** | v0.23.0 | Apache 2.0 | Current released version in the official repo. Colang has a learning curve. |
| Output validation | **Guardrails AI** | 0.10.0 | Apache 2.0 | Pin explicitly to 0.10.0. The project advisory says not to install 0.10.1 while the PyPI quarantine is active. |
| Content safety classifier | **Llama Guard 4** (Meta) | 12B params | Custom (Meta) | Optional classifier layer inside NeMo-style safety rails. Verify before adoption. |
| PII detection | **Presidio** | v2.2.362 | MIT | Self-hosted, customizable, auditable detection. |
| Observability | **Langfuse** | >=v3.x | MIT core / EE modules | Self-hostable tracing, evals, prompt management. |
| Red-teaming (probes) | **Garak** | latest | MIT-style | Broad automated probe coverage. |
| Red-teaming (multi-turn) | **PyRIT** | latest | MIT | Crescendo-style multi-turn attacks. |
| RAG evaluation | **RAGAS** | >=v0.2 | Apache 2.0 | Reference-free metrics for faithfulness and relevance. |
| Governance classifiers | **jina-embeddings-v2-small-en** (fine-tuned) | 33M params | Apache 2.0 | Lightweight off-topic detection. |
| Governance classifiers | **stsb-roberta-base** (fine-tuned) | 110M params | MIT | Cross-encoder for higher-precision topic relevance. |
| Governance fastpath | **Custom regex engine** | - | - | Cheap first-pass topic/sentiment triage before heavier checks. |

### Guardrail Architecture: Complementary Layers

Do not choose between NeMo Guardrails and Guardrails AI. They solve different problems and compose together.

| Concern | Handled by |
|---|---|
| Dialog flow control | NeMo Guardrails |
| Topic/safety rails | NeMo Guardrails + classifier layer |
| Structured output enforcement | Guardrails AI |
| PII detection | Presidio |
| Jailbreak detection | NeMo jailbreak rail or another maintained scanner |

**Note:** LLM Guard (Protect AI) was archived July 9, 2026. Do not use it.

### LangGraph Server Licensing

The `langgraph` core library and `langgraph-api` server are separate packages. For a multi-tenant startup shipping per-customer deployments:

- **Phase 1:** Use LangGraph core + custom FastAPI server. Cost: $0 in licensing for the runtime wrapper.
- **Phase 2:** If LangGraph's managed/server offering becomes cost-effective versus self-hosting, revisit.
- **Alternative:** AG2 (`ag2ai/ag2`, Apache 2.0, independently governed) remains a fallback if LangGraph economics stop working.

---

## Frontend Stack

| Component | Choice | Version | Notes |
|---|---|---|---|
| Bundler | **Vite** | >=6.x | Fast dev server, optimized builds. |
| UI framework | **React 19** | >=19.0 | Good fit for an authenticated admin dashboard. |
| Routing | **TanStack Router** | >=1.x | Typed routing for the product UI. |
| Runtime (prod) | **Node.js 24 LTS** | 24.x | Already on the LTS line. Only needed for build tooling; deployment is static files. |
| Admin UI | Custom dashboard | - | Tenant config, agent editor, monitoring, escalation queue. |
| Customer chat widget | **Web component** (no iframe) | - | Embeddable via `<script>` tag and communicates via SSE. |

No SSR is required for the admin surface. Static SPA delivery is simpler, cheaper, and easier to reason about for this use case.

---

## Data / Storage

| Component | Choice | Version | Notes |
|---|---|---|---|
| Primary database | **PostgreSQL 18** | 18.x | Stable production baseline. PostgreSQL 19 is still in beta. |
| Vector search (default) | **pgvector** | >=v0.8 | Best default for Postgres-first tenants. |
| Vector search (scale path) | **pgvectorscale** (Timescale) | latest | Use when you need to push past plain pgvector territory without moving off Postgres. |
| Vector search (alternate) | **Qdrant** | >=v1.12 | Better fit for dedicated vector workloads or hybrid search. |
| Object storage | S3-compatible | - | Documents, exports, eval artifacts. |
| Cache / queue | **Redis** | >=v7 | Lightweight cache / job queue. |

### Vector Database Decision Flow

```text
Already on Postgres?
  -> yes -> <=5M vectors -> pgvector
         -> >5M vectors or higher throughput -> pgvectorscale
         -> need dedicated vector service -> Qdrant
  -> no  -> need zero-ops? -> Pinecone Serverless
         -> need self-host control? -> Qdrant
```

---

## Internal / Dev Tooling

| Component | Role |
|---|---|
| **OpenCode** | Internal dev workbench: provider testing, prompt iteration, MCP prototyping, session forking. Not the production runtime. |
| **GitHub Actions** | CI/CD |
| **Docker** | Container builds |
| **Terraform** | Infrastructure as code |

---

## OpenCode Role

OpenCode's current feature set makes it useful as an internal engineering tool, not as the production runtime.

Use it for:

- exercising real provider APIs without mixing that logic into the product
- testing model behavior across providers
- prototyping MCP servers and tool integrations
- session forking for debugging
- keeping internal sessions for implementation work, provider comparisons, and replay/debug flows

Do not use it for:

- the production customer agent runtime
- tenant session storage
- policy enforcement

OpenCode sessions are internal operator sessions. They are not customer sessions and they are not the Neryva runtime state store.

---

## Governance Classifier Layer

Add a lightweight pre-filter before the heavier guardrails:

```text
User Message
  -> L0: Regex Fastpath
  -> L1: Classifier Models
  -> L2: Llama Guard 4 / NeMo-style guardrails
  -> L3: Main LLM via LangGraph
```

This is meant to reduce cost and latency, not to replace the real safety boundary. The final decision still belongs to the policy and guardrail stack.

---

## Tools Not Chosen

| Tool | Reason rejected |
|---|---|
| **LangGraph API server** | Separate licensing path. Core + custom FastAPI wrapper keeps early deployment simpler. |
| **LLM Guard (Protect AI)** | Archived July 9, 2026. |
| **Promptfoo** | Ownership neutrality risk for a model-agnostic product. |
| **CrewAI** | Less deterministic for auditable customer-service workflows. |
| **AutoGen / AG2** | Viable fallback, but not the default if LangGraph economics work. |
| **Next.js** | Unnecessary for this admin dashboard. Adds SSR and framework coupling we do not need right now. |
| **Pinecone Serverless** | Strong managed option, but not the default if we want tighter infra control. |
| **Weaviate** | Not the default when simpler Postgres-first and Qdrant paths exist. |
| **Helicone** | Proxy-only tracing is not enough for the observability depth we want. |
| **LangSmith** | Useful if we go deep on LangChain stack, but less attractive if we want broader stack independence. |
| **ChromaDB** | Not the right default for multi-tenant production RAG at scale. |

---

## Final Recommendation

Proceed with:

```text
Backend:      Python 3.13.x + FastAPI + Pydantic v2
Orchestrate:  LangGraph core (MIT) + custom FastAPI server wrapper
Guardrails:   NeMo Guardrails + Guardrails AI + classifier layer
PII:          Presidio
Classifiers:  jina-embeddings-v2-small-en + stsb-roberta-base
Observability: Langfuse (self-hosted)
Red-team:     Garak + PyRIT
Database:     PostgreSQL 18 + pgvector (with pgvectorscale for growth)
Frontend:     TypeScript + React 19 + Vite + TanStack Router
Dev tool:     OpenCode (internal only)
```

This is the lowest-risk stack for the current architecture. The two main open decisions are LangGraph server economics and when, if ever, to add a dedicated vector service beyond Postgres-first storage.

---

## Primary Sources

- [Python downloads](https://www.python.org/downloads/)
- [Python source releases](https://www.python.org/downloads/source/)
- [FastAPI docs](https://fastapi.tiangolo.com/)
- [Pydantic v2 migration guide](https://docs.pydantic.dev/2.4/migration/)
- [LangGraph GitHub](https://github.com/langchain-ai/langgraph)
- [NeMo Guardrails GitHub](https://github.com/NVIDIA-NeMo/Guardrails)
- [Guardrails AI GitHub](https://github.com/guardrails-ai/guardrails)
- [Presidio GitHub](https://github.com/microsoft/presidio)
- [Langfuse GitHub](https://github.com/langfuse/langfuse)
- [Garak GitHub](https://github.com/NVIDIA/garak)
- [PyRIT GitHub](https://github.com/microsoft/PyRIT)
- [pgvector GitHub](https://github.com/pgvector/pgvector)
- [pgvectorscale (Timescale)](https://github.com/timescale/pgvectorscale)
- [Qdrant](https://qdrant.tech/)
- [Node.js releases](https://nodejs.org/en/about/previous-releases)
- [PostgreSQL releases](https://www.postgresql.org/docs/release/)
- [OpenCode docs](https://opencode.ai/v2/docs/providers)
- [OWASP Top 10 for LLM Applications 2025 (v2.0)](https://genai.owasp.org/)

## Secondary References

- [NeMo Guardrails release discussion](https://github.com/NVIDIA-NeMo/Guardrails/discussions/2117)
- [Guardrails AI security advisory](https://github.com/guardrails-ai/guardrails/blob/main/SECURITY_ADVISORY.md)
- [Node.js LTS blog](https://nodejs.org/en/blog/release/v24.11.0)
- [LangGraph vs CrewAI vs AutoGen 2026 comparison](https://devops.gheware.com/blog/posts/langgraph-vs-crewai-vs-autogen-comparison-2026.html)
- [Guardrails AI vs NeMo Guardrails comparison 2026](https://genai.qa/blog/guardrails-ai-vs-nemo-guardrails/)
- [Llama Guard 4 overview](https://huggingface.co/blog/llama-guard-4)
- [Langfuse vs LangSmith vs Helicone 2026 comparison](https://geodocs.dev/tools/langfuse-vs-langsmith-vs-helicone-agent-observability)
- [Vector DB comparison 2026](https://topreviewed.ai/blog/vector-database-comparison-2026-pinecone-vs-qdrant-vs-pgvector-vs-weaviate-at-scale)
- [Off-topic guardrail paper (arXiv 2411.12946)](https://arxiv.org/html/2411.12946v1)
