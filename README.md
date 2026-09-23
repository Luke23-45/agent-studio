# Agent Studio

**Enterprise-grade governance and control layer for LLM applications.**

## Overview

 Agent Studio provides a production-ready control layer that governs the behavior, voice, scope, and workflow fit of customer-selected LLMs without replacing the model provider or secure deployment layer.

## Architecture

Built on the CoALA (Cognitive Architectures for Language Agents) framework, the system decomposes into:

- **Memory**: Tenant-specific configurations, policies, and knowledge bases
- **Action Space**: Structured tool integrations and escalation paths
- **Decision Loop**: LangGraph-based orchestration with guardrails

## Key Features

- **Multi-tenant**: Each enterprise customer gets isolated policy, data scope, and knowledge base
- **Layered Security**: PII detection (Presidio), input/output validation (NeMo Guardrails, Guardrails AI), topic filtering
- **Escalation**: Automatic human handoff when confidence is low or policy requires
- **Observability**: Langfuse integration for tracing and debugging
- **RAG**: Document ingestion, chunking, embedding, and retrieval with tenant isolation

## Project Structure

```
neryva_studio/
├── backend/app/           # Main application code
│   ├── adapters/          # External service adapters (LLM, vector store, DLP)
│   ├── api/               # FastAPI routes, middleware, dependencies
│   ├── application/       # Application services (orchestration, handoff, ingestion, retrieval, validation)
│   ├── context/           # Context engineering stack: assembler, memory, compaction (Arch §8)
│   ├── domain/            # Domain models (tenant, policy, conversation, knowledge)
│   ├── gateway/           # LLM gateway: router, fallbacks, breakers, usage, ledger, quota, caches (Arch §10)
│   ├── infrastructure/    # Infrastructure (database, cache, queue, storage)
│   ├── modules/           # Feature modules (guardrails, RAG, observability, tenant config, escalation)
│   ├── session/           # Session engine: thread store, parts, coordinator, fork (Arch §7)
│   └── settings/          # Configuration management
├── backend/tests/         # Test suite
├── contracts/             # API schemas and event contracts
├── frontend/              # Admin UI (React 19 + TanStack Router)
├── widget/                # Embeddable chat widget
└── ops/                   # Deployment configurations
```

The implementation is governed by the architecture in `docs/implementation/architecture-v2.md` (Hybrid Architecture v3) and the task ledger in `docs/implementation/ledger.md`. Where the code and the architecture conflict, the architecture wins and the conflicting implementation is replaced; aligned components are kept and extended in place.

## Quick Start

### Prerequisites

- Python 3.12+
- SQLite (dev default, zero-config) or PostgreSQL 15+ with pgvector extension (production)
- Redis 7+ (cache / queue)
- Node.js 20+ (for frontend/widget)

### Installation

```bash
# Install backend dependencies
pip install -e ".[all]"

# Set environment variables
cp .env.example .env
# Edit .env with your configuration (at least an OPENAI_API_KEY for conversations)

# Start the server
# Migrations run automatically at startup; if AUTH_ENABLED=true and no API key
# exists yet, a bootstrap super-admin key is generated and printed to the logs.
python -m uvicorn backend.app.main:app --reload
```

### First API calls

```bash
# 1. Grab the bootstrap key from the startup logs, then create a tenant:
curl -X POST http://localhost:8000/api/v1/tenants \
  -H "Content-Type: application/json" -H "X-API-Key: <bootstrap-key>" \
  -d '{"name":"Acme","slug":"acme"}'

# 2. Create an operator key scoped to the tenant (returns raw key once):
curl -X POST http://localhost:8000/api/v1/api-keys \
  -H "Content-Type: application/json" -H "X-API-Key: <bootstrap-key>" \
  -d '{"name":"widget","role":"operator","tenant_id":"<tenant-id>"}'

# 3. Send a conversation with the tenant-scoped key:
curl -X POST http://localhost:8000/api/v1/conversations \
  -H "Content-Type: application/json" -H "X-API-Key: <operator-key>" \
  -d '{"tenant_slug":"acme","message":"What can you help me with?"}'
```

### Development

```bash
# Run tests
pytest backend/tests

# Run linting
ruff check backend/
mypy backend/

# Run with coverage
pytest --cov=backend/app backend/tests
```

## OpenCode Integration

OpenCode is used internally as a **developer/operator workbench only**. It is NOT part of the production Neryva runtime.

### Internal Use Cases

- Provider connection testing (OpenAI, Anthropic, Gemini, self-hosted)
- Model behavior comparison across providers
- MCP server prototyping and tool integration experiments
- Prompt iteration and A/B testing in a sandboxed environment
- Session forking for debugging complex orchestration flows
- Replay and debug workflows using sampled production traces (after PII redaction)

### Setup

OpenCode config lives in `opencode.json` and `.opencode/` at the project root. See `AGENTS.md` for the complete boundary rules.

Boundary rules are enforced in `AGENTS.md`, `opencode.json` (permissions), and `.opencode/agents/` (agent-level restrictions). Any tool, provider connection, or workflow prototyped here must pass through the production policy gate, PII layer, and guardrail stack before entering the customer runtime.

## License
MIT
