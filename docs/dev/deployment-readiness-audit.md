# Neryva Agent Studio — Deployment Readiness Audit (What Is Still Missing)

**Status:** August 2026 · Codebase audit vs. production requirements.

This document is a **gap list only** — nothing here is implemented yet. It contrasts the
current codebase state (verified by file-level audit) against what a professional,
customer-facing deployment needs. Use it as the checklist for the production push.

Priority definitions (same as `enterprise-feature-matrix.md`):
- **P0 — Blockers.** Without these, do not deploy. No external customer, no security story.
- **P1 — Professional.** Expected by any serious buyer; needed for multi-tenant scale.
- **P2 — Differentiator.** Wins deals, enables enterprise compliance tiers.

---

## 1. Readiness Snapshot (per component)

| Component | State | Readiness |
|---|---|---|
| Backend API (FastAPI, DB, auth, migrations) | Real, runnable, tested at unit level | ~55% |
| Backend guardrails stack | Skeleton + regex/jailbreak/topic layers real; NeMo + GuardrailsAI are stubs | ~40% |
| Backend RAG / retrieval / ingestion | Implemented but **never wired into the request path** (dead code) | ~20% |
| Frontend admin console | Routed shell with hardcoded mock data; no API client, no auth UI | ~10% |
| Widget (customer chat) | 0% — no source files exist | 0% |
| Worker | Full queue (Redis/in-memory fallback, retries, DLQ, idempotency), worker entrypoint + scheduler + schedule file, 7 job handlers wired to API triggers (ingestion chunk→embed, notifications incl. escalation alerts, red-team runner, eval replay, retention, webhook delivery) | ~90% |
| Evals / red team | 2 design YAMLs only; no harness, no datasets, no CI gate | ~5% |
| Contracts | OpenAPI v1 generated + pinned by CI test; 4 event schemas + 2 config schemas with validation tests | 100% |
| Ops (Docker/CI/Terraform/monitoring) | Empty | 0% |
| Packages (`packages/types`, `packages/shared`) | Empty `.gitkeep` only | 0% |

Overall: **backend prototype-grade, everything else scaffolding. Not deployment-ready.**

---

## 2. What Actually Works Today (baseline)

Do not lose these — they are the foundation:

- **API + auth + persistence**: `POST /api/v1/conversations`, tenants CRUD, API-key
  lifecycle with SHA-256 hashing, RBAC roles, expiry/revocation, tenant scoping
  (`backend/app/api/dependencies/auth.py`, `backend/app/api/routes/conversations.py`).
- **DB layer**: SQLAlchemy async engine + Alembic migration `0001_initial` creating
  9 real tables (tenants, conversations, messages, guardrail_evidence, audit_events,
  api_keys, escalations, policy_sets, policy_rules).
- **Orchestration**: real LangGraph `StateGraph` (6 nodes, conditional edges) with
  handoff preparation and tenant-scoped system prompt
  (`backend/app/application/orchestration/service.py`).
- **Guardrails**: compiled-regex fast path, 14-pattern jailbreak scanner, topic
  classifier (sentence-transformers + cosine), spotlighting templates, orchestrator
  with per-layer circuit breakers and fail-open/fail-closed policy
  (`backend/app/modules/guardrails/`).
- **Adapters**: real OpenAI/Anthropic clients (incl. streaming), pgvector SQL adapter,
  Presidio analyzer/anonymizer, Langfuse client (unwired).
- **Infrastructure patterns**: retry, circuit breaker, token-bucket rate limiter,
  metrics, health checks — real in-process implementations.
- **Tests**: 5 files with real SQLite-backed persistence tests.

---

## 3. P0 — Deployment Blockers (fix first)

### 3.1 Backend hardening
- [x] **Wire feature flags.** Flags are now read at their enforcement points:
  `ENABLE_NEMO_GUARDRAILS`/`ENABLE_GUARDRAILS_AI`/`ENABLE_PRESIDIO`/`ENABLE_SPOTLIGHTING`
  gate `guardrails/config.py:build_config_from_tenant`; `ENABLE_PRESIDIO` also gates PII
  redaction + 503 fail-closed in `conversations.py`; `ENABLE_HUMAN_HANDOFF` gates
  ticketing. Unwired flags (`ENABLE_LLAMA_GUARD_4`, `ENABLE_CLOUD_DLP`, etc.) are now
  explicitly documented as not enforced in `settings/feature_flags.py`.
