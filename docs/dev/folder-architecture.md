# Neryva Folder Architecture v1.0

This document defines the repository layout for the Neryva Agent Studio implementation.

The goal is a modular architecture, not a pile of feature folders. Each module has a narrow responsibility, explicit boundaries, and a clear dependency direction.

---

## 1. Architecture Rules

1. **Separate runtime surfaces.** Backend control plane, admin UI, customer widget, and internal tooling live in different top-level modules.
2. **Keep domain logic isolated.** Policy, workflow, routing, and tenant configuration should not depend on framework code.
3. **External dependencies stay at the edge.** LLM providers, databases, vector stores, tracing, and ticketing systems belong in adapters.
4. **Cross-module access goes through contracts.** One module should not reach into another module's internals.
5. **Shared code must be real shared code.** If a utility is not used by multiple modules, it stays local.
6. **OpenCode stays outside the production runtime.** It is an internal operator surface, not a product module.

---

## 2. Proposed Repository Layout

```text
neryva_studio/
  docs/
    notes/
    implementation/
    dev/
  backend/
    app/
      main.py
      api/
      domain/
      application/
      adapters/
      infrastructure/
      modules/
      settings/
    tests/
  frontend/
    src/
      app/
      features/
      components/
      lib/
      styles/
    public/
  widget/
    src/
      runtime/
      api/
      ui/
  worker/
    src/
      jobs/
      pipelines/
      consumers/
  ops/
    docker/
    terraform/
    scripts/
  evals/
    garak/
    pyrith/
    prompts/
    datasets/
  contracts/
    openapi/
    events/
    schemas/
  packages/
    shared/
    types/
  .github/
    workflows/
  pyproject.toml
  package.json
  pnpm-workspace.yaml
  README.md
```

---

## 3. Top-Level Modules

### `backend/`

Python control plane for the product.

Contains:

- tenant config resolution
- policy and guardrail orchestration
- model provider adapters
- retrieval and RAG logic
- PII handling
- escalation logic
- observability hooks

### `frontend/`

Admin and operator UI.

Contains:

- tenant setup
- policy editing
- workflow inspection
- escalation queue
- trace and eval review
- admin authentication screens

### `widget/`

Embeddable customer-facing chat runtime.

Contains:

- the chat shell
- transport code
- session bootstrapping
- presentation logic

This stays thin. Business logic belongs in the backend.

### `worker/`

Background jobs and asynchronous processing.

Contains:

- eval replay jobs
- red-team runs
- document ingestion
- cleanup and retention jobs
- notification jobs

### `ops/`

Operational infrastructure.

Contains:

- deployment templates
- container definitions
- Terraform
- environment bootstrap scripts

### `evals/`

Automated safety and quality testing.

Contains:

- Garak scenarios
- PyRIT flows
- regression prompts
- synthetic tenant test data
- goldens and fixtures

### `contracts/`

Shared interfaces and schemas.

Contains:

- OpenAPI exports
- event contracts
- JSON schemas
- versioned API payload definitions

### `packages/`

Shared cross-runtime code.

Contains only code that is genuinely shared between backend, frontend, widget, or worker surfaces.

---

## 4. Backend Module Layout

The backend should use a modular-by-domain layout, not a giant flat services folder.

```text
backend/app/
  api/
    routes/
    dependencies/
    middleware/
  domain/
    tenant/
    policy/
    workflow/
    conversation/
    knowledge/
    safety/
    pii/
  application/
    orchestration/
    handoff/
    validation/
    retrieval/
    ingestion/
  adapters/
    llm/
    vectorstore/
    dlp/
    tracing/
    ticketing/
    auth/
  infrastructure/
    db/
    cache/
    queue/
    storage/
  modules/
    guardrails/
    rag/
    escalation/
    observability/
    tenant_config/
  settings/
    env.py
    feature_flags.py
```

### Backend Layer Rules

- `domain/` contains pure business concepts and invariants.
- `application/` contains use cases and orchestration logic.
- `adapters/` contains integrations with external systems.
- `infrastructure/` contains concrete persistence and runtime plumbing.
- `api/` exposes HTTP routes and request/response glue only.
- `modules/` groups the major product capabilities so they can evolve independently.

### Backend Import Direction

Allowed flow:

```text
api -> application -> domain
application -> adapters
application -> infrastructure
adapters -> infrastructure
```

Not allowed:

