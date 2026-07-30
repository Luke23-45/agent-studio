# Neryva Agent Studio

**Enterprise-grade governance and control layer for LLM applications.**

## Overview

Neryva Agent Studio provides a production-ready control layer that governs the behavior, voice, scope, and workflow fit of customer-selected LLMs without replacing the model provider or secure deployment layer.

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
│   ├── domain/            # Domain models (tenant, policy, conversation, knowledge)
│   ├── infrastructure/    # Infrastructure (database, cache, queue, storage)
│   ├── modules/           # Feature modules (guardrails, RAG, observability, tenant config, escalation)
│   └── settings/          # Configuration management
├── backend/tests/         # Test suite
├── contracts/             # API schemas and event contracts
├── frontend/              # Admin UI (React 19 + TanStack Router)
├── widget/                # Embeddable chat widget
├── worker/                # Background job processor
└── ops/                   # Deployment configurations
```

## Quick Start

### Prerequisites

- Python 3.12+
- PostgreSQL 15+ with pgvector extension
- Redis 7+
- Node.js 20+ (for frontend/widget)

### Installation

```bash
# Install backend dependencies
pip install -e ".[all]"

# Set environment variables
cp .env.example .env
# Edit .env with your configuration

# Run migrations
# (when implemented)

# Start the server
python -m uvicorn backend.app.main:app --reload
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

OpenCode is used internally for:
- MCP experiments and server prototyping
- Provider connection testing
- Prompt iteration
- Tool integration testing
- Session forking and replay/debug workflows

**Note**: OpenCode is NOT part of the production runtime. It remains an internal developer/operator surface.

## License

Proprietary - All rights reserved.
