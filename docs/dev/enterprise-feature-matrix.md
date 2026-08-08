# Neryva Agent Studio — Enterprise Feature Matrix

**Status:** July 2026 · Research-backed (OWASP LLM Top 10 2025, EU AI Act, ISO/IEC 42001, NIST AI RMF 1.0 + GenAI Profile, CIS MCP Companion Guide, production RAG guides, competitor analysis: LangSmith, Langfuse, Helicone, Kong, Bifrost, Portkey, Kosmoy, FloTorch).

**Priority definitions**
- **P0 — Table stakes.** Without these, no enterprise pilot, no SOC-2-adjacent evidence, no security story. First production customer blocks on these.
- **P1 — Scale.** Needed as adoption grows past one or two tenants/pilots.
- **P2 — Differentiator.** Wins deals in a crowded market (governance evidence, agent containment, cost control).

**Standards map** — each feature lists the framework it satisfies where relevant:
- `OWASP-LLM01..10` — OWASP Top 10 for LLM Applications 2025
- `EU-AI-Act` — Art. 12 (logging), Art. 26 (human oversight), Art. 50 (transparency), Art. 72 (post-market), Art. 73 (incidents)
- `ISO-42001` — Annex A controls (A.3–A.11), evidence artifacts
- `NIST-RMF` — Govern / Map / Measure / Manage functions
- `GDPR` / `HIPAA` / `PCI` — data handling modes

---

## 0. Foundation (cross-cutting, do first)

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 0.1 | ORM + Alembic migrations | Replace JSON-file tenant storage and in-memory state with SQLAlchemy models + Alembic; `run_migrations()` stub becomes real | P0 | |
| 0.2 | Repository layer | Persistence for tenants, conversations, policies, handoffs, audit events, metering | P0 | |
| 0.3 | Event bus / outbox | `conversation.created`, `guardrail.blocked`, `escalation.raised`, `eval.failed` → Redis stream/outbox → worker + webhooks | P1 | EU-AI-Act Art.12 |
| 0.4 | Correlation IDs | `trace_id` on every request, log line, span, and event; propagated into Langfuse | P0 | |
| 0.5 | Secret management | Provider keys + webhook secrets via Vault/KMS/SSM, never in `.env` in prod; per-tenant encrypted keys | P0 | |
| 0.6 | OpenTelemetry | Instrument API, guardrails, retrieval, LLM, queue; export to Langfuse + Prometheus/Grafana | P1 | |
| 0.7 | Config validation & feature flags wired | `settings/feature_flags.py` must actually gate code paths; flag values audited on change | P0 | |
| 0.8 | Dependency/security hygiene | `pip-audit`/`osv-scanner` in CI, SBOM (CycloneDX) per release, pinned+reviewed deps | P0 | OWASP-LLM03, ISO-42001 A.11 |

---

## 1. API Gateway / Edge Layer (`backend/app/api`)

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 1.1 | Authentication | API keys (scoped, expiring), OIDC/JWT for admin, mTLS option for high-assurance tenants | P0 | |
| 1.2 | Authorization (RBAC) | Roles: super-admin, org-admin, tenant-admin, operator, auditor (read-only); enforced per route | P0 | NIST-RMF Govern, ISO-42001 A.8 |
| 1.3 | SCIM provisioning | Auto-provision/deactivate users from Okta/Azure AD | P1 | |
| 1.4 | Rate limiting at API layer | Per tenant / per key / per model, token-aware; 429 with `Retry-After`; currently only guardrails-internal limiter exists | P0 | OWASP-LLM10 |
| 1.5 | Streaming endpoints (SSE) | `POST /conversations/stream` with token stream, used by widget and partners | P0 | |
| 1.6 | Idempotency keys | Retry-safe writes for conversation/external effects | P1 | |
| 1.7 | Webhook subscriptions | Customer-registered webhooks for conversation events with signatures, retries, replay | P1 | |
| 1.8 | Versioned API contracts | `/api/v1` fixed; OpenAPI spec published; additive-only policy within major version | P0 | |
| 1.9 | Request hardening | Max body size, strict Pydantic validation, header/CORS/CSRF policy, security headers, request signing | P0 | |
| 1.10 | Admin audit log | Every admin operation (tenant change, policy publish, key rotation) logged immutably | P0 | ISO-42001 A.9, NIST-RMF Measure |
| 1.11 | Tenant isolation at API | Header/key → tenant resolution (replace `NotImplementedError` stub); no cross-tenant data in responses | P0 | |
| 1.12 | Batch API | Bulk tenant config, policy sync, export endpoints | P2 | |