- [x] **Make NeMo Guardrails real or remove it.** `nemo_rails.py` now fails CLOSED when
  not initialized (HIGH safety violation instead of pass-through), calls the real
  `validate(messages=...)` API under a timeout, and is only built when the tenant
  enables it AND `ENABLE_NEMO_GUARDRAILS` is on. `enable_nemo_rails` now defaults to
  False; enabling it without `nemo_config_path` raises `GuardrailConfigurationError`
  at boot instead of silently passing.
- [x] **Make GuardrailsAI real or remove it.** `guardrails_ai.py:_build_rail_string`
  serializes the registered JSON schema into a real RAIL spec: properties, required
  flags, primitive types, nested objects, and arrays (`_rail_object_body`,
  `_rail_property`, `_rail_element_name`, `_rail_type`). Fallback `_basic_json_validation`
  runs real jsonschema validation when the `guardrails` package is absent.
- [x] **Fail-closed by default on classifier/PII errors.** `classifier.py:evaluate`
  returns HIGH-severity violations when the model is unavailable/timeout/error/unknown
  (only when the tenant has topic restrictions; unrestricted tenants still pass).
  `pii_engine.py` raises `GuardrailEngineError` when its analyzer is missing.
  Both engines degrade gracefully at boot instead of crashing the app.
- [x] **Hard dependency on Presidio at import time.** `adapters/dlp/presidio.py` now
  imports presidio lazily inside the constructor; a missing package raises
  `RuntimeError` at construction, which the route converts to a 503 (fail-closed)
  only when `ENABLE_PRESIDIO` is on.
- [x] **Policy enforcement is a no-op in production.** Request time now reads tenants
  and policy sets from the DB: `tenant_config_from_data`/`policy_set_from_db`
  (`modules/tenant_config/service.py`) rehydrate domain objects; `_load_or_create_policy_set`
  persists an empty published set on first use so evaluation is always explicit and
  survives restarts. JSON files are write-side cache only.
- [x] **Wire retrieval/RAG into the conversation path.** `_get_retrieval_service` builds
  `create_rag_service` + `create_retrieval_service` from `create_vector_store_from_settings`
  (pgvector for Postgres, real in-memory cosine store otherwise) and passes it to the
  orchestration service; `_retrieve_context` fills `retrieved_docs`.
- [x] **Session memory.** DB-backed history: `ConversationRepository.list_messages`
  loads prior turns oldest-first, `_load_conversation_history` excludes the in-flight
  message, and the orchestration layer threads the turns into the system prompt, the
  LLM message list (system → history → current), and the handoff payload.
- [x] **Add CORS middleware.** `CORSMiddleware` added in `main.py` before the other
  middlewares, `allow_origins` from `CORS_ORIGINS` (dev default `["*"]`), credentials
  only when no wildcard origin is configured.
- [x] **Fix LLM provider matrix.** `adapters/llm/provider.py` adds `AzureAdapter`
  (endpoint/api_version from settings) and `OpenAIBasedAdapter` (Google Gemini
  OpenAI-compatible endpoint, custom gateways); `create_llm_adapter` now resolves all
  five providers, with missing key/base URL failing fast instead of 500.
- [x] **Real audit log.** Audit + guardrail evidence writes are awaited and durable
  (`conversations.py` evidence callback, `auth.py` failed/revoked/expired paths). The
  only remaining fire-and-forget call is an API-key usage cache bump, not audit data.
- [x] **Remove `RUN_MIGRATIONS_ON_STARTUP=True` default** (`env.py`). Default is now
  False; deployments run `alembic upgrade head` explicitly. `run_migrations` remains
  available for managed environments that opt in.
- Verified: `python -m pytest backend/tests -q -p no:cacheprovider` → 39 passed
  (21 pre-existing + 18 new in `test_p0_fixes.py` covering fail-closed guardrails,
  RAIL serialization, in-memory vector store, DB-first policy hydration, the LLM
  factory, session memory, and end-to-end API smoke tests). Also fixed pre-existing
  request-time crash: `api/middleware` used `log.context()` (not a structlog API) —
  now `structlog.contextvars.bound_contextvars`; and a broken relative import in
  `modules/rag/__init__.py` (`..adapters` → `...adapters`) that made the app
  unimportable once retrieval was wired.

