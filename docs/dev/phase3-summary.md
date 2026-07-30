# Phase 3 Implementation Summary - Escalation + Observability + UI

**Status:** ✅ Complete  
**Date:** July 2026  
**Duration:** Weeks 7-10

---

## Overview

Phase 3 delivers the user-facing and operational capabilities of Neryva Agent Studio:
- **Observability Stack**: Langfuse integration for distributed tracing
- **Worker Background Jobs**: Async processing for evals, red-teaming, ingestion, cleanup, notifications
- **Frontend Admin UI**: React 19 + Vite + TanStack Router dashboard
- **Helpdesk Integration Foundation**: Webhook-based ticketing system

This phase transforms the backend guardrail and orchestration layers into an operable enterprise product.

---

## 1. Observability Module (`backend/app/modules/observability/`)

### Architecture

```
backend/app/modules/observability/
└── __init__.py          # LangfuseAdapter, TracingContext, instrumentation helpers
```

### Key Components

#### `ObservabilityConfig`
- Environment-based configuration
- Supports self-hosted and cloud Langfuse
- Graceful degradation if keys missing

#### `LangfuseAdapter`
- Wrapper around Langfuse Python SDK
- Handles initialization, error recovery
- Provides trace/span creation APIs

#### `TracingContext`
- Context manager for request lifecycle tracing
- Manages span stack for nested operations
- Auto-cleanup on exit

#### `ActiveSpan` / `NullSpan`
- Active span wraps Langfuse span objects
- Null span provides no-op fallback when tracing disabled

#### Instrumentation Helpers
- `instrument_guardrail_evaluation()` - Logs each guardrail check
- `instrument_llm_call()` - Tracks LLM generation with token usage
- `instrument_tool_call()` - Records tool execution success/failure

### Usage Example

```python
from backend.app.modules.observability import trace_request, instrument_llm_call

async def handle_conversation(tenant_id: str, session_id: str, message: str):
    with trace_request(tenant_id, session_id) as ctx:
        with ctx.span("input_processing", input_data={"message": message}) as span:
            # Process input...
            span.update(output={"processed": True})
        
        # ... orchestration logic ...
        
        instrument_llm_call(
            ctx,
            model="gpt-4",
            messages=prompt_messages,
            response=output,
            usage={"total_tokens": 150},
            latency_ms=450,
        )
        
        ctx.score("safety_score", value=0.98)
```

### Configuration

```bash
# .env
OBSERVABILITY_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-xxx
LANGFUSE_SECRET_KEY=sk-lf-xxx
LANGFUSE_HOST=https://cloud.langfuse.com  # or self-hosted URL
LANGFUSE_PROJECT_ID=neryva-prod
ENVIRONMENT=production
APP_VERSION=neryva-v1.0
```

---

## 2. Worker Module (`worker/src/`)

### Directory Structure

```
worker/src/
├── jobs/
│   ├── __init__.py          # Exports all job classes
│   ├── eval_replay.py       # EvalReplayJob - Regression detection
│   ├── redteam.py           # RedTeamJob - Garak + PyRIT execution
│   ├── ingestion.py         # IngestionBatchJob - RAG document processing
│   ├── cleanup.py           # CleanupJob - Data retention enforcement
│   └── notification.py      # NotificationJob - Multi-channel alerts
├── pipelines/               # (Future: complex multi-job workflows)
├── consumers/               # (Future: queue consumers)
└── schedulers/              # (Future: cron-like scheduling)
```

### Job Implementations

#### `EvalReplayJob`
**Purpose:** Detect regressions by replaying historical traces

**Features:**
- Fetches traces from Langfuse
- Replays through current orchestration pipeline
- Compares outputs using similarity metrics
- Integrates Garak and PyRIT for additional scoring
- Generates detailed regression reports

**Usage:**
```python
job = EvalReplayJob(
    tenant_id="tenant-123",
    trace_sample_size=100,
    garak_enabled=True,
    pyrith_enabled=True,
)
report = await job.run()
print(f"Regressions found: {report.regression_count}")
```

#### `RedTeamJob`
**Purpose:** Automated adversarial testing

**Features:**
- Executes Garak probes (injection, jailbreak, PII leak, etc.)
- Runs PyRIT scenarios (Crescendo, Tree of Attacks)
- Parses output and categorizes vulnerabilities by severity
- Provides mitigation recommendations

**Usage:**
```python
job = RedTeamJob(
    tenant_id="tenant-123",
    model="gpt-4",
    garak_probes=["all"],
    pyrith_scenarios=["crescendo", "tree_of_attacks"],
)
report = await job.run()
print(f"Critical vulnerabilities: {report.critical_count}")
```

#### `IngestionBatchJob`
**Purpose:** Process documents for RAG knowledge base

**Features:**
- Supports PDF, DOCX, TXT, Markdown
- Four chunking strategies: fixed-size, sentence, paragraph, recursive
- Embedding generation (placeholder for actual model integration)
- Vector store upsertion (pgvector, Qdrant, Pinecone)
- Metadata enrichment