---

## 2. Identity & Access

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 2.1 | SSO (OIDC + SAML 2.0) | Enterprise IdP login for admin console; optional customer-facing identity bridge | P0 | |
| 2.2 | API key lifecycle | Create/rotate/revoke/expire; per-key scopes (conversation-only, admin), per-key limits, usage report | P0 | |
| 2.3 | Agent identities | Non-human identity records for agent/tool accounts (separate from human users), scoped credentials, short-lived tokens | P1 | NIST agent identity work, CIS MCP |
| 2.4 | MFA | For admin console privileged roles | P1 | |
| 2.5 | Multi-org hierarchy | Parent org → tenants → sub-tenants; policy/data inheritance with explicit overrides | P1 | |

---

## 3. Tenant Management & Policy Engine

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 3.1 | Tenants in DB | Persisted, versioned tenant config (replace JSON files); migrations on tenant schema changes | P0 | |
| 3.2 | Policy DSL | Conditions: topic, confidence, PII category, severity, model, role, time window; actions: allow / block / escalate / redact / rewrite / request-human; priority + first-match; evaluation tracing (why did this policy fire) | P0 | NIST-RMF Manage |
| 3.3 | Policy simulation | Dry-run mode: evaluate new policies against logged traffic, show impact before publish | P0 | |
| 3.4 | Policy lifecycle | Draft → review → publish → rollback; versioned with diffs; approval gate for privileged changes | P0 | ISO-42001 A.6/A.7 |
| 3.5 | Feature flags live | Wire `FeatureFlags` into real code paths; change history | P0 | |
| 3.6 | Tenant lifecycle | Onboarding checklist, offboarding, data export (GDPR), full deletion (right to be forgotten), transfer | P0 | GDPR |
| 3.7 | Model catalog per tenant | Allowed providers/models, fallback chains, max tokens, cost ceiling, provider health gating | P1 | OWASP-LLM03 |
| 3.8 | Row-level isolation | Postgres RLS, vector index partitioning, Redis key namespaces, queue namespaces per tenant | P1 | |
| 3.9 | Guardrail profiles | Per-tenant, per-department, per-agent guardrail configs (extends existing `guardrail_config`) | P1 | |
| 3.10 | SLA tiers | Priority tiers with different rate limits, retention, region, support | P2 | |

---

## 4. Guardrails (`backend/app/modules/guardrails`)