### 3.2 Frontend
- [x] **Real API client + auth.** New `src/lib/auth/storage.ts` (localStorage key
  storage), `src/lib/auth/session.ts` (zustand store + role helpers), and
  `src/lib/api/client.ts` (axios, baseURL `/api/v1`, `X-API-Key` header injected from
  storage, 401/403 → session cleared + `neryva:auth-error` event). Backend gained
  `GET /api/v1/auth/me` returning the resolved principal; the frontend uses it for a
  real Login screen (`src/features/auth/Login.tsx`), principal bootstrap + auth-gate
  in `App.tsx`, role-aware navigation in `Sidebar.tsx` (tenant-scoped keys see only
  their own tenant; `canManageTenants`/`canViewEscalations`/`canViewAudit` helpers),
  and a session-aware `Header.tsx` (name/role + sign out).
- [x] **Replace hardcoded mock data.** Dashboard (`features/dashboard/Dashboard.tsx`)
  now derives stats from `GET /tenants`, `GET /escalations`, `GET /audit/events` with
  explicit loading/error states and a recent-audit table. Tenants
  (`features/tenants/TenantsList.tsx`) is backed by `GET /tenants` with a super-admin
  create form (`POST /tenants`). Typed wrappers in `src/lib/api/{types,endpoints}.ts`
  mirror backend response models.
- [x] **Fix dependency mismatch.** `package.json` now declares `@tanstack/react-router`
  (v1, the version this registry serves; the TanStack v1 API used by the code is
  unchanged). `vite.config.ts` manualChunks updated. Also fixed pre-existing
  `RouterProvider` misuse in `main.tsx` (v1 takes no `children`; the root route
  renders `App`), and bumped `lucide-react`/`recharts` to React-19-compatible versions
  (0.300.0/2.10.0 did not accept react@19).
- [x] **Add eslint config + real tests.** `.eslintrc.cjs` (typescript, react-hooks,
  react-refresh; `npm run lint` passes with 0 warnings), vitest wired into
  `vite.config.ts`, and 11 tests in `src/lib/{auth,api}/*.test.ts` covering key
  storage, the X-API-Key interceptor, 401 session clearing, error normalization, and
  endpoint wrappers. Verified: `npm run lint`, `npx tsc --noEmit`, `npm run test`
  (11 passed), `npm run build` all green.

### 3.3 Widget — build from zero
- [x] Widget now builds from source. New `widget/` workspace: `package.json`
  (framework-free, dev/typecheck/build scripts), `tsconfig.json`, `vite.config.ts`
  (lib mode → `neryva-widget.js` ESM + `neryva-widget.umd.cjs` UMD, dev proxy to
  backend), `index.html` playground, and real source under `src/` following the
  planned layout: `state/messages.ts` (message model), `api/client.ts` (typed
  `POST /v1/conversations` with `X-API-Key` + abort support, normalized errors),
  `ui/widget.ts` (`<neryva-widget>` custom element — shadow DOM, launcher/panel,
  attributes `tenant`/`api-key`/`api-base`/`title`/`accent-color`/`session-id`,
  `open()`/`close()`/`sendMessage()`, `neryva:reply`/`neryva:error` events,
  session-id round-trip), `index.ts` (registers the element, exports the class).
  Root `dev:widget`/`build:widget` scripts work; root `lint`/`typecheck` now
  function via new root `eslint.config.js` (flat, ESLint 9) + root `tsconfig.json`.
  Verified: root `npm run lint` (0 problems), root `npm run typecheck`, widget
  `tsc --noEmit` (incl. vite.config.ts), and `npm run build:widget` (9.4 kB ES +
  8.1 kB UMD).
- Known limitation (documented in `api/client.ts`): embedding the API key in
  customer HTML is only suitable for private deployments; a public-token/
  turn-based flow is the production path for untrusted pages.

### 3.4 Worker — make deployable
- [x] **No queue framework, no entrypoint, no packaging.** Added `backend/app/worker/`
  (entrypoint + 5 handlers), console scripts `neryva-worker` / `neryva-api` in
  `pyproject.toml` (hatchling packages `backend`, editable install verified),
  `backend/requirements.txt`, `python -m backend.app.worker.main` CLI
  (`--redis-url` / `--schedule` JSON / `--once` / `--verbose`), graceful
  SIGINT/SIGTERM shutdown. Verified: `neryva-worker --help`, `--once` drain run.
