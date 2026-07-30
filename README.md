# Neryva Agent Studio

Governance and control layer for LLM applications. Neryva controls the behavior, voice, scope, and workflow fit of a customer-selected LLM without replacing the model provider or the secure deployment layer.

## Architecture

This repository implements the architecture described in:

- [Reference Architecture v1.1](docs/implementation/idea.md)
- [Development Stack v1.2](docs/dev/stack.md)
- [Folder Architecture v1.0](docs/dev/folder-architecture.md)

## Repository Structure

```
neryva_studio/
├── backend/          # Python control plane (FastAPI + LangGraph)
├── frontend/         # Admin UI (React 19 + Vite + TanStack Router)
├── widget/           # Embeddable customer chat runtime
├── worker/           # Background jobs and async processing
├── ops/              # Deployment templates and infrastructure
├── evals/            # Red-team and evaluation harness
├── contracts/        # Shared API schemas and event contracts
├── packages/         # Shared cross-runtime code
└── docs/             # Architecture and implementation docs
```

## Tech Stack

### Backend
- **Language:** Python 3.13.x
- **Framework:** FastAPI >=0.115
- **Validation:** Pydantic v2
- **Orchestration:** LangGraph core >=1.2
- **Guardrails:** NeMo Guardrails + Guardrails AI
- **PII Detection:** Presidio
- **Observability:** Langfuse
- **Database:** PostgreSQL 18 + pgvector

### Frontend
- **Language:** TypeScript
- **UI Framework:** React 19
- **Bundler:** Vite >=6.x
- **Routing:** TanStack Router

### Red Teaming
- Garak (automated probing)
- PyRIT (multi-turn adversarial testing)

## Getting Started

### Prerequisites

- Python 3.13+
- Node.js 24 LTS
- pnpm 9.0+
- PostgreSQL 18+
- Redis 7+

### Backend Setup

```bash
cd /workspace
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Frontend Setup

```bash
pnpm install
```

### Run Development Servers

```bash
# Backend
uvicorn backend.app.main:app --reload

# Frontend
pnpm dev:frontend

# Widget
pnpm dev:widget
```

## Design Principles

1. **The model is not the product; the control layer is.** Works with Claude, GPT, Gemini, or compatible self-hosted providers.
2. **No single filter is a security boundary.** Layered controls with defense in depth.
3. **Multi-tenant from day one.** Each customer gets their own policy, data scope, and knowledge base.
4. **Every guardrail is also a test target.** Recurring adversarial testing required.
5. **Internal operator tooling is separate from customer runtime.** OpenCode is for internal development only.

## License

Proprietary - All rights reserved.