Current state: layered orchestrator (regex/classifier/jailbreak/PII/GuardrailsAI) with circuit breakers — a good skeleton. Missing: real coverage of most OWASP categories, tuning loops, evidence.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 4.1 | OWASP-LLM01 Prompt injection | Production-grade injection detector: fine-tuned classifier + encoded/obfuscated/multilingual variants + indirect injection scanning of retrieved chunks | P0 | OWASP-LLM01 |
| 4.2 | OWASP-LLM02 Sensitive data disclosure | Context-aware PII (regex + NER + LLM verification), disclosure policies, output leak scanning (keys, secrets, phone/email patterns) | P0 | OWASP-LLM02, GDPR |
| 4.3 | OWASP-LLM03 Supply chain | Model provenance registry (checksums, model cards), dependency SBOM, vendor attestation capture | P1 | OWASP-LLM03 |
| 4.4 | OWASP-LLM04 Data/model poisoning | Indexed-corpus vetting before ingestion, poisoning-detection evals, embedding drift alerting | P1 | OWASP-LLM04 |
| 4.5 | OWASP-LLM05 Improper output handling | Sink-aware sanitization: SQL/HTML/shell/URL escaping when output is executed; allowlist-based rendering | P0 | OWASP-LLM05 |
| 4.6 | OWASP-LLM06 Excessive agency | Tool allowlists, permission scoping, approval gates on privileged actions, session kill switch (see §6) | P0 | OWASP-LLM06, EU-AI-Act Art.26 |
| 4.7 | OWASP-LLM07 System prompt leakage | Prompt anti-exfiltration rules, output scanning for prompt fragments, hardened prompt templates, leakage eval suite | P0 | OWASP-LLM07 |
| 4.8 | OWASP-LLM08 Vector/embedding weaknesses | Retrieval-time content filtering, ACL-aware retrieval, embedding provenance | P1 | OWASP-LLM08 |
| 4.9 | OWASP-LLM09 Misinformation | Faithfulness/grounding check on outputs with citations, abstention policy on low confidence, factual-consistency evals | P0 | OWASP-LLM09 |
| 4.10 | OWASP-LLM10 Unbounded consumption | Token budgets per request/session/tenant, cost caps, generation length caps, loop detection | P0 | OWASP-LLM10 |
| 4.11 | LLM-as-judge guardrails | Model-assisted evaluation of nuanced violations (policy, tone, off-topic) with confidence + human fallback | P1 | |
| 4.12 | Shadow mode | Run new guardrail rules in log-only mode, measure block rates/false positives, then promote | P0 | |
| 4.13 | Feedback loop | Human corrections (false positives/negatives) stored, sampled into tuning/evals | P1 | ISO-42001 A.7 |
| 4.14 | Guardrail evidence record | Every decision persisted: input hash, layer results, violations, decision, model version, policy version → auditable packet | P0 | ISO-42001 A.9, EU-AI-Act Art.12 |
| 4.15 | Fail-closed/fail-open policy | Per-tenant configurable with default fail-closed for regulated tiers (exists at layer level; needs tenant-level exposure) | P0 | |
| 4.16 | Per-layer SLOs | Latency budgets, availability targets, alerting when a layer degrades (circuit breaker state surfaced in UI) | P1 | |

---

## 5. PII & Data Protection

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 5.1 | Multi-strategy redaction | Mask / redact / tokenize / pseudonymize / synthesize per entity type per tenant | P0 | GDPR |
| 5.2 | Reversible tokenization vault | Store token↔value mapping in encrypted vault for legitimate downstream use; restricted access | P1 | HIPAA |
| 5.3 | Data residency | Region pinning per tenant; no-cross-border transfer policy; region-aware provider routing | P0 | GDPR |
| 5.4 | DLP on egress | Outbound content inspection on API responses and widget streams (credit cards, secrets, keys) | P1 | PCI |
| 5.5 | Retention & deletion | Per-tenant TTL on conversations/logs/traces; automated deletion jobs (worker) | P0 | GDPR |
| 5.6 | Encryption | At-rest (KMS), TLS 1.3 in transit, field-level encryption for sensitive metadata, envelope encryption | P0 | |
| 5.7 | Compliance presets | HIPAA / PCI / GDPR configuration presets (entity sets, retention, redaction defaults) with evidence export | P1 | HIPAA/PCI/GDPR |

---

## 6. Orchestration / Agent Runtime (`backend/app/application/orchestration`)