- [x] Replace mocks: real chunkers already existed in `ingestion/service.py`
  (audit line refs were stale; no `notification.py`/`eval_replay.py`/`redteam.py`/
  `cleanup.py` files existed in the repo). Added real implementations:
  `modules/notifications/` (httpx webhook, SMTP via stdlib, SMS provider webhook —
  all fail loudly when unconfigured), `application/cleanup/service.py` (vector
  retention by metadata+age, dead-letter sweep with requeue, storage cleanup),
  `application/eval_replay/service.py` (DB-backed replay through the real
  orchestration pipeline, audit-logged), `application/ingestion/embeddings.py`
  (real SentenceTransformer embeddings, lazy; dimension checks; upsert with
  tenant/document/source/stored_at metadata).
- [x] Add scheduling (JSON schedule file, interval-window idempotency keys) + DLQ,
  retries, idempotency keys. Added `QueueManager` idempotency (atomic Redis SET NX
  claim + in-memory set; processed-key skip), in-memory DLQ storage + `drain_dead_letter`,
  Redis-down fallback to in-memory queue (was crash-on-startup), poll-on-empty
  in-memory dequeue (was a busy loop). 16 new tests in `backend/tests/test_worker.py`;
  full suite: 55 passed.

### 3.5 Ops — everything missing
- [ ] Dockerfiles (backend/frontend/worker) + docker-compose for dev/staging.
- [ ] CI/CD (GitHub Actions: lint, mypy, tests, security scan, SBOM, image build).
- [ ] Terraform + Helm/K8s, secrets management (Vault/KMS), monitoring
  (Prometheus/Grafana/OTel), backup/DR, load testing.
- [ ] Fix `.gitignore` (lines 1 and 87 contain stray markdown fences) and remove
  committed `__pycache__` artifacts.

---

## 4. P1 — Professional Deployment (next)

### Backend / platform
- [ ] Distributed rate limiting per tenant/key/model (current limiter is per-process
  in-memory, `auth.py:88-93` — breaks with multiple workers).
- [ ] Initialize and wire cache, queue, storage managers (`get_*` raise `RuntimeError`
  because `init_*` is never called); multi-worker safe.
- [ ] Wire Langfuse tracing into the request path (adapter exists, zero call sites;
  `ENABLE_LANGFUSE_TRACING` flag dead).
- [x] SSE/streaming endpoint (`POST /conversations/stream`). Real async stream with
  a `None` sentinel in `stream_message._run`, `_extract_stream_delta` token
  extraction, and real-adapter contract fakes. 8 tests in `backend/tests/test_sse_streaming.py`.
- [x] Webhooks (signed, retries, replay) + idempotency keys on writes.
  `modules/webhooks/` (HMAC `X-Neryva-Signature` signer, deliverer, publisher),
  `webhook_subscriptions/events/deliveries` tables + worker `handle_webhook_deliver`,
  `/webhooks/*` routes, `webhooks:read/write` permissions, `Idempotency-Key` replay
  on `POST /conversations`, migration `0002_webhooks`. 14 tests in `test_webhooks.py`.
- [x] Admin audit log with immutable, exportable records (ISO-42001 A.9).
  `GET /audit/export` returns an immutable snapshot; audit rows are write-only
  append records.
- [x] Tenant lifecycle: onboarding, offboarding, GDPR export/delete APIs.
  `TenantLifecycleService`: `GET /tenants/{id}/gdpr/export`,
  `DELETE /tenants/{id}/data` (erasure, keeps audit trail), `DELETE /tenants/{id}`
  (offboard). 7 tests in `test_tenant_lifecycle.py`.
- [x] Model catalog per tenant (providers, fallbacks, cost ceilings).
  `modules/model_catalog/` (allowlist resolve + fallback_order, cache-backed rolling
  cost ceiling, token estimator), `model_catalog` table + migration `0003`,
  `/tenants/{id}/models` CRUD, orchestration `model_override` + 403/429 enforcement.
  18 tests in `test_model_catalog.py`.