- `domain` importing `api`
- `domain` importing `adapters`
- `application` importing framework-specific route code
- one feature module reaching into another feature module's private files

---

## 5. Frontend Module Layout

```text
frontend/src/
  app/
    routes/
    providers/
    layout/
  features/
    tenants/
    policies/
    workflows/
    traces/
    evaluations/
    handoffs/
  components/
    ui/
    forms/
    charts/
  lib/
    api/
    auth/
    state/
    utils/
  styles/
```

### Frontend Rules

- `features/` holds user-facing business slices.
- `components/` holds reusable UI building blocks.
- `lib/` holds shared client logic, API bindings, and helpers.
- Route files should stay thin and delegate to feature modules.

---

## 6. Widget Module Layout

```text
widget/src/
  runtime/
  api/
  ui/
  state/
  transport/
```

### Widget Rules

- The widget should stay minimal and dependency-light.
- It should only handle embedding, session bootstrap, and presentation.
- It should never contain policy logic, model routing, or tenant configuration logic.

---

## 7. Worker Module Layout

```text
worker/src/
  jobs/
  pipelines/
  consumers/
  schedulers/
```

### Worker Rules

- Long-running or asynchronous tasks move here.
- Eval replay and red-team execution belong here, not in the request path.
- Cleanup and retention jobs also belong here.

---

## 8. Shared Contracts

Use `contracts/` and `packages/` carefully.

### Put in `contracts/`

- public API schemas
- event schemas
- serialized message shapes
- versioned integration payloads

### Put in `packages/`

- types or utilities shared across runtime surfaces
- serialization helpers
- date, ID, and formatting utilities

### Do not put in shared code

- business rules specific to one surface
- framework-specific components
- direct provider integrations

---

## 9. OpenCode Placement

OpenCode is not a repository module.

It is an internal operational surface used alongside this repo for:

- provider setup and comparison
- prompt iteration
- tool and MCP experiments
- session forking
- debugging and replay workflows

OpenCode sessions should be treated as operator sessions, not product sessions.

**Explicit Boundary:**

| Surface | Purpose | Part of Production Runtime? |
|---|---|---|
| OpenCode | Internal developer/operator workbench | **No** |
| Neryva Backend (`backend/`) | Customer-facing control plane | **Yes** |
| Neryva Widget (`widget/`) | Embeddable customer chat | **Yes** |
| Neryva Frontend (`frontend/`) | Admin/tenant configuration UI | **Yes** |

**What OpenCode Is Used For:**
- Testing multiple LLM provider APIs without polluting production adapters
- Comparing model behavior across providers (Claude, GPT, Gemini, self-hosted)
- Prototyping MCP servers and tool integrations before they enter the policy gate
- Session forking for debugging complex orchestration flows
- Prompt iteration and A/B testing in a sandboxed environment
- Replay and debug workflows using sampled production traces (after PII redaction)

**What OpenCode Is NOT Used For:**
- The production customer agent runtime
- Tenant session storage or state management
- Policy enforcement or guardrail evaluation
- Direct customer-facing interactions
- Bypassing Neryva's tenant policies, PII handling, or approval boundaries

**Implementation Rule:**
OpenCode must remain behind Neryva's security boundary. Any tool, provider connection, or workflow prototyped in OpenCode must pass through the production policy gate (Component P), PII layer (Component E), and guardrail stack (Component A/D) before it can be used in the customer runtime.

---

## 10. Recommended Initial Build Order

1. Create `backend/` with tenant config, policy, and orchestration modules first.
2. Add `contracts/` so backend and frontend share stable interfaces.
3. Add `frontend/` for the admin console.
4. Add `worker/` for eval replay and red-team jobs.
5. Add `widget/` after the backend session and policy model are stable.
6. Add `ops/` once deployment shape is decided.

---

## 11. Non-Negotiable Constraints

- One tenant's config must not leak into another tenant's runtime state.
- Policy enforcement must not live only in the UI.
- Prompt handling, PII, and observability must all be enforced in backend modules.
- Model-provider adapters must be swappable.
- The production runtime must remain independent from OpenCode.

---

## 12. Recommended Starting Structure

If the team wants a first implementation target, start with:

```text
backend/app/domain/
backend/app/application/
backend/app/adapters/
backend/app/api/
backend/app/settings/
contracts/
frontend/src/features/
worker/src/jobs/
evals/
```

That gives the team a modular base without overbuilding infrastructure too early.