Current state: single linear LangGraph pipeline. Missing: agents/tools, memory, streaming, persistence, containment.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 6.1 | Session persistence | Conversations in DB, resumable sessions, TTL per tenant | P0 | |
| 6.2 | End-to-end streaming | Token-level streaming through guardrails → LLM → output check → SSE | P0 | |
| 6.3 | MCP tool integration | MCP client with tool registry, allowlists per tenant/agent, input/output validation on tool calls | P1 | ✅ backend shipped (P9-1) — tool registry API (`/api/v1/tools`) with MCP servers as a tool source behind the P5-3 gate; UI/workbench wiring deferred | CIS MCP, OWASP-LLM06 |
| 6.4 | Human-in-the-loop gates | Pause/suspend agent loop for approval; resume with modified plan; timeout → safe default | P0 | EU-AI-Act Art.26 |
| 6.5 | Loop & runaway detection | Max steps, step budget, time budget, cost budget per session; hard kill switch | P0 | OWASP-LLM10 |
| 6.6 | Model failover | Provider/model fallback chains, health-gated routing, degraded mode (static KB answers) | P1 | |
| 6.7 | Memory | Tenant-scoped short/long-term memory with retention, forget/erase, provenance | P1 | GDPR |
| 6.8 | Multi-agent topologies | Supervisor/hierarchical graphs with scoped delegation, max delegation depth, handoff policy checks | P2 | NIST agent standards |
| 6.9 | Deterministic replay | Re-run a session from persisted trace (synthetic debug) | P1 | |
| 6.10 | Backpressure & queueing | Request queue with priority, concurrency caps, graceful degradation under load | P1 | |

---

## 7. RAG / Knowledge (`backend/app/modules/rag`)

Current state: naive single-stage retrieval, in-memory, no persistence, broken plumbing (fixed). Production RAG needs the offline/online split below.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 7.1 | Offline indexing pipeline | Worker-driven: parse (OCR), structure-aware chunking (markdown/HTML/table-aware), semantic chunking, dedupe, PII scan before indexing, embedding, upsert | P0 | |
| 7.2 | Hybrid retrieval | Dense + BM25/sparse + RRF fusion (pgvector + tsvector) | P1 | ✅ backend shipped (P9-2) — Postgres FTS tsvector + GIN (migration 0011), in-memory BM25 for dev/tests, RRF fusion + fusion metrics; flag-gated (default off until P6-6 recall evals) |
| 7.3 | Cross-encoder reranking | Top-50 → rerank → top-5 (BGE/Cohere/Jina); +12–25 pts precision | P1 | ✅ backend module shipped (P9-2, flag-gated default off) — `application/retrieval/rerankers.py`; still deferred until recall evals demand it | OWASP-LLM09 |
| 7.4 | Query transformation | Expansion, HyDE, multi-query for hard queries | P2 | |
| 7.5 | ACL-aware retrieval | Metadata filters carry access-control; user/role-scoped document access enforced at retrieval time | P0 | OWASP-LLM08, GDPR |
| 7.6 | Citations & grounding | Output must cite retrieved chunks; faithfulness check (RAGAS) on generation; ungrounded claims flagged | P0 | OWASP-LLM09 |
| 7.7 | Corpus management | Versioning, refresh cadence, stale-document detection, index health metrics | P1 | |
| 7.8 | Semantic caching | 40–70% cost/latency reduction on repeated queries; per-tenant, TTL — ✅ backend shipped (P3-7): exact + semantic layers, invalidation on config publish/KB reindex proven by tests | P1 | OWASP-LLM10 |
| 7.9 | Retrieval evals in CI | Recall@k, MRR, context_precision/recall gates on every chunker/embedding/param change | P0 | |
| 7.10 | Embedding drift monitoring | Compare online query embeddings vs index distribution; alert on drift | P2 | |
| 7.11 | Cross-tenant leakage tests | Automated negative tests: tenant A query must never return tenant B content | P0 | |

---

## 8. Escalation & Human-in-the-Loop

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 8.1 | Escalation queue (backend) | Handoff records persisted (not just in-memory), statuses, SLAs | P0 | |
| 8.2 | Channel integrations | Email, Slack, Teams, webhook, Zendesk/Jira/ServiceNow (replace generic webhook only) | P1 | ✅ backend shipped (P9-3) — concrete adapters (Zendesk/Jira/ServiceNow) dispatch from the escalation path, credential-gated, generic webhook unchanged as fallback; Slack/Teams/email remain via NOTIFICATION_CHANNEL | |
| 8.3 | Routing rules | By tenant, category, severity, confidence, agent skill | P1 | |
| 8.4 | Resolution feedback loop | Outcome captured → feeds guardrail/policy tuning and evals | P1 | ISO-42001 A.7 |
| 8.5 | SLA timers & reminders | Escalation aging, stale-handoff alerts | P1 | |