- [x] Guardrail evidence packet per decision persisted. `PolicySet.evaluate_with_results`
  returns per-rule details; `_emit_decision_evidence` writes a `direction="policy"`
  packet on EVERY message (ALLOW/BLOCK/ESCALATE/REDACT) incl. violations,
  layers_evaluated, processing_time_ms. 7 tests in `test_evidence.py`.
- [ ] Health endpoint must check guardrails engines, Redis, storage, Langfuse
  (`main.py:118-127` reports healthy when those are down).
- [x] Human-in-the-loop gates (pause/resume/approve) + loop/runaway/cost budgets.
  Tenant `budgets` (`max_redact_iterations`/`max_graph_steps`/`max_duration_s`)
  feed the orchestration redact-loop guard + LangGraph recursion_limit, with
  `budget_exceeded` surfaced in the result; `POST /conversations/{id}/pause|resume`
  with 423 Locked while paused; escalation lifecycle
  `POST /escalations/{id}/assign|resolve`; handoff rows now persist
  `conversation_id` + external ticket ref. 12 tests in `test_hitl.py`.
  Cost budgets: model catalog ceilings (see above).

### RAG
- [x] Wire ingestion → embeddings → pgvector upsert → retrieval into the conversation
  path; tenant isolation verified with cross-tenant leakage tests. Retrieval was
  wired in P0 (3.1); tenant isolation is now verified end-to-end at store →
  `RAGService.search` → `RetrievalService.retrieve` (store filter + defense-in-depth
  post-filter + per-tenant cache keys) → orchestration `_retrieve_context` with
  identical cross-tenant documents. `knowledge_allowlist` is now enforced as a
  source allowlist during retrieval (was dead). 18 tests in `test_rag_isolation.py`.
  Ingestion trigger: `POST /api/v1/tenants/{tenant_id}/documents` (RBAC
  `knowledge:write`) enqueues `ingestion.process` (202); the worker chunks the
  document and enqueues the `ingestion.embed` follow-up (idempotency key
  `embed:<document_id>[:<idem>]`), which indexes into the vector store. Worker
  entrypoint now also initializes the DB (previously missing — eval.replay and
  webhook.deliver would have crashed under the real worker).
- [x] Chunking done in worker (`ingestion.py` chunkers are real — wire them).
  `handle_ingestion_process` runs the real recursive/sentence/paragraph/fixed
  chunkers; chunk→embed chaining covered in `test_worker.py`.
- [x] Citations/grounding + faithfulness check on outputs. `modules/grounding/`
  lexical-overlap `FaithfulnessChecker` runs in `_validate_output` when context was
  retrieved; citations (source/document_id/chunk_id/score) surface in
  `AgentState.citations`, `ConversationResponse.citations` and the SSE `result`
  event, plus `faithfulness` metadata.

### Frontend
- [ ] Real pages: tenants CRUD + config editor, policy editor (draft→review→publish),
  live dashboard (usage/cost/latency/block rates), traces explorer, escalation queue,
  audit log viewer, model catalog, settings.
- [ ] SSO (OIDC) + MFA for admin roles; role-aware navigation.

### Widget
- [ ] Web component (shadow DOM, `<script>` embed, themeable), SSE token streaming
  with reconnect, signed session bootstrap + resume, accessibility (WCAG 2.2 AA),
  AI-disclosure notice (EU AI Act Art. 50), feedback capture.

### Worker / Evals / Contracts / Ops
- [x] Ingestion, notification, red-team runner, eval replay, retention jobs —
  all wired to the queue with retries/DLQ.
  - Ingestion: `POST /tenants/{tenant_id}/documents` → `ingestion.process`
    (chunks in worker) → `ingestion.embed` (indexes chunks); idempotent per
    source/`_idem`; tests in `test_worker.py`.
  - Notification: real transports (webhook/SMTP/SMS via `notification.send`);
    escalations now enqueue an operator alert when `NOTIFICATION_TARGET` is set
    (channel from `NOTIFICATION_CHANNEL`, idempotency key per handoff) —
    `test_worker.py::TestEscalationNotifications`.
  - Red-team runner: `application/redteam/` — deterministic suites
    (jailbreak, prompt-injection, PII, benign controls) executed against the
    real guardrail pipeline; versioned machine-readable report persisted to the
    audit log (`redteam.run`); `redteam.run` handler registered with the worker;
    runs weekly via `schedule.default.json`. 6 tests in `test_redteam.py`.
  - Eval replay: `POST /eval/replay` → `eval.replay` job (404 on unknown
    tenant; RBAC `evals:run`).
  - Retention: `POST /retention/run` (super-admin, per-tenant age filter +
    DLQ sweep) and daily `cleanup.run` sweep job (requeues dead letters);
    sweep-only runs skip vector cleanup (previously raised).
  - Worker entrypoint: DB is now initialized (with migrations when
    `RUN_MIGRATIONS_ON_STARTUP`) alongside queue/cache/storage.
  - RBAC: new permissions `knowledge:write`, `evals:run`
    (super_admin + tenant_admin). API smoke: `test_operations.py`.