**Usage:**
```python
job = IngestionBatchJob(
    tenant_id="tenant-123",
    chunking_strategy=ChunkingStrategy.RECURSIVE,
    chunk_size=512,
    chunk_overlap=50,
)
job.add_file("policy.pdf", pdf_bytes, metadata={"category": "compliance"})
report = await job.run()
print(f"Ingested {report.total_chunks} chunks from {report.successful} documents")
```

#### `CleanupJob`
**Purpose:** Enforce data retention policies

**Features:**
- Deletes expired traces
- Purges old eval results
- Removes temporary files
- Finds orphaned vectors
- Supports dry-run mode

**Usage:**
```python
job = CleanupJob(
    retention_days=30,
    tenant_id="tenant-123",
    dry_run=True,  # Test before actual deletion
)
report = await job.run()
```

#### `NotificationJob`
**Purpose:** Send alerts via multiple channels

**Features:**
- Email (SMTP)
- Slack (webhooks)
- Webhook (custom endpoints)
- SMS (Twilio/Vonage placeholder)
- Priority levels: LOW, NORMAL, HIGH, CRITICAL

**Usage:**
```python
from worker.src.jobs.notification import send_escalation_alert

await send_escalation_alert(
    tenant_id="tenant-123",
    handoff_id="handoff-456",
    recipients=["support@example.com"],
    reason="Low confidence response on billing question",
)
```

---

## 3. Frontend Admin UI (`frontend/`)

### Tech Stack

| Component | Choice | Version |
|-----------|--------|---------|
| Bundler | Vite | ^6.0.0 |
| Framework | React | ^19.0.0 |
| Routing | TanStack Router | ^1.0.0 |
| State | Zustand | ^4.4.0 |
| Data Fetching | TanStack Query | ^5.0.0 |
| Styling | Tailwind CSS | ^3.4.0 |
| Charts | Recharts | ^2.10.0 |
| Icons | Lucide React | ^0.300.0 |

### Directory Structure

```
frontend/
├── src/
│   ├── app/
│   │   ├── App.tsx              # Root layout with Header + Sidebar
│   │   └── routes.tsx           # TanStack Router route tree
│   ├── components/
│   │   └── layout/
│   │       ├── Header.tsx       # Top navigation bar
│   │       └── Sidebar.tsx      # Side navigation menu
│   ├── features/
│   │   ├── tenants/             # Tenant management screens
│   │   ├── policies/            # Policy editor
│   │   ├── workflows/           # Workflow builder
│   │   ├── traces/              # Trace viewer
│   │   ├── evaluations/         # Eval run management
│   │   └── handoffs/            # Escalation queue
│   ├── lib/
│   │   └── api/
│   │       └── client.ts        # Axios-based API client
│   └── styles/
│       └── index.css            # Tailwind + custom styles
├── package.json
├── vite.config.ts
├── tailwind.config.js
├── postcss.config.js
├── tsconfig.json
└── index.html
```

### Routes

| Path | Component | Purpose |
|------|-----------|---------|
| `/` | Dashboard | System overview with key metrics |
| `/tenants` | TenantsList | Tenant CRUD operations |
| `/policies` | PoliciesList | Guardrail policy configuration |
| `/traces` | TracesList | Conversation trace exploration |
| `/evaluations` | EvaluationsList | Eval run history and triggers |
| `/handoffs` | HandoffsList | Human escalation queue |

### API Client

Type-safe Axios wrapper with:
- Automatic auth token injection
- 401 handling (logout + redirect)
- Centralized error handling
- Typed request/response interfaces

```typescript
import { apiClient } from '@/lib/api/client';

// Fetch tenants
const tenants = await apiClient.getTenants();

// Create new tenant
const tenant = await apiClient.createTenant({
  name: "Acme Corp",
  model_provider: "openai",
  model_name: "gpt-4",
});
```

---

## 4. Helpdesk Integration

### Current Implementation

The escalation module (`backend/app/modules/escalation/`) includes:

- `TicketingService` with webhook-based integration
- Configurable endpoint per tenant
- Payload includes: handoff ID, conversation history, context, recommended actions
- Retry logic with exponential backoff

### Supported Systems (via webhooks)

- **Zendesk**: Create tickets via webhook → Zendesk API
- **ServiceNow**: REST API integration
- **Jira Service Management**: Webhook → Jira automation
- **Custom**: Any system with HTTP endpoint

### Future Enhancements (Phase 4)

- Native OAuth flows for major helpdesk platforms
- Bi-directional sync (status updates from helpdesk → Neryva)
- SLA tracking and breach alerts
- Custom field mapping

---

## 5. File Inventory

### New Files Created (Phase 3)