---

## 9. Observability (`backend/app/modules/observability` — currently empty)

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 9.1 | End-to-end tracing | Wire the existing Langfuse adapter into the request path: guardrails → retrieval → LLM → policy → handoff spans; tenant-tagged | P0 | |
| 9.2 | PII-safe logging | Configurable: log full / redacted / content-free; redaction before export | P0 | GDPR |
| 9.3 | Metrics | Latency percentiles, tokens, cost, error rate, block rate, escalation rate, retrieval quality proxies | P0 | |
| 9.4 | Alerting | Error-rate, cost-spike, block-rate anomaly, drift, queue depth, DLQ | P0 | |
| 9.5 | Drift monitoring | Quality drift (LLM-as-judge scores over time), topic drift, jailbreak-trend | P1 | EU-AI-Act Art.72 |
| 9.6 | Prompt management | Versioned prompts, A/B, rollback, prompt↔trace linkage | P1 | ✅ backend shipped (P9-4) — portal API + deterministic A/B resolution + trace linkage; UI portal deferred |
| 9.7 | SIEM export | Audit events to SIEM (Splunk/Datadog) for enterprise security teams | P1 | ISO-42001 A.9 |
| 9.8 | Retention controls | Trace/log retention per tenant, archival to S3 — ✅ backend shipped (P1-7): cold-tier thread archive/restore, region-pinned, checksum-verified | P0 | GDPR |

---

## 10. Evals & Red Teaming (`evals/`)

Current state: two design-doc YAMLs, no harness, no datasets, no CI integration.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 10.1 | Eval harness in CI | Regression suites on every prompt/model/policy change; gate merges on failure | P0 | ISO-42001 A.6 |
| 10.2 | RAGAS suite | Faithfulness, answer relevance, context precision/recall | P0 | OWASP-LLM09 |
| 10.3 | LLM-as-judge | With judge calibration, bias mitigation, rubric versioning | P1 | |
| 10.4 | Golden datasets | Injection, jailbreak, PII, off-topic, sensitive topics, multilingual, system-prompt-leak, encoded attacks | P0 | |
| 10.5 | Garak/PyRIT runners | Implement the configured runners on a schedule (cron in configs exists) | P1 | |
| 10.6 | Red-team release gate | Critical probe failures block deployment; report linked to release | P1 | EU-AI-Act Art.55 (aligns), NIST-RMF Measure |
| 10.7 | Trace sampling → datasets | One-click creation of eval cases from production incidents | P1 | |
| 10.8 | Human annotation | Review workflows with disagreement metrics | P2 | |

---

## 11. Worker (`worker/`)

Current state: 5 skeleton jobs, no framework, no queue wiring, mocks everywhere.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 11.1 | Queue framework | arq or Celery on Redis with retries, DLQ, scheduling, priorities; wire to backend `QueueManager` | P0 | |
| 11.2 | Real ingestion job | PDF/OCR, embeddings, vector upsert with idempotency | P0 | |
| 11.3 | Real notification job | Email/Slack/Teams via providers (replace "would send" logs) | P1 | |
| 11.4 | Real red-team job | Execute Garak with actual probe configs; PyRIT crescendo; persist results | P1 | |
| 11.5 | Eval replay job | Replay production traces with current policies; regression diff | P1 | |
| 11.6 | Cleanup/retention jobs | Retention policies, tenant deletion, temp cleanup (exists as skeleton) | P1 | GDPR |
| 11.7 | Health & metrics | Job status API, failure alerts, throughput metrics | P1 | |
| 11.8 | Idempotent workers | At-least-once with dedupe keys so retries are safe | P1 | |

---