- [ ] Eval harness in CI with golden datasets (injection, jailbreak, PII, off-topic,
  multilingual, prompt-leak); RAGAS suite; red-team release gate.
- [x] OpenAPI spec file + versioned event contracts + config JSON Schemas.
  - `contracts/openapi/openapi.v1.json` — generated from the live FastAPI app
    (`python backend/scripts/export_openapi.py`); `backend/tests/test_contracts.py`
    pins the committed file to the running app, so route changes fail CI until
    re-exported.
  - `contracts/events/` — `webhook-envelope.schema.json`,
    `conversation.completed.schema.json`, `guardrail.blocked.schema.json`,
    `sse-stream.schema.json`; payloads produced by `WebhookEnvelope`/SSE
    validate against them in CI.
  - `contracts/schemas/` — `evidence-packet.schema.json` (guardrail/policy
    decision packets), `tenant-config.schema.json` (tenant creation payload +
    stored shape). 11 tests in `backend/tests/test_contracts.py`.
  - Known gap for later: `ENABLE_RAGAS_EVAL` is declared in config but not
    wired to an eval runner; event contracts for eval results can follow once
    the runner exists.
- [ ] Observability: OTel + Prometheus/Grafana, alerting (error rate, cost spike,
  block-rate anomaly), PII-safe logging.
- [ ] Secrets: Vault/KMS/SSM, never `.env` in prod; per-tenant encrypted keys.
- [ ] Dependency/security hygiene: `pip-audit`/`osv-scanner` + SBOM (CycloneDX) in CI.

---

## 5. P2 — Differentiators / Scale

- SCIM provisioning, multi-org hierarchy, RLS (Postgres row-level isolation), Qdrant
  scale path, hybrid retrieval + cross-encoder reranking, semantic caching.
- Multi-agent topologies, deterministic session replay, model failover routing.
- Drift monitoring, prompt management (versioned, A/B), SIEM export, compliance
  presets (HIPAA/PCI/GDPR), evidence packet export, data-subject request automation.
- Batch APIs, SLA tiers, chargeback/billing reports, SDKs (Python/TS), sandbox
  environment, docs portal, status page + incident runbooks.

---

## 6. Suggested Order of Operations

1. **Stabilize backend truth** (P0 §3.1): wire flags, remove or implement NeMo/GuardrailsAI,
   fail-closed behavior, DB-first tenant config + policies, wire RAG + checkpointer.
2. **Ship a deployable skeleton end-to-end**: Dockerfiles + compose, CI, worker entrypoint
   with queue, contracts/OpenAPI, frontend API client + login + tenants page.
3. **Build the widget** (customer surface) against the real streaming API.
4. **Observability + evals**: Langfuse wiring, metrics, alerting, harness in CI, datasets.
5. **Compliance program**: audit log, retention, GDPR APIs, evidence packets, certifications.

---

## 7. Repo Hygiene (cheap, do immediately)

- [ ] `README.md:118-120` references `opencode.json` / `.opencode/` that do not exist.
- [ ] No LICENSE file despite "Proprietary — All rights reserved" (README:124).
- [ ] `.gitignore` malformed (markdown fences at lines 1, 87); `__pycache__` committed.
- [ ] `stack.md` targets Python 3.13; code runs on 3.14 — reconcile.
- [ ] Root `package.json` references `widget/vite.config.ts` which does not exist;
  `pnpm-workspace.yaml` lists `widget` without a `package.json` → `pnpm install` fails.