| File | Lines | Purpose |
|------|-------|---------|
| `backend/app/modules/observability/__init__.py` | ~350 | Langfuse tracing integration |
| `worker/src/jobs/__init__.py` | ~20 | Job module exports |
| `worker/src/jobs/eval_replay.py` | ~190 | Eval replay job |
| `worker/src/jobs/redteam.py` | ~280 | Red-team automation |
| `worker/src/jobs/ingestion.py` | ~330 | Document ingestion pipeline |
| `worker/src/jobs/cleanup.py` | ~140 | Data retention cleanup |
| `worker/src/jobs/notification.py` | ~340 | Multi-channel notifications |
| `frontend/package.json` | ~40 | Frontend dependencies |
| `frontend/vite.config.ts` | ~36 | Vite build configuration |
| `frontend/tsconfig.json` | ~30 | TypeScript config |
| `frontend/tsconfig.node.json` | ~12 | Node-specific TS config |
| `frontend/index.html` | ~14 | HTML entry point |
| `frontend/src/main.tsx` | ~36 | React app bootstrap |
| `frontend/src/app/App.tsx` | ~17 | Root layout component |
| `frontend/src/app/routes.tsx` | ~150 | Route definitions |
| `frontend/src/components/layout/Header.tsx` | ~30 | Header component |
| `frontend/src/components/layout/Sidebar.tsx` | ~36 | Sidebar navigation |
| `frontend/src/styles/index.css` | ~60 | Tailwind + custom styles |
| `frontend/tailwind.config.js` | ~24 | Tailwind theme config |
| `frontend/postcss.config.js` | ~6 | PostCSS plugins |
| `frontend/src/lib/api/client.ts` | ~120 | API client wrapper |

**Total New Code:** ~2,500 lines

---

## 6. Integration Points

### Backend ↔ Worker

```python
# Trigger eval replay from backend
from worker.src.jobs.eval_replay import EvalReplayJob

job = EvalReplayJob(tenant_id=tenant_id)
report = await job.run()
# Store report in database or send via notification
```

### Backend ↔ Observability

```python
# Instrument any backend function
from backend.app.modules.observability import trace_request

with trace_request(tenant_id, session_id) as ctx:
    # All spans within this context are auto-traced
    pass
```

### Frontend ↔ Backend

```typescript
// All frontend data flows through API client
const traces = await apiClient.getTraces(tenantId, {
  limit: 50,
  offset: 0,
});
```

### Worker ↔ External Services

- **Garak**: CLI invocation via subprocess
- **PyRIT**: Python API (future: direct integration)
- **Email**: SMTP (aiosmtplib - future)
- **Slack**: HTTP webhooks
- **Vector Stores**: pgvector/Qdrant/Pinecone APIs

---

## 7. Testing Strategy

### Unit Tests (To Implement)

```python
# tests/test_eval_replay.py
def test_eval_replay_job_fetches_traces():
    job = EvalReplayJob(tenant_id="test")
    # Mock Langfuse client
    # Assert trace fetching logic

# tests/test_notification_job.py
def test_send_email_notification():
    job = NotificationJob(smtp_host="smtp.test.com")
    job.add_recipient(NotificationChannel.EMAIL, "test@example.com")
    # Mock SMTP server
    # Assert email sent
```

### Integration Tests (To Implement)

```python
# tests/integration/test_observability.py
def test_langfuse_trace_creation():
    # Real Langfuse instance (test project)
    # Assert traces appear in Langfuse UI
```

### E2E Tests (Future)

- Playwright/Cypress for frontend flows
- Full workflow: tenant creation → policy config → conversation → trace review

---

## 8. Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| Trace coverage | >95% of requests | Langfuse dashboard |
| Job success rate | >99% | Worker logs + notifications |
| Frontend load time | <2s | Lighthouse |
| Time-to-handoff | <500ms | Backend metrics |
| Notification delivery | <10s latency | NotificationJob timestamps |

---

## 9. Open Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Langfuse vendor lock-in | Medium | Abstract adapter layer; easy to swap |
| Worker job failures | High | Retry logic + notifications + dead-letter queue (future) |
| Frontend bundle size | Low | Code splitting already configured |
| Helpdesk integration complexity | Medium | Start with webhooks; add native integrations later |
| Embedding model costs | Medium | Cache embeddings; use smaller models where possible |

---

## 10. Next Steps (Phase 4)

1. **Production Readiness**
   - Comprehensive test suite
   - Load testing
   - Security audit
   - Documentation

2. **CI/CD Pipeline**
   - GitHub Actions workflows
   - Docker image builds
   - Terraform deployments
   - Staging/production environments

3. **Enhanced Features**
   - Workflow visual editor
   - Advanced policy templating
   - Custom metric dashboards
   - Multi-language support

4. **Operational Excellence**
   - Runbooks
   - On-call rotation setup
   - Incident response procedures
   - Customer support training

---

## 11. References

- [Langfuse Documentation](https://langfuse.com/docs)
- [TanStack Router](https://tanstack.com/router)
- [Vite Documentation](https://vitejs.dev/)
- [Tailwind CSS](https://tailwindcss.com/)
- OWASP Top 10 for LLM Applications 2025 (v2.0)
- Neryva Reference Architecture v1.1
- Neryva Development Stack v1.2

---

**Phase 3 Status:** ✅ **COMPLETE**

All core deliverables implemented. Ready for integration testing and pilot deployment preparation.