## 12. Contracts (`contracts/`)

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 12.1 | OpenAPI source of truth | `neryva-api-v1.yaml` published; SDK generation (Python/TS) | P0 | |
| 12.2 | Event contracts | CloudEvents schema for conversation/guardrail/escalation/eval events | P1 | |
| 12.3 | Config JSON Schemas | Tenant config, policy DSL, guardrail config — validate in CI and at runtime | P0 | |
| 12.4 | Versioning policy | Additive-only within v1, deprecation window, breaking-change process | P1 | |

---

## 13. Admin Console (`frontend/`)

Current state: hardcoded shell, no API client, no auth.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 13.1 | Real data layer | API client (axios), TanStack Query, typed endpoints from OpenAPI | P0 | |
| 13.2 | SSO + RBAC UI | Login, role-aware navigation, audit-viewer role | P0 | |
| 13.3 | Tenant management | CRUD, config editor with validation, guardrail toggles, thresholds | P0 | |
| 13.4 | Policy editor | Visual rule builder, versioning, diff, simulation results, publish/rollback | P0 | |
| 13.5 | Live dashboard | Real metrics: usage, cost, latency, block rates, health | P0 | |
| 13.6 | Traces explorer | Search/filter spans, inspect guardrail decisions, replay session | P0 | |
| 13.7 | Escalation queue UI | Triage, assign, resolve, SLA timers | P0 | |
| 13.8 | Evals UI | Run suites, view results, compare, release gates | P1 | |
| 13.9 | Audit log viewer | Immutable, filterable, exportable | P1 | ISO-42001 A.9 |
| 13.10 | Model catalog UI | Providers, keys, fallbacks, health | P1 | |
| 13.11 | Usage & billing view | Metering per tenant, chargeback export | P1 | |

---

## 14. Widget (`widget/`)

Current state: 0% — pure scaffolding. This is the customer-facing surface; quality here decides adoption.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 14.1 | Web component | Shadow DOM, no iframe, `<script>` embed, configurable theme | P0 | |
| 14.2 | SSE streaming | Token streaming from backend, typing indicators, reconnect/backoff | P0 | |
| 14.3 | Session bootstrapping | Signed token handshake, session resume, tenant binding | P0 | |
| 14.4 | Accessibility | WCAG 2.2 AA, keyboard nav, screen-reader labels, focus management | P0 | |
| 14.5 | Transparency notice | "You are chatting with an AI" disclosure (required), consent handling | P0 | EU-AI-Act Art.50 |
| 14.6 | Feedback capture | Thumbs up/down, rating, free-text → eval datasets + analytics | P1 | ISO-42001 A.7 |
| 14.7 | Localization | i18n, RTL, per-tenant language config | P1 | |
| 14.8 | Security | No PII in URLs, CSP, token-based sessions with rotation, input length caps, sanitized rendering | P0 | |
| 14.9 | Offline/reconnect UX | Queued messages, graceful error states, retry | P1 | |
| 14.10 | Analytics events | Widget load/abandon/resolution events into observability | P2 | |

---

## 15. Compliance & Governance Program

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 15.1 | AI inventory registry | Per-tenant system cards: purpose, model, data categories, owner, risk tier | P1 | ISO-42001 A.3, NIST-RMF Map |
| 15.2 | Evidence generation | Auto-generated compliance packets: policy decisions, guardrail outcomes, eval reports, audit events, retention state | P1 | ISO-42001 Annex A, NIST-RMF Measure |
| 15.3 | EU AI Act readiness | Transparency (Art.50), logging (Art.12), human oversight (Art.26), post-market monitoring (Art.72), incident reporting (Art.73) hooks + Annex III posture: per-tenant risk-assessment artifacts (`POST /tenants/{id}/compliance/risk-assessment` → audit trail), documented adversarial testing (P6-6 garak/pyrit), deployer documentation — surfaced via `GET /tenants/{id}/compliance` checklist | P1 | EU-AI-Act |
| 15.4 | Data subject requests | Export/deletion APIs, automated fulfillment | P1 | GDPR |
| 15.5 | Certifications path | SOC 2 Type II + ISO 27001 first, ISO 42001 next; controls documented from day one | P1 | |

---

## 16. Ops / Deployment (`ops/`)

Current state: empty.

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 16.1 | Containerization | Dockerfiles (backend/frontend/worker), compose for dev/staging | P0 | |
| 16.2 | CI/CD | GitHub Actions: lint, typecheck, tests, security scan (Trivy), SBOM, image build/push, deploy | P0 | OWASP-LLM03 |
| 16.3 | Kubernetes + Helm | Production charts: HA, HPA, pod security, network policies, egress allowlists | P1 | |
| 16.4 | Terraform | AWS multi-region: RDS/Postgres+pgvector, ElastiCache/Redis, S3, WAF, KMS | P1 | |
| 16.5 | HA & DR | Multi-replica API, DB failover, Redis sentinel/cluster, S3 versioning, backup/restore drills with RPO/RTO | P1 | |
| 16.6 | Monitoring stack | Prometheus/Grafana, Loki, OTel collector; SLOs/SLIs with error budgets | P1 | |
| 16.7 | Load testing | k6 scenarios with latency targets (p95 < 1.5s end-to-end) and soak tests | P1 | |
| 16.8 | Secrets & config | Vault/KMS, externalized config, image signing | P1 | |
| 16.9 | Canary/blue-green | Zero-downtime model/policy deployments with rollback | P1 | |
| 16.10 | Status page & incident runbooks | Customer-facing status, runbooks for common failures | P2 | |

---

## 17. Cost Management / FinOps

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 17.1 | Metering | Token/cost per tenant, user, model, conversation, hour | P0 | OWASP-LLM10 |
| 17.2 | Budgets & hard caps | Soft alerts + hard block when exceeded; per-tenant configurable | P0 | OWASP-LLM10 |
| 17.3 | Model routing for cost | Route to cheapest adequate model per query type (policy-driven) | P1 | |
| 17.4 | Cost anomaly alerts | Spike detection (compared to baseline) | P1 | |
| 17.5 | Chargeback reports | CSV/API export for customer invoicing | P1 | |

---

## 18. Developer Platform (DX)

| # | Feature | Description | Priority | Standards |
|---|---|---|---|---|
| 18.1 | SDKs | Python + TypeScript SDKs for integration | P1 | |
| 18.2 | Sandbox environment | Tenant can test policies/guardrails against synthetic traffic before production | P1 | |
| 18.3 | Docs portal | API reference, guides, changelog, migration guides | P1 | |
| 18.4 | Support SLAs | Severity-based response targets, incident communication | P2 | |

---

## Suggested build order (phases)

1. **Phase A — Trust (P0 security & persistence):** 0.1–0.8, 1.1, 1.2, 1.4, 1.9, 1.11, 2.1, 2.2, 3.1, 3.2, 3.3, 3.4, 4.1–4.10, 4.12, 4.14, 5.1, 5.5, 6.1, 6.2, 6.4, 6.5, 7.1, 7.5, 7.6, 7.9, 7.11, 9.1, 9.2, 9.3, 10.1, 10.4, 11.1, 12.1, 12.3, 13.1–13.7, 14.1–14.5, 16.1, 16.2
2. **Phase B — Scale:** 1.3, 1.5, 1.6, 1.7, 2.3, 2.5, 3.7, 3.8, 4.11, 4.13, 5.2, 5.4, 6.3, 6.6, 6.10, 7.2, 7.3, 7.8, 8.2–8.5, 9.4, 9.6, 10.2, 10.5, 10.6, 11.2–11.8, 13.8–13.11, 14.6, 14.9, 15.1–15.3, 16.3–16.9, 17.1, 17.2
3. **Phase C — Differentiate:** 1.12, 3.10, 6.8, 6.9, 7.4, 7.10, 10.8, 14.10, 15.4, 15.5, 16.10, 17.3–17.5, 18.1–18.4
