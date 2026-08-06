# Neryva Agent Studio — Implementation Ledger (Architecture-Conformance)

**Status:** Working document (single source of truth for implementation tasks)
**Date:** August 2026
**Scope:** Bring the implementation to the architecture defined in `docs/implementation/architecture-v2.md` (Hybrid Architecture v3, Verified & Corrected) by **conformance, not by rebuild**. The existing codebase remains the base of this build: wherever it already aligns with the architecture it is kept as-is; wherever it conflicts with the architecture, the architecture wins and the conflicting implementation is removed and replaced by the architecture's design.
**Companion docs:** `architecture-v2.md` (target), `docs/dev/deployment-readiness-audit.md` (gap record — context only), `docs/dev/enterprise-feature-matrix.md` (P0/P1/P2 feature backlog), `docs/dev/stack.md` (tool choices), `docs/notes/neryva-agent-studio.md` (product).

---

## 1. Conformance policy (read before anything else)

`architecture-v2.md` is the contract; the current implementation is judged against it. Where the two agree, the implementation stands untouched. Where they conflict, the architecture's design replaces the conflicting implementation.

1. **Keep what aligns.** Any implementation component that satisfies its architecture requirement (§7-§15) stays, including its APIs, schema, file layout, and behavior. No rework, rename, or rewrite for stylistic reasons.
2. **Replace what conflicts.** Where an implementation component contradicts the architecture (behavior, data model, or boundaries), it is **removed and rebuilt per the architecture** — never patched into grudging alignment, and never grandfathered. The conflicting component dies; the requirement it failed is implemented fresh.
3. **Build what is missing.** Architecture requirements with no implementation are new work, built against the architecture directly (no legacy compatibility constraints).
4. **Data is carried forward on the existing schema chain.** The existing Alembic chain (0001-0003) continues; new tables and columns arrive via migration 0004+. Legacy data is migrated/backfilled where the kept schema evolves, unless a task says otherwise.
5. **Salvage is default for aligned code.** If a component aligns, it is kept and extended in place. Porting/rewriting happens only for components with a conflict verdict in §2.
6. **Conflict resolution is a task, not an argument.** Every suspected conflict is recorded in the §2 conflict register with a verdict (KEEP / REPLACE / BUILD-NEW) and an owning task (P0-1). Implementation work starts on a conflict only after its verdict is recorded.
7. **Tests keep their useful assertions.** Existing tests that assert aligned behavior (redaction, isolation, fail-closed, idempotency) are kept and extended; tests for replaced components are rewritten with the replacement.
8. **Repo layout is ratified, not rebuilt.** The existing tree is kept; new layers (session engine, context stack, gateway) land in new modules without forcing renames of aligned code (P0-2).

---

## 2. Conformance inventory & conflict register (decision record)

Each row: current implementation component vs its architecture requirement → verdict → action. Verdicts are settled in P0-1; a task reference means the component changes in that task.

| Current component | Arch requirement | Verdict | Action / owning task |
|---|---|---|---|
| `contracts/` JSON schemas (events, evidence, tenant-config, OpenAPI) | §13 eventing, §12 evidence, §6 config | **KEEP** | CI-pinned; extended by P4-1, P5-9, P7-3 |
| `evals/garak`, `evals/pyrith` configs | §16 evals | **KEEP** | Reference configs for P6-6 |
| `docs/`, `AGENTS.md` | n/a | **KEEP** | P0-0 keeps them accurate |
| Tenant lifecycle + RBAC + scoping (`api/routes/tenants`, `dependencies/auth.py`) | §6 tenancy, §6.3.7 RBAC | **KEEP** (provisional) | Confirm in P0-1 audit; extend (P5-2, P5-8) |
| `models.py` + `repositories.py` + Alembic 0001-0003 | §7.3, §11 | **KEEP, evolve** | Existing chain continues; migration 0004 adds session/governance/gateway tables (P0-3, P1-1) |
| `api/routes/conversations.py` (1175 ln monolith) | §7, §8, §9, §10 layering | **REPLACE (shape conflict)** | Monolithic request path conflicts with layered architecture; split into session/context/gateway modules (P1-10, P2-1, P3-9) while its API contract is preserved where it aligns (see P1-10) |
| `application/orchestration/service.py` (LangGraph loop) | §7.5, §8.4 | **KEEP, evolve** | Loop pattern aligns; rebuilt wiring onto thread store + assembler (P2-9, P3-9) |
| Guardrail layering (regex → classifier → jailbreak → … → PII, fail-closed policy) | §12 rails | **KEEP** | Layering aligns; P4-6, P5-4 complete the policy |
| Queue manager (Redis list/ZSET, DLQ, idempotent claim, in-memory fallback) | §11 worker, §13 | **KEEP, evolve** | Aligned pattern; extend with outbox + contracts events (P4-7) |
| Webhook signing/replay (HMAC, deliveries, replay) | §13 | **KEEP** | Aligns; migrate to contracts event schema (P4-7) |
| Evidence packet (input hash, layers, violations, decision) | §12 | **KEEP, evolve** | Aligns; rebuild against evidence schema (P5-9) |
| LLM adapters (`adapters/llm/provider.py`) | §10 gateway | **REPLACE (contract conflict)** | Bare call pattern conflicts with gateway contract; rebuilt as adapter contract + per-tenant credentials (P3-1, P0-8) |
| Presidio / Langfuse / pgvector adapters | §12, §16, §8 | **KEEP** | Aligned thin adapters |
| `api/dependencies/auth.py` rate limiter (per-process) | §6.4, §10 | **REPLACE (conflict)** | Per-process limits conflict with distributed requirement; P0-9 |
| In-memory registries (`_rag_services`, `_guardrails_registry`) | §11 multi-worker | **REPLACE (conflict)** | In-process state conflicts with horizontal scaling; Redis/DB-backed (P0-13) |
| Global provider key (settings) | §6.3.9 BYOK | **REPLACE (conflict)** | Conflicts with per-tenant credentials; P0-8 |
| Fail-open policy set (empty policy → ALLOW) | §2.5 deny-by-default | **REPLACE (conflict)** | P0-4 |
| History from raw `content` (SessionContextLoader) | §8.1, §12 PII rule 3 | **REPLACE (conflict)** | Redacted-only assembly; P0-5 |
| Stream-then-validate deltas | §9 rolling-window moderation | **REPLACE (conflict)** | Rolling-window buffer; P4-3 |
| 30-message hard cap | §8 compaction | **REPLACE (conflict)** | Compaction engine; P2-3 |
| Orphaned stream task (un-cancellable generation) | §9.6 cancel/regenerate | **REPLACE (conflict)** | Cancellation via request-id; P4-4 |
| No tool auth gate | §14 tool gate | **BUILD-NEW (missing)** | P5-6 |
| No admission control | §10, OWASP-LLM10 | **BUILD-NEW (missing)** | P0-7 |
| No usage capture | §10 usage | **BUILD-NEW (missing)** | P0-10 |
| No caching layer | §10 caches | **BUILD-NEW (missing)** | P3-7 |
| No config versioning | §12 | **BUILD-NEW (missing)** | P0-11 |
| `webhook_subscriptions.secret` stored plaintext in DB | §13, secret handling | **REPLACE (conflict)** *(new finding, P0-1 audit)* | Envelope-encrypted secrets; P4-7 |
| Concurrent messages to one thread (no serialization; `get_or_create`/`add_message` race) | §7.2 session coordinator | **REPLACE (conflict)** *(new finding, P0-1 audit)* | Per-thread serialization; P1-3 |
| Widget (`widget/`), Admin UI (`frontend/`) | §5 surfaces | **REPLACE (conflict)** | Static-key widget conflicts with session-token model; rebuilt (P7-1…P7-4); frontend kept where aligned |
| Root `worker/` tree | §11 | **REPLACE (conflict)** | Dead duplicate of `backend/app/worker/`; remove in P0-1 |
| `packages/`, `legacy/`, `data/` leftovers, committed `dist/` | n/a | **REPLACE (conflict)** | Dead clutter; cleanup in P0-1 |
| Legacy test suite | n/a | **KEEP selectively** | Assertions for aligned behavior kept; replaced-component tests rewritten per task |

**Rule:** verdicts are recorded in P0-1, not argued during implementation. Anything judged KEEP is not reworked without a new conflict finding; anything judged REPLACE or BUILD-NEW is owned by the referenced task.

---

## 3. How to use this ledger

- **Status legend:** `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` blocked (reason in task).
- Every task carries: status, dependencies, architecture section (`Arch §n`), why it exists (requirement or conflict), subtasks, acceptance criteria.
- **Rule:** a phase is done only when every task in it is `[x]` and its exit criteria hold. Do not delete tasks that turn out unnecessary — mark `[x]` with a note.
- The **Traceability Matrix (§16)** is the completeness check: every architecture requirement maps to at least one task. If a requirement has no task ID, add one.
- Open decisions (§15) are *blockers* for the tasks that depend on them — resolve before starting those tasks.
- **Conflict register entries** (§2) reference current files to record the conflict verdict and its owning task. They are not patch targets — once a verdict is recorded, the owning task removes or replaces the component.
- **Conformance rule:** when implementation and architecture disagree, the architecture wins. Never modify a task to accommodate a conflicting implementation; modify the implementation.

---

## 4. Build order at a glance

| Phase | Name | Arch | Primary outcome | Depends on |
|---|---|---|---|---|
| P0 | Conformance audit & foundations | all | Conflict register settled; kept code verified aligned; schema evolved on existing chain; CI, infra skeleton | — |
| P1 | Session engine & thread store | §7, §6.4 | Durable part-based threads, coordinator, fork, end-user tokens, surfaces | P0 |
| P2 | Context engineering stack | §8 | Assembler, compaction, tool-result clearing, memory | P1 |
| P3 | LLM gateway | §10 | Routing, fallbacks, breakers, usage, ledger, quota, caches | P0, P1 |
| P4 | Real-time path & streaming durability | §9 | Safe streaming: buffer, replay, rolling-window moderation, cancel | P1, P2, P3 |
| P5 | Governance plane & tenancy at scale | §6, §12, §14 | Compiled config, tool gate, isolation, lifecycle, compliance | P1, P2, P3 |
| P6 | Operations plane | §13 | Observability, metrics/SLOs, evals in CI, ops infra | P4, P5 |
| P7 | Surfaces | §5 | Widget streaming, hosted page, public API, admin UI, harness | P4, P5 |
| P8 | Data plane & scaling readiness | §11, §15 | Write discipline, replicas, sharding design, residency | P6 |
| §15 | Open decisions | §17 | Decisions that gate tasks | — |

---

## 5. Phase 0 — Conformance audit & foundations

**Goal:** every component in the codebase has a recorded verdict (KEEP / REPLACE / BUILD-NEW) against `architecture-v2.md`; kept components are verified aligned, conflicting components are removed or scheduled for replacement, the schema evolves on the existing chain, and CI/infra skeleton is green. **Exit criteria:** §2 register complete and settled; no conflicting component is reachable from the request path; migration 0004 applies clean; skeleton builds and tests green in CI.

### P0-0 — Keep reference docs accurate
**Status:** `[x]` · **Depends:** — · **Arch:** n/a
**Subtasks:**
- [x] `architecture-v2.md:423`: "seven weeks before this document's date" → "ten days before this document's date" (matches §0 row 8).
- [x] `architecture-v2.md:67`: Claude Agent SDK sessions citation → `code.claude.com/docs/en/agent-sdk/sessions` (verified live).
- [x] `architecture-v2.md:437` (§18): same stale sessions URL corrected (found during audit; kept docs internally consistent per P0-0 acceptance).
- [ ] (optional) §18: note LiteLLM Rust migration is a staged, opt-in beta; note Art. 50's 4-month transition for pre-Aug-2-2026 systems (recital 38).
**Acceptance:** docs internally consistent; no claim in §0/§18 contradicts §3/§10/§14.

### P0-1 — Conformance audit & conflict register
**Status:** `[x]` · **Depends:** — · **Arch:** all
**Subtasks:**
- [x] Walk the full implementation tree against §7-§15 of the architecture; confirm or overturn every verdict in the §2 register; record the settled register (any new conflicts found here are added to §2 with an owning task).
- [x] Remove outright-conflicting dead weight: root `worker/` duplicate tree, `packages/`, `legacy/`, `data/` leftovers, committed `dist/` artifacts.
- [x] Keep in place: every component with a KEEP verdict (tenancy/RBAC, guardrail layering, queue manager, webhook/evidence patterns, adapters, LangGraph loop, contracts, evals configs).
- [x] Confirm zero imports from kept code into components scheduled for REPLACE that would block removal; where a KEEP component depends on a REPLACE component, the dependency is re-pointed by the REPLACE task's own steps.
- [x] Record where each REPLACE component lives so its removal/rebuild task can proceed without ambiguity.
**Acceptance:** §2 register settled with no open verdicts; dead clutter removed; no REPLACE component is imported by KEEP code after re-pointing (checked in each owning task's acceptance).
**Audit notes (Aug 2026):** full-tree walk done; all 24 §2 verdicts confirmed by direct file reads. Two NEW conflicts added to §2: plaintext webhook secrets (`webhook_subscriptions.secret`), and no per-conversation serialization (concurrent `get_or_create`/`add_message` race). Removed: root `worker/` (verified zero imports — only README tree mention), `packages/`, `legacy/`, `data/` (empty), `widget/dist`, `frontend/dist`. Line-count corrections vs earlier survey: `conversations.py` is 1367 ln (not 1175), `service.py` 661 ln, `auth.py` 226 ln, `guardrails/orchestrator.py` 439 ln.

### P0-2 — Ratify repository layout
**Status:** `[ ]` · **Depends:** P0-1 · **Arch:** §4 (layers)
**Subtasks:**
- [ ] Keep the existing tree where it aligns (backend `app/` structure, `widget/`, `frontend/`, `contracts/`, `evals/`, `ops/`).
- [ ] Ratify where new layers land (proposal, adjust in review):
  ```
  backend/app/
    api/          # existing routes kept; session-token routes added (P1-8)
    session/      # NEW: thread store, parts, coordinator, fork, compaction
    context/      # NEW: assembler, memory, tool-result clearing
    gateway/      # NEW: router, fallbacks, breakers, usage, ledger, quota, caches
    governance/   # existing + compiled config, tool gate, evidence
    infrastructure/ # existing repos/redis/object storage + new tables
    worker/       # existing queue + handlers + outbox consumers
    adapters/     # existing llm providers (rebuilt per P3-1), vector store, dlp, tracing
  ```
- [ ] No forced renames of aligned code; new modules import from existing infrastructure, not the reverse.
- [ ] Rewrite README to describe the conformance build and how to run it.
**Acceptance:** layout matches architecture layers without breaking aligned code; `docker compose up` runs the skeleton (see P0-12); README accurate.

### P0-3 — Schema evolution on the existing chain
**Status:** `[ ]` · **Depends:** P0-1 · **Arch:** §7.3, §11
**Subtasks:**
- [ ] Continue the existing Alembic chain; **migration 0004** adds the new tables and columns (no fresh chain, no re-created tables):
  - `session/`: threads, thread_events, message_parts, messages
  - `governance/`: surfaces, end_users, tenant_config_versions, tool_registry, tenant_provider_keys, audit_events (extend existing tenants/policies/evidence tables in place)
  - `gateway/`: spend_events, quota_state, cache_invalidation_log
  - `data/`: webhook_* and escalation tables (extend in place if present)
- [ ] Backfill script for existing data where the kept schema evolves (e.g., existing message rows → `messages`/`message_parts` rows with redacted content computed by P5-4's redactor; existing config row → first `tenant_config_versions` entry).
- [ ] Composite `(tenant_id, ...)` indexes on every tenant table; per-thread sequence `(thread_id, seq)` unique.
- [ ] Raw/redacted split for message content from day one (both columns required, not nullable-later).
**Acceptance:** migration 0004 applies clean to existing DBs and fresh ones; backfill is idempotent and verified on a copy of production data; schema reflects the layer ownership map above.

### P0-4 — Deny-by-default core
**Status:** `[ ]` · **Depends:** P0-3 · **Arch:** §2.5, §12
**Why:** *Do-not-repeat:* legacy evaluated an empty policy set as ALLOW.
**Subtasks:**
- [ ] Core policy evaluation: unconfigured tenant, surface, or policy set resolves to BLOCK with an evidence packet; no implicit allow.
- [ ] Written as the governance layer's first unit of work, with the compiled-config model (P5-1) designed to consume it.
- [ ] Tests: empty config → deny; partial config → deny-what-is-not-configured.
**Acceptance:** default-deny is enforced by the core, not by convention.

### P0-5 — Redacted-only storage and assembly (day one)
**Status:** `[ ]` · **Depends:** P0-3 · **Arch:** §8.1, §12 PII rule 3
**Why:** *Do-not-repeat:* legacy served history from raw content while storing redacted content separately, reinjecting PII into prompts.
**Subtasks:**
- [ ] Write path: ingress redaction produces redacted content before anything is stored (P5-4 completes the policy).
- [ ] `SessionContextLoader` reads the redacted column only; raw content is reachable only through access-controlled review/DSR paths.
- [ ] Tests: PII in turn 1 never appears in the provider prompt for turn 5.
**Acceptance:** the model context is redacted-only by construction; no fallback path to raw exists.

### P0-6 — LLM client foundation
**Status:** `[ ]` · **Depends:** P0-3 · **Arch:** §9.3, §10
**Why:** *Do-not-repeat:* legacy awaited provider calls with no timeout, retry, or breaker.
**Subtasks:**
- [ ] Gateway client foundation: per-call timeout (tenant-tunable), bounded retries with backoff, provider+model circuit breaker (in-process now; Redis-shared in P3-4).
- [ ] Fallback-chain integration point (targets in P3-3); after chain exhaustion the turn degrades (queue / escalate / offline capture — tenant-configurable).
- [ ] Tests: timeout, 5xx retry, breaker open → fallback → degrade.
**Acceptance:** no bare provider call exists anywhere in the new code.

### P0-7 — Admission control
**Status:** `[x]` · **Depends:** P0-6 · **Arch:** §10, OWASP-LLM10
**Why:** *Do-not-repeat:* legacy had no cap on concurrent provider calls.
**Subtasks:**
- [x] Per-tenant + platform concurrency semaphores (asyncio + Redis counter for multi-worker safety); excess → 429 with `Retry-After` or bounded queue.
- [x] Admission precedes routing in the gateway path (P3).
- [ ] Tests: cap enforcement, queueing, no deadlock on cancel.
**Acceptance:** concurrent generations bounded per tenant; limits tenant-configurable.
**Notes (2026-08-06):** `gateway/admission.py` ConcurrencyGate (ManagedService): per-tenant + platform asyncio semaphores, Redis INCR/EXPIRE slot counter with lease (crash-safe; count rolled back via DECR on release/excess). Redis down → in-process only, health DEGRADED (never silent). Wired in `main.py` lifespan (init/close/health); routes wrap generation (both non-stream `conversations.py` process block + SSE generator) with 429 `Retry-After` / SSE error event on `AdmissionLimitExceeded`; blocked inputs consume no slot. Settings: `ADMISSION_*` (5/50/5s/300s defaults). Redis-shared concurrency moved to P3-4 with the shared breaker.

### P0-8 — Per-tenant provider credentials
**Status:** `[x]` · **Depends:** P0-3 · **Arch:** §6.3.9
**Why:** *Conflict:* legacy used a single global provider key from settings.
**Subtasks:**
- [x] `tenant_provider_keys` table (tenant_id, provider, encrypted key, kms ref, platform-managed | tenant-owned); envelope encryption (KMS-ready).
- [x] Provider factory resolves per-tenant keys; platform-managed key only when the tenant has none and the platform permits it.
- [ ] Backfill: existing tenants inherit the platform-managed key row (or admin-set keys) in migration 0004; global key removed from settings and from any request path.
- [x] No key leaves the backend, never logged; rotation endpoint for tenant admins.
- [ ] Tests: two tenants with different credentials concurrently; rotation; keys absent from traces/logs.
**Acceptance:** BYOK per tenant; no global key path exists.
**Notes (2026-08-06):** `infrastructure/keys/` (crypto.py Fernet envelope, KMS-ready via per-row kms_ref + env-injected `PROVIDER_KEY_ENCRYPTION_KEY`; dev file fallback auto-created + gitignored; prod refuses auto-generation) + `service.py` ProviderKeyService (DB row wins, `_settings_key_for` fallback only while `PLATFORM_MANAGED_KEYS_ENABLED`; 503 on none). `TenantProviderKeyRepository` (get/upsert bumps key_version/delete, never returns secret). Route `PUT /tenants/{tenant_id}/provider-keys/{provider}` (tenants:write, key_source validated, audit `provider_key.rotated` without the secret). Both conversation endpoints resolve via `await _resolve_llm_api_key(tenant_config, db)`. **Deferred to P3-1:** backfill rows for existing tenants + removal of global settings keys from the request path (fallback is the only remaining settings read, behind flag). New dep `cryptography>=42` in pyproject.

### P0-9 — Distributed rate limiting
**Status:** `[x]` · **Depends:** P0-3 · **Arch:** §6.4, §10
**Why:** *Do-not-repeat:* legacy rate limiter was per-process and silently degraded to in-process under Redis failure.
**Subtasks:**
- [x] Redis token bucket (Lua) per tenant/key/model with `Retry-After`; Redis-down → documented degraded mode with alert, never silent.
- [ ] Per-end-user limits + concurrent-session caps (ties P1-8).
- [ ] Tests: limits hold across two app instances; fallback alerts.
**Acceptance:** rate limits are distributed; degradation is visible.
**Notes (2026-08-06):** `patterns/rate_limiter.py` rewritten: Redis path is now a Lua token bucket (continuous refill max/window, single-key → Cluster-safe; replaces fixed-window INCR which bursted at window edges). Degradation is never silent: alert-level transition log (down/recovered) + `degraded` flag + `health_check()` DEGRADED component, wired into `/health`; lazy self-healing re-ping while degraded. Per-key `Retry-After` already surfaced via `RateLimitExceeded` (auth.py returns window seconds). Per-end-user limits + concurrent-session caps deferred to P1-8 (depends on end_users).

### P0-10 — Usage contract
**Status:** `[x]` · **Depends:** P0-6 · **Arch:** §10 (usage capture)
**Why:** *Do-not-repeat:* legacy `LLMResponse` carried no usage data.
**Subtasks:**
- [x] Adapter contract returns input/output/reasoning tokens (and cached-token fields where the provider reports them) on every call.
- [x] Usage persisted per turn; feeds the cost ledger (P3-5).
- [ ] Tests with adapter fakes.
**Acceptance:** every completed turn records token usage.
**Notes (2026-08-06):** `adapters/llm/provider.py` — canonical usage keys `input_tokens/output_tokens/reasoning_tokens/cached_tokens` on every `chat()` (OpenAI-family: `usage.completion_tokens_details.reasoning_tokens` + `prompt_tokens_details.cached_tokens`; Anthropic: cache_read + cache_creation summed into cached_tokens; None-safe). Streaming: OpenAI/Azure now send `stream_options={"include_usage": True}` (CUSTOM/GOOGLE excluded — compat gateways may reject unknown params); `_extract_usage` merges usage from stream chunks (Anthropic message_start/message_delta events). `SpendEventRepository` + orchestration `usage_callback` (`_record_usage` after every completed generation, sync/awaitable-safe, failures logged); both conversation endpoints persist one `spend_events` row per turn (tenant/conversation/session/provider/model/tokens; `usd`=0.0 until P3-5 pricing).

### P0-11 — Config versioning (core)
**Status:** `[x]` · **Depends:** P0-3 · **Arch:** §12
**Why:** *Do-not-repeat:* legacy read tenant config from a single mutable row per request.
**Subtasks:**
- [x] `tenant_config_versions` immutable rows (config JSON, version, status, published_at, promoted_by); runtime reads the published version.
- [x] Publish/promote/rollback endpoints (canary flow in P6-5); invalidation events on publish.
- [ ] Tests: publish → new behavior; rollback → old; concurrent publish serialized.
**Acceptance:** every config change is versioned, revertible, audited.
**Notes (2026-08-06):** `TenantConfigVersionRepository` (append-only drafts; `promote` demotes prior published → superseded, sets published_at/promoted_by; unique tenant+version). Routes: `POST /tenants/{id}/config-versions` (draft; validates id/slug vs tenant), `.../publish`, `.../rollback` (re-promote) — tenants:write + audit. Runtime: both conversation endpoints load via `_load_effective_tenant_config` = tenant row merged with latest published version (none → row authoritative); publish invalidates the in-process service cache. Concurrent-publish serialization (advisory lock) deferred to P6-5 canary; `get_latest_published` (max version) deterministic under last-writer-wins.

### P0-12 — Ops scaffolding
**Status:** `[x]` · **Depends:** P0-2 · **Arch:** §16
**Subtasks:**
- [x] Dockerfiles: backend, worker, frontend, widget build stage.
- [x] `docker-compose.yml`: api, worker, Postgres 18 + pgvector, Redis 7, (optional) Langfuse — dev parity with prod.
- [x] `.env.example` aligned with settings; env-driven flags documented.
- [x] CI: lint, mypy, pytest, typecheck, build images, Trivy scan.
- [x] Health/readiness endpoints wired to real components from day one (DB, Redis, storage, engines, queue) — liveness vs readiness split.
**Acceptance:** `docker compose up` runs api + worker; CI green on the skeleton; readiness reflects real component state.
**Notes (2026-08-06):** `ops/docker/backend.Dockerfile` (python:3.13-slim, `pip install -e '.[all]'`, HEALTHCHECK on /health/live), `worker.Dockerfile` (same base, `neryva-worker`), `frontend/widget.Dockerfile` (pnpm workspace → Vite build → nginx). `docker-compose.yml`: api/worker/postgres (pgvector/pgvector:pg18, pg_isready healthcheck)/redis 7 (+ Langfuse behind `observability` profile). `.github/workflows/ci.yml`: ruff + mypy + pytest, pnpm lint/typecheck, docker compose build api worker, Trivy (CRITICAL/HIGH, exit-code 1). `/health/live` liveness (no deps) split from `/health` readiness (DB/Redis/queue/cache/storage/engines/rate_limiter/admission). `.env.example` fully aligned (admission, provider keys + BYOK flags, notifications, vector store). TODO: commit a pnpm-lock.yaml (currently only package-lock.json, so Docker uses no-frozen install) — tracked in P0-14.

### P0-13 — Multi-worker safety (day one)
**Status:** `[x]` · **Depends:** P0-2 · **Arch:** §11
**Subtasks:**
- [x] No in-process shared mutable state in request path (state lives in Redis/DB); documented init order and failure semantics.
- [x] Test: two worker/API processes boot and share queue/cache state cleanly.
**Acceptance:** horizontal scaling is safe from the first deployment.
**Notes (2026-08-06):** `docs/ops/operations.md` — state inventory (queue/cache/rate-limit/admission → Redis; tenants/policies/keys/config versions/spend → Postgres), lifespan init order, failure contract (startup never fails on backend outage; degradation never silent: error-level transition log + DEGRADED readiness; liveness vs readiness split). `backend/tests/test_multi_worker.py`: two OS processes share queue state via Redis (skipif no Redis), both boot in-process under Redis outage, limiter/admission report DEGRADED. CI backend job gained a Redis service so the two-process tests run.

### P0-14 — Dependency & security hygiene
**Status:** `[x]` · **Depends:** P0-2 · **Arch:** n/a (OWASP-LLM03)
**Subtasks:**
- [x] Declare only needed deps (no dormant flags/imports); `pip-audit` + `osv-scanner` + `npm audit` in CI; CycloneDX SBOM per release.
- [ ] Pinned, reviewed versions per `stack.md`.
**Acceptance:** CI fails on known-vulnerable dependencies; SBOM artifact per release.
**Notes (2026-08-06):** CI backend job: pip-audit (fail on vulns), osv-scanner (SARIF upload to code scanning); frontend job: `pnpm audit --prod`; release-tag job: CycloneDX SBOM (`cyclonedx-py requirements --pyproject-only`) uploaded as artifact. Removed dormant `ragas>=0.2` (unused anywhere; returns with the eval harness). Added `cryptography>=42` (P0-8) to pyproject + backend/requirements.txt. Reviewed: aiohttp (handoff/escalation), jsonschema (validators), python-dotenv (pydantic-settings .env), psycopg2-binary (alembic sync env) all in use. **Remaining:** strict pinning + committed pnpm-lock.yaml and pip lockfile (currently `>=` ranges per repo convention); tracked in P3/P4 hygiene sweep.

---

## 6. Phase 1 — Session engine & thread store (Arch §7, §6.4, §6.5)

**Goal:** durable append-only thread log is the source of truth; parts, sequences, cursor pagination, request-id dedup, coordinator, fork, end-user session tokens, surfaces model — built on the existing engine where it aligns, new modules where it is missing. **Exit criteria:** no static-key customer path exists; two simultaneous messages to one thread cannot interleave; retries never double-write; threads fork/regenerate append-only; all reads tenant+end-user scoped.

### P1-1 — Thread store schema + models
**Status:** `[ ]` · **Depends:** P0-3 · **Arch:** §7.1, §7.3, §11
**Subtasks:**
- [ ] In migration 0004 (P0-3): `thread_events` (append-only log, per-thread `seq`, `request_id`, `type`, payload jsonb); `message_parts` (part_type: text|reasoning|tool_use|tool_result|citation|compaction|step, index, content jsonb, redacted_content jsonb); `messages` (seq, request_id unique, parent_message_id, surface_id, end_user_id).
- [ ] `surfaces`, `end_users` tables owned by governance (P0-3) — session layer references them, never owns them.
- [ ] Vertical partitioning: message metadata separate from parts (constant-cost appends regardless of thread length).
- [ ] Tests: schema constraints (unique seq per thread, unique request_id).
**Acceptance:** the session schema is complete and layer-owned.

### P1-2 — Thread repository
**Status:** `[ ]` · **Depends:** P1-1 · **Arch:** §7.1
**Subtasks:**
- [ ] `ThreadRepository`: atomic append with seq assignment, write parts, read tail by seq, cursor pagination (`after`, `limit`, `hasMore`), list threads by end-user.
- [ ] Request-id dedup: insert-on-conflict returns the original result; client retry after 5xx never double-writes.
- [ ] Every query tenant+end-user scoped.
- [ ] Tests: pagination ordering, dedup idempotency, cross-tenant negative tests.
**Acceptance:** history reads come from the durable log; pagination is cursor-based; dedup proven.

### P1-3 — Session coordinator
**Status:** `[ ]` · **Depends:** P1-2 · **Arch:** §7.2
**Why:** *Do-not-repeat:* legacy interleaved history appends under concurrent messages to one thread.
**Subtasks:**
- [ ] Per-thread serialization: at most one in-flight generation per thread; concurrent messages queue in sequence order (Redis lock with lease + local queue; documented fallback).
- [ ] Disjoint threads run concurrently; cancellation releases the slot (ties P4-4).
- [ ] Tests: N concurrent messages → strictly ordered appends; no lost updates.
**Acceptance:** the race cannot occur; disjoint-thread throughput unaffected.

### P1-4 — Append-only regeneration and editing
**Status:** `[ ]` · **Depends:** P1-2 · **Arch:** §7.1
**Subtasks:**
- [ ] Regenerate = new assistant message with `parent_message_id` to the original; nothing mutated in place.
- [ ] Edit = new user message + regenerated successor chain; audit shows both attempts.
- [ ] API: `POST /threads/{id}/regenerate`, `POST /threads/{id}/messages/{mid}/edit`.
- [ ] Tests: chains resolve; original immutable.
**Acceptance:** no in-place mutation of durable messages from these features.

### P1-5 — Fork
**Status:** `[ ]` · **Depends:** P1-2 · **Arch:** §7.1 (OpenCode fork + Claude `fork_session` analogue)
**Subtasks:**
- [ ] `POST /threads/{id}/fork?at=<seq>`: new thread copies history to the boundary; original unchanged; both independently resumable.
- [ ] Customer path: behind "regenerate" UX; operator path: harness fork/replay (P7-5).
- [ ] Tests: fork isolation, original immutability, both sides resumable.
**Acceptance:** fork at any message boundary with full isolation.

### P1-6 — Hot tier: Redis thread state
**Status:** `[ ]` · **Depends:** P1-2 · **Arch:** §7.3
**Subtasks:**
- [ ] Keys `tenant:{id}:thread:{thread_id}:tail` (last N turns + running summary block), TTL = session timeout, promoted on read; summary position stable (ties P2-6).
- [ ] Idempotency keys, token buckets, breaker state all tenant-prefixed (§6.3.4).
- [ ] Redis down → Postgres tail fallback, documented degraded mode with alert.
- [ ] Tests: hot reads, promotion, fallback.
**Acceptance:** hot path serves tails from Redis; self-heals on Redis failure.

### P1-7 — Cold tier: archive
**Status:** `[ ]` · **Depends:** P1-2 · **Arch:** §7.3, §11
**Subtasks:**
- [ ] Archive job: threads older than tenant window (default 30-90 days) → object storage `tenant:{id}/archive/`; marker in DB; async restore endpoint.
- [ ] Region-pinned archive path (ties P5-12).
- [ ] Tests: archive + restore round-trip; archived threads absent from hot queries.
**Acceptance:** archive is automated and reversible.

### P1-8 — End-user model and session tokens
**Status:** `[ ]` · **Depends:** P1-1 · **Arch:** §6.4
**Why:** *Do-not-repeat:* legacy widget shipped a static tenant API key in customer HTML.
**Subtasks:**
- [ ] Anonymous bootstrap: per-device identity (local storage) → `POST /v1/session-tokens` mints short-lived token bound to (tenant, surface, end_user, device, expiry, scopes).
- [ ] Authenticated exchange: tenant app swaps its auth state for a platform session token (JWT/OAuth exchange); platform never sees tenant passwords.
- [ ] Token → (tenant, surface, end_user) resolution middleware at the connection tier; tokens scoped to exactly one tenant.
- [ ] Abuse limits per end-user (rate, spend cap, concurrent sessions) via Redis; bot detection hook at widget edge (P7-1).
- [ ] Tests: expiry/rotation, cross-tenant rejection, device continuity.
**Acceptance:** customer surfaces authenticate by session token only; end-user identity never crosses tenants.

### P1-9 — Surfaces model
**Status:** `[ ]` · **Depends:** P1-1 · **Arch:** §6.1
**Subtasks:**
- [ ] Surface CRUD + per-surface config (persona, knowledge allowlist, tool allowlist, model pin, budgets, brand voice override); default surface on tenant onboarding.
- [ ] Request path resolves surface and applies its config; unconfigured surface denies (P0-4).
- [ ] Tests: two surfaces on one tenant behave differently; unconfigured surface denies.
**Acceptance:** a tenant runs sales + support assistants concurrently with different rails on one engine.

### P1-10 — Conversation API v1 (evolve existing surface)
**Status:** `[ ]` · **Depends:** P1-2, P1-3 · **Arch:** §7, §5
**Subtasks:**
- [ ] Rework `api/routes/conversations.py` request path onto the thread repository + coordinator + session tokens (its monolith shape conflicts with the architecture layers; its v1 contract is kept where it aligns — additive-only from here forward).
- [ ] History construction delegated to the context assembler (P2-1) — no fixed-count slice exists anywhere.
- [ ] OpenAPI generated from the reworked app; contracts CI pin from day one.
- [ ] Tests: API surface, auth, pagination, idempotency end-to-end.
**Acceptance:** the v1 API keeps its aligned contract, loses its conflicting internals, and is contract-pinned.

---

## 7. Phase 2 — Context engineering stack (Arch §8)

**Goal:** model-visible context assembled every turn inside a token budget from redacted content; compaction atomic, cache-aware, failure-safe; memory opt-in and PII-filtered. **Exit criteria:** compaction runs with breaker + truncate fallback; assembler is the only component touching history; compaction quality measurable.

### P2-1 — Context assembler (SessionContextLoader)
**Status:** `[ ]` · **Depends:** P0-5, P1-10 · **Arch:** §8.1
**Subtasks:**
- [ ] Single component builds: `system` (stable prefix) → `summary block` (immutable between compactions) → `memory block` (opt-in, top-k) → `recent tail` (newest first, in budget) → `retrieved knowledge` (allowlist-filtered, spotlighted) → `current message`.
- [ ] Budget math against per-model context from the catalog; 4-chars-per-token heuristic with per-model override.
- [ ] Reads redacted content only (P0-5); applies tool-result clearing (P2-7).
- [ ] Tests: budget overshoot trimming, block placement, redaction guarantee.
**Acceptance:** assembler is a single testable component; nothing else constructs the provider prompt.

### P2-2 — Token estimator + model catalog
**Status:** `[ ]` · **Depends:** P2-1 · **Arch:** §8.1, §10
**Subtasks:**
- [ ] Estimator service (4 chars/token default, model overrides; counts parts, not raw text).
- [ ] New `gateway/catalog` module: context-window size + price card per model (owned by gateway, consumed by context layer).
- [ ] Unit tests with fixtures.
**Acceptance:** estimation error measured (eval in P2-10); catalog is the single source of model facts.

### P2-3 — Compaction service
**Status:** `[ ]` · **Depends:** P2-1 · **Arch:** §8.2
**Subtasks:**
- [ ] Triggers: preemptive ~70%; reactive ~95%; provider-overflow one-shot recovery (retried exactly once; second overflow = hard error → degrade, not auto-fallback).
- [ ] Mechanics: head → structured rolling summary (objective, key facts, decisions, pending work, next moves); token-bounded tail (`keep.tokens` default 8000, `buffer` 20000, per-tenant tunable).
- [ ] Summary generation: cheap summarizer model, tools disabled, bounded output tokens (4096 floor).
- [ ] Atomicity: snapshot → summarize against snapshot → schema validation → atomic swap; failure leaves prior boundary active.
- [ ] Compaction events appended to thread log (part_type `compaction`).
- [ ] Tests: triggers, atomic swap, interrupted compaction.
**Acceptance:** compaction is atomic, observable, OpenCode-compatible on overflow semantics.

### P2-4 — Compaction failure handling (Neryva additions)
**Status:** `[ ]` · **Depends:** P2-3 · **Arch:** §8.2 (breaker/truncate/chunk-and-merge — **Neryva additions**, not OpenCode behavior)
**Subtasks:**
- [ ] Per-session breaker (3 consecutive failures) → lossy truncation (system + recent K turns), session marked degraded, surfaced to operators.
- [ ] Wedged summarizer: buffer > summarizer context → chunk-and-merge.
- [ ] Snapshot-rollback: deep copy before compaction; validate output; roll back on failure.
- [ ] Tests: 3 failures → truncate + degraded flag; chunk-and-merge correctness; rollback.
**Acceptance:** a session never dies from a bad compaction; degradation visible.

### P2-5 — Instant compaction (background)
**Status:** `[ ]` · **Depends:** P2-3 · **Arch:** §8.2
**Subtasks:**
- [ ] Background job refreshes running summaries as threads grow (`summary.refresh`, idempotent per thread).
- [ ] Triggered compaction = instant swap; never user-facing wait.
- [ ] Tests: cadence, triggered latency budget.
**Acceptance:** compaction adds no user-visible latency.

### P2-6 — Cache discipline
**Status:** `[ ]` · **Depends:** P2-3 · **Arch:** §8.2, §10
**Subtasks:**
- [ ] Summary block at stable position (after system), immutable for next K turns; favor removing superseded content + appending immutable blocks over full rewrites.
- [ ] `cache_control` markers on stable prefixes where providers support it; per-tenant hit-rate metric.
- [ ] Tests: cache-prefix stability across compactions.
**Acceptance:** prompt-cache prefix survives compaction boundaries; hit rate measured.

### P2-7 — Tool-result clearing (+ thinking clearing)
**Status:** `[ ]` · **Depends:** P2-1 · **Arch:** §8.3
**Subtasks:**
- [ ] Sub-transcript op: superseded, re-fetchable tool results → placeholders (keep `tool_use` record, drop payload); configurable trigger.
- [ ] Thinking-block clearing for extended-thinking traffic — evaluate Anthropic context-editing API (`clear_thinking_20251015`, beta `context-management-2025-06-27`) vs bespoke; record decision (D-12).
- [ ] Tests: space reclaimed; re-fetch works; audit shows placeholders.
**Acceptance:** agentic turns cannot bloat context via stale tool payloads.

### P2-8 — Memory (opt-in, tenant-gated)
**Status:** `[ ]` · **Depends:** P2-1 · **Arch:** §8.4
**Subtasks:**
- [ ] Background worker extracts durable structured facts from closed turns → per-tenant/per-end-user memory store; versioned, retrievable, PII-filtered.
- [ ] Assembler fetches top-k relevant facts on demand; tenant controls read scope + expiry.
- [ ] Erasure support (ties P5-10).
- [ ] Build-vs-adopt spike (D-2): Anthropic first-party memory tool for Claude-routed traffic vs bespoke store.
**Acceptance:** memory ships gated behind tenant consent; PII-filtered; fully erasable.

### P2-9 — Orchestration loop on the session engine
**Status:** `[ ]` · **Depends:** P2-1, P2-3 · **Arch:** §8, §9.3
**Subtasks:**
- [ ] New runtime loop (screen → retrieve → generate → authorize → verify → reply/escalate) consumes assembler output; tool parts flow through the loop and persist as first-class parts each turn (ties P5-3).
- [ ] Bounded re-ask on verify failure; escalate on repeated failure.
- [ ] End-to-end tests through the real pipeline.
**Acceptance:** one assembly path; parts persisted; provider prompt fully redacted.

### P2-10 — Compaction quality evals
**Status:** `[ ]` · **Depends:** P2-3 · **Arch:** §8.2, §17
**Subtasks:**
- [ ] Round-trip eval: facts present before compaction answerable after; per-tenant summary prompt + keep/buffer tuning harness.
- [ ] Bad-compaction detection surfaced to operators (context-rot alert, ties P6-4).
- [ ] Baseline datasets in `evals/datasets/`.
**Acceptance:** compaction quality measured per tenant, never assumed.

---

## 8. Phase 3 — LLM gateway (Arch §10)

**Goal:** the only component that talks to providers. **Exit criteria:** every provider call through the gateway; spend metered and quota-enforced at all four levels; failures degrade per fallback-chain contract.

### P3-0 — Build-vs-adopt decision (blocker for P3-2…P3-8)
**Status:** `[ ]` · **Depends:** — · **Arch:** §10, §17
**Subtasks:**
- [ ] Re-cost LiteLLM (Rust core, axum gateway, native `/v1/messages`, `Customer` object, per-tenant teams, Redis cooldowns + v1.82.0 dependency breaker) vs bespoke gateway per this phase.
- [ ] Criteria: data residency, BYOK, EU posture, <30ms routing, maintainability, spend hierarchy fit.
- [ ] Record in §14 (D-1). If adopt: P3-2…P3-8 become integration tasks against LiteLLM; if build: proceed as designed.
**Acceptance:** decision documented; downstream tasks adjusted.

### P3-1 — Unified adapter contract
**Status:** `[ ]` · **Depends:** P0-10 · **Arch:** §10
**Subtasks:**
- [ ] One contract for chat, streaming, tool calls, structured output across OpenAI/Anthropic/Gemini/Azure/self-hosted; wire translation inside the gateway.
- [ ] Normalized streaming events (delta, tool_use start/end, tool_result, usage, done, error).
- [ ] Reasoning/thinking tokens surfaced.
**Acceptance:** application code has zero provider branches.

### P3-2 — Router
**Status:** `[ ]` · **Depends:** P3-0, P3-1 · **Arch:** §10
**Subtasks:**
- [ ] Decision <30ms from per-deployment health/price/latency tables; per-tenant strategy: cost, latency, quality-pinned, pinned model.
- [ ] Tiered routing: simple → cheap model, complex → capable model, under tenant policy.
- [ ] Tests: strategy selection, decision latency, stale tables.
**Acceptance:** routing meets budget; tenant strategies enforced.

### P3-3 — Fallback chains (three classes)
**Status:** `[ ]` · **Depends:** P3-1 · **Arch:** §10
**Subtasks:**
- [ ] General (timeout/5xx), content-policy (refusal), context-window (overflow) — ordered targets from tenant catalog.
- [ ] Failover transparent only before first byte; mid-stream failures surface to connection tier (ties P4-4).
- [ ] Tests per class.
**Acceptance:** fallback behavior matches the contract exactly.

### P3-4 — Resilience: cooldowns + dependency breaker
**Status:** `[ ]` · **Depends:** P3-2 · **Arch:** §10, §3
**Subtasks:**
- [ ] Per provider+model cooldown state in Redis (LiteLLM `cooldown_cache.py` pattern), shared across replicas.
- [ ] Dependency-level circuit breaker for Redis (LiteLLM v1.82.0 semantics: 5 consecutive failures, 0ms fast-fail, 60s half-open probe, Postgres fallback for auth/rate-limit).
- [ ] Tests: cross-replica cooldowns; Redis slow/down → degraded-but-serving.
**Acceptance:** breaker state cross-replica; Redis degradation contained.

### P3-5 — Usage capture + cost ledger
**Status:** `[ ]` · **Depends:** P0-10 · **Arch:** §10
**Subtasks:**
- [ ] Append-only `spend_events` per request (tenant, surface, end_user, model, provider, tokens, USD); consumers for billing/dashboards/anomaly alerts.
- [ ] Async write (queue) so the request path never blocks.
- [ ] Tests: event correctness, aggregations, immutability.
**Acceptance:** every request lands in the ledger; per-tenant/surface/end-user cost answerable.

### P3-6 — Quota: USD reservation/reconciliation
**Status:** `[ ]` · **Depends:** P3-5 · **Arch:** §10, §6.3.8
**Subtasks:**
- [ ] Redis Lua: reserve estimated max spend before routing; reconcile actual after completion; platform > tenant > surface > end-user (any over → reject).
- [ ] Soft alert at 80%, hard block at 100%; per-level status codes.
- [ ] Tests: reservation math, races, boundary at each level.
**Acceptance:** rejection when any path level is over budget; no double-spend under concurrency.

### P3-7 — Caches
**Status:** `[ ]` · **Depends:** P3-2 · **Arch:** §10
**Subtasks:**
- [ ] Exact-match cache per tenant; per-tenant semantic cache with similarity threshold.
- [ ] Invalidation on knowledge change + config publish (ties P0-11); correctness tests (stale results after KB edit are a product bug — §17).
- [ ] Prompt-cache awareness: stable prefixes + markers (ties P2-6).
**Acceptance:** hit rates measured; invalidation correctness proven by tests.

### P3-8 — Tenancy mapping (if adopting LiteLLM)
**Status:** `[ ]` · **Depends:** P3-0 · **Arch:** §10
**Subtasks:**
- [ ] Neryva `tenant` → LiteLLM `Team` (or `Organization` for dedicated shape); `end_user` → LiteLLM `Customer` (never `User` — that is proxy members).
- [ ] Budgets mirrored per level.
**Acceptance:** mapping documented; end-user spend attribution correct.

### P3-9 — Wire orchestration to gateway
**Status:** `[ ]` · **Depends:** P3-2, P3-3, P3-5 · **Arch:** §10
**Subtasks:**
- [ ] Runtime loop calls the gateway interface only; admission (P0-7) precedes routing; no key handling in orchestration.
- [ ] Provider failure → fallback → degrade tested end-to-end.
**Acceptance:** orchestration has zero direct provider calls.

### P3-10 — Gateway evals
**Status:** `[ ]` · **Depends:** P3-9 · **Arch:** §10, §13
**Subtasks:**
- [ ] Latency budget tests (<30ms routing under load), fallback suite, quota races, cache invalidation.
- [ ] Failure injection: provider 500s, timeouts, refusals, overflow errors.
**Acceptance:** gateway passes the failure-injection suite in CI.

---

## 9. Phase 4 — Real-time path & streaming durability (Arch §9)

**Goal:** streaming is the only path customers see; durable (replay on reconnect), safe (rolling-window moderation), cancellable, idempotent. **Exit criteria:** no delta reaches the client before passing the moderation window; reconnect replays without loss; cancel releases upstream capacity.

### P4-1 — SSE transport
**Status:** `[ ]` · **Depends:** — · **Arch:** §9.1
**Subtasks:**
- [ ] `Last-Event-ID` resume; `X-Accel-Buffering: no`; heartbeats; CDN-safe headers.
- [ ] SSE frame contract (session/guardrails/delta/result/error + redaction/retraction + compaction frames) in `contracts/events/sse-stream.schema.json` (revised from legacy).
- [ ] Tests: reconnect with Last-Event-ID, heartbeat.
**Acceptance:** SSE resumable and contract-validated.

### P4-2 — Server-side stream buffer
**Status:** `[ ]` · **Depends:** P1-2 · **Arch:** §9.1, §3
**Subtasks:**
- [ ] Durable chunk writes while streaming (Redis stream `tenant:{id}:thread:{id}:buffer`, TTL; overflow → Postgres); replay on reconnect; flush to durable log at completion.
- [ ] Live deltas are never part of the replayable log until the turn completes (§7.1 distinction).
- [ ] Tests: reconnect mid-stream replays identical bytes; interrupted turn recovery.
**Acceptance:** no half-finished message is lost across reconnects.

### P4-3 — Rolling-window output moderation
**Status:** `[ ]` · **Depends:** P4-2 · **Arch:** §9.1, §12 PII rule 2
**Why:** *Do-not-repeat:* legacy streamed deltas before validation, so a violation could reach the client while only the stored copy was redacted.
**Subtasks:**
- [ ] Release deltas through a small buffer window; validate chunks (PII, policy, brand) as they pass.
- [ ] Mid-stream violation → redaction/retraction event + truncation; client sees only validated content.
- [ ] Window size = per-tenant latency/safety knob (§17); record default + rationale (D-3).
- [ ] Tests: violation never reaches client; truncation + retraction contract.
**Acceptance:** the customer never sees unvalidated content.

### P4-4 — Cancellation propagation
**Status:** `[ ]` · **Depends:** P3-3 · **Arch:** §9.1
**Why:** *Do-not-repeat:* legacy leaked an in-flight generation on disconnect (orphaned task).
**Subtasks:**
- [ ] Disconnect → cancel token → gateway aborts provider stream → capacity released (coordinator slot, quota reservation, breaker state).
- [ ] Cancellation-safe providers in the adapter contract; no orphaned-task pattern anywhere.
- [ ] Tests: disconnect frees the slot; provider aborted; task-count assertions.
**Acceptance:** no in-flight generation survives its client.

### P4-5 — Stream idempotency
**Status:** `[ ]` · **Depends:** P1-2, P4-2 · **Arch:** §9.1, §7.1
**Subtasks:**
- [ ] Request-id dedup on the stream path; reconnect with same request-id continues, never duplicates.
- [ ] Tests: retry after 5xx on stream path.
**Acceptance:** stream path idempotent and resume-safe.

### P4-6 — Input moderation pipeline
**Status:** `[ ]` · **Depends:** P0-4 · **Arch:** §9.1, §12, §2.7
**Subtasks:**
- [ ] Synchronous order: regex fastpath → classifier → jailbreak scan → guardrail stack (cheap to heavy, per-tenant rails); blocked input → hardcoded refusal + evidence.
- [ ] Deterministic before probabilistic (§2.7).
- [ ] Tests: ordering, refusal, evidence emission.
**Acceptance:** input fully screened before any model call.

### P4-7 — Turn completion + event outbox
**Status:** `[ ]` · **Depends:** P3-5, P4-2 · **Arch:** §9.1
**Subtasks:**
- [ ] Completion: durable message finalized with usage (P0-10); ledger + quota reconciliation async; webhook/event contract fires; summary refresh queued (P2-5).
- [ ] Transactional outbox: `conversation.created`, `escalation.raised`, `eval.failed` written with the transaction → Redis stream → worker + webhooks; no lost events on crash (EU-AI-Act Art. 12).
- [ ] Tests: ordering, async ledger, webhook delivery, outbox crash-safety.
**Acceptance:** completion side-effects asynchronous and reliable.

### P4-8 — Turn failure semantics
**Status:** `[ ]` · **Depends:** P0-6, P3-3 · **Arch:** §9.3
**Subtasks:**
- [ ] LLM failure: timeout → retries → breaker → fallback chain → degrade (queue / escalate / offline capture — tenant-configurable).
- [ ] Guardrail failure: fail-closed for PII + policy layers; fail-open only where tenant explicitly configures a non-authoritative layer.
- [ ] Escalation carries full durable thread + attempted resolutions + recommended next step.
- [ ] Tests per branch.
**Acceptance:** turn degradation tenant-configurable, never silent.

### P4-9 — Non-streaming path parity
**Status:** `[ ]` · **Depends:** P4-3 · **Arch:** §9.2
**Subtasks:**
- [ ] Same pipeline without deltas; full-output validation before return (bounded re-ask, max N, then escalate).
- [ ] Tests: same moderation gates on non-stream responses.
**Acceptance:** no bypass through the non-streaming API.

---

## 10. Phase 5 — Governance plane & tenancy at scale (Arch §6, §12, §14)

**Goal:** tenant config is the product; PII rules non-negotiable; tool calls authorized deterministically; isolation enforced + tested; lifecycle automated; compliance current. **Exit criteria:** default-deny end-to-end; cross-tenant leakage impossible by construction + tested; config eval-gated; DSR automated.

### P5-1 — Compiled tenant config
**Status:** `[ ]` · **Depends:** P0-11, P1-9 · **Arch:** §12, §6
**Subtasks:**
- [ ] Versioned config schema: input rails, output validators, escalation policy, model catalog policy, tool allowlists, budgets, brand voice, knowledge allowlists — per surface.
- [ ] Compile step → ready-to-run rails/validators/catalogs/budgets (cached, invalidated on publish); deny-by-default if unconfigured (P0-4).
- [ ] Config JSON Schema in `contracts/schemas/` + runtime validation.
- [ ] Tests: compile correctness, schema validation, default-deny.
**Acceptance:** one compiled artifact per surface version; invalid config cannot publish.

### P5-2 — Config pipeline endpoints
**Status:** `[ ]` · **Depends:** P5-1 · **Arch:** §13, §12
**Subtasks:**
- [ ] Edit → validate → eval suite (P6-6) → canary % (P6-5) → promote → auto-rollback on regression; immutable + revertible versions.
- [ ] Approvals for privileged changes (audit).
**Acceptance:** config promotion eval-gated and revertible.

### P5-3 — Tool authorization gate
**Status:** `[ ]` · **Depends:** P2-9 · **Arch:** §12, §2.8
**Why:** *Do-not-repeat:* legacy had no tool-call authorization.
**Subtasks:**
- [ ] Tool registry + per-tenant/per-surface allowlists; deterministic check before any side-effecting call; denials audited.
- [ ] Tool calls + results persisted as first-class parts each turn.
- [ ] Tests: allowlist enforcement, denial audit, part persistence.
**Acceptance:** authorization separate from verification; nothing side-effecting runs unallowed.

### P5-4 — PII rules (full policy)
**Status:** `[ ]` · **Depends:** P0-5, P4-3 · **Arch:** §12
**Subtasks:**
- [ ] Redact at ingress (before storage or send); redact at egress rolling window (P4-3); model context redacted-only (P0-5).
- [ ] Traces/evals/replay corpora redacted before persistence, including at the assembler boundary.
- [ ] Raw content access-controlled (review/DSR only); access audit-logged.
- [ ] Tests: trace persistence contains no raw PII.
**Acceptance:** the four PII rules hold by construction; raw access narrow + audited.

### P5-5 — Isolation primitives
**Status:** `[ ]` · **Depends:** P1-1, P1-8 · **Arch:** §6.3
**Subtasks:**
- [ ] Tenant-context resolution is a single mandatory middleware step; downstream components receive it as an immutable field.
- [ ] Redis prefixes `tenant:{id}:` everywhere; end-user keys add `end_user:{id}`.
- [ ] Vector namespaces per tenant + KB; retrieval filters by surface knowledge allowlist.
- [ ] Object storage per-tenant prefixes; archive/export confined to tenant prefix.
- [ ] Trace spans carry tenant+surface; redaction before persistence.
- [ ] Cross-tenant leakage suite: DB, Redis, vector, storage, traces, evals.
**Acceptance:** negative cross-tenant tests pass for every store.

### P5-6 — Postgres Row-Level Security
**Status:** `[ ]` · **Depends:** P1-1 · **Arch:** §6.3.2
**Subtasks:**
- [ ] RLS on tenant tables; `SET app.tenant_id` per session; policies per table; app role cannot bypass; super-admin separate role.
- [ ] Tests (Postgres): RLS blocks cross-tenant access even with buggy queries.
**Acceptance:** RLS is defense-in-depth beneath scoped repositories.

### P5-7 — Budget hierarchy integration
**Status:** `[ ]` · **Depends:** P3-6 · **Arch:** §6.3.8, §10
**Subtasks:**
- [ ] Platform > tenant > surface > end-user end-to-end; rejection at any level; UI visibility (P7-4).
- [ ] Tests: surface over budget while tenant fine → surface requests rejected.
**Acceptance:** budget levels compose correctly.

### P5-8 — Endpoint isolation
**Status:** `[ ]` · **Depends:** P1-8 · **Arch:** §6.3.10
**Subtasks:**
- [ ] Operator endpoints reject tenant keys; customer endpoints never accept operator credentials; cross-surface token misuse rejected.
- [ ] Tests: credential class cross-use → 401/403.
**Acceptance:** no credential class usable outside its endpoint class.

### P5-9 — Evidence & audit
**Status:** `[ ]` · **Depends:** P0-4 · **Arch:** §12, EU-AI-Act Art. 12
**Subtasks:**
- [ ] Evidence packets on every guardrail/policy/tool-gate/quota decision, per tenant + end-user scoped, validated against `contracts/schemas/evidence-packet.schema.json`.
- [ ] Immutable audit trail covering config publishes, key rotations, DSR actions.
- [ ] Retention policy per tenant (ties P6-8).
- [ ] Tests: every decision class emits a valid packet.
**Acceptance:** decisions reconstructable from evidence alone.

### P5-10 — Tenant lifecycle automation
**Status:** `[ ]` · **Depends:** P1-1, P1-8 · **Arch:** §14
**Subtasks:**
- [ ] Onboarding: tenant + surfaces + end-user domain + vector namespaces + object prefixes + budgets + default-deny config + operator keys.
- [ ] In-life: delegated admin; tenant-scoped metrics.
- [ ] End-user DSR: export or erase one end-user's threads/memory/metadata without affecting others (GDPR).
- [ ] Offboarding: erase or archive per contract; revoke keys; drop namespaces; retain audit/evidence per retention.
- [ ] Tests: onboarding checklist, DSR isolation, offboarding cleanup.
**Acceptance:** lifecycle automated and tested; DSR per end-user, not per tenant.

### P5-11 — Compliance posture
**Status:** `[ ]` · **Depends:** P5-9 · **Arch:** §14
**Subtasks:**
- [ ] **Art. 50 (due now):** bot/AI-interaction disclosure — widget notice (P7-1), hosted page footer, API metadata; AI-generated-content marking on exports.
- [ ] Art. 12 logging (P5-9); Art. 26 human oversight (escalation); Art. 72 post-market monitoring (P6-4 drift); Art. 73 incident reporting hook.
- [ ] Annex III posture (Dec 2027): per-tenant risk-assessment artifact, documented adversarial testing (P6-6), deployer documentation.
- [ ] Re-verify legal status against the EU AI Act Service Desk before tenant contracts (§17 volatility).
**Acceptance:** compliance checklist current as of last re-verification date.

### P5-12 — Deployment shapes & residency
**Status:** `[ ]` · **Depends:** — · **Arch:** §6.5, §11
**Subtasks:**
- [ ] Region field on tenant; onboarding pins region; archive pinned to tenant region.
- [ ] Dedicated-tenant deployment option documented + configurable (own DB/vector/gateway, same control plane) — data-plane boundary moves, architecture does not.
- [ ] Tests: residency pinning honored for writes and archives.
**Acceptance:** residency pinning works from L1; dedicated shape is config, not a fork.

---

## 11. Phase 6 — Operations plane (Arch §13)

**Goal:** operator tooling behind the same policy/PII boundaries; observability, SLOs, quality monitoring, evals in CI, config canary, ops infra. **Exit criteria:** sampled traces end-to-end, metrics exported, SLOs alerting, eval gate in CI, docker/CI/Terraform baselines merged.

### P6-1 — Tracing
**Status:** `[ ]` · **Depends:** P0-12 · **Arch:** §13, §12 PII rule 4
**Subtasks:**
- [ ] Spans: guardrails → retrieval → LLM → policy → handoff; tenant+surface tagged; PII redaction before persistence (reuse P0-5).
- [ ] Head sampling for errors/guardrail events; per-tenant sample rate.
- [ ] Tests: trace payloads contain no raw PII.
**Acceptance:** traces cover the full request path; redaction at the boundary.

### P6-2 — Metrics
**Status:** `[ ]` · **Depends:** P0-12 · **Arch:** §13
**Subtasks:**
- [ ] Prometheus/OTel: TTFT, inter-token latency, guardrail hit rates, cost per conversation, compaction frequency, queue depth, DLQ, token usage, cache hit rates.
- [ ] Per-tenant dashboards (P7-4) fed by these.
**Acceptance:** metrics exported; per-tenant dashboards render.

### P6-3 — SLOs & alerting
**Status:** `[ ]` · **Depends:** P6-2 · **Arch:** §13
**Subtasks:**
- [ ] SLOs: TTFT p95, end-to-end p95 (<1.5s), stream completion rate, error rate; error budgets.
- [ ] Alerts: error-rate, cost-spike, block-rate anomaly, drift, queue depth, DLQ, Redis breaker state.
**Acceptance:** alerts fire on budget exhaustion; runbooks exist (P6-9).

### P6-4 — Production quality monitoring
**Status:** `[ ]` · **Depends:** P6-2 · **Arch:** §13, §17
**Subtasks:**
- [ ] LLM-as-judge on sampled traffic (in-scope, on-brand, helpful); judge calibration + rubric versioning.
- [ ] Drift detection over time; automatic re-escalation of degraded turns.
- [ ] Bad-compaction detection surfaced (ties P2-10).
**Acceptance:** quality tracked; drift alerts actionable.

### P6-5 — Config canary pipeline (ops side)
**Status:** `[ ]` · **Depends:** P5-2, P6-6 · **Arch:** §13
**Subtasks:**
- [ ] Canary % of traffic, promote, auto-rollback on regression; immutable versions.
- [ ] Rollback drill.
**Acceptance:** config promotion gated and revertible in production.

### P6-6 — Eval harness in CI
**Status:** `[ ]` · **Depends:** P0-12 · **Arch:** §13, §2.9, EU-AI-Act (documented adversarial testing)
**Subtasks:**
- [ ] Golden datasets: injection, jailbreak, PII, off-topic, sensitive topics, multilingual, system-prompt-leak, encoded attacks.
- [ ] RAGAS suite (faithfulness, answer relevance, context precision/recall).
- [ ] Garak runner (probe families from kept configs) + PyRIT runner (crescendo etc.), scheduled, results persisted.
- [ ] Red-team release gate: critical failures block deployment.
- [ ] Eval-case creation from sampled production incidents.
- [ ] Tests: harness executes in CI; gates enforce.
**Acceptance:** every config/prompt/model change runs the suite; gates block regressions.

### P6-7 — Eval datasets from production (redacted)
**Status:** `[ ]` · **Depends:** P6-6 · **Arch:** §12 PII rule 4, §13
**Subtasks:**
- [ ] Sampled, redacted traces → replay corpora (ties P7-5).
- [ ] Per-tenant eval isolation.
**Acceptance:** corpora redacted; tenant-scoped.

### P6-8 — Retention & archive automation
**Status:** `[ ]` · **Depends:** P1-7 · **Arch:** §11, §14
**Subtasks:**
- [ ] Tenant-configurable retention for threads, traces, logs, evidence; automated deletion/archive jobs.
- [ ] GDPR export/erase automation (ties P5-10); restore verified.
**Acceptance:** retention honored per tenant; deletion drills pass.

### P6-9 — Ops infrastructure
**Status:** `[ ]` · **Depends:** P0-12 · **Arch:** §16
**Subtasks:**
- [ ] CI/CD: lint, typecheck, mypy, pytest, security scans, SBOM, image build/push, deploy (staging).
- [ ] Terraform baseline (AWS): RDS Postgres 18 + pgvector, ElastiCache Redis, S3, KMS, WAF.
- [ ] Monitoring stack: Prometheus/Grafana/Loki/OTel collector.
- [ ] Backups + DR drills (RPO/RTO targets); HA: multi-replica API, DB failover, Redis cluster.
- [ ] Load testing (k6): p95 < 1.5s end-to-end, soak; concurrency targets from §15 L1.
- [ ] Secrets: Vault/KMS/SSM (ties P0-8).
**Acceptance:** staging deployable end-to-end from CI; runbooks + drills exist.

---

## 12. Phase 7 — Surfaces (Arch §5)

**Goal:** five surfaces: widget, hosted page, public chat API, admin UI, harness workbench. **Exit criteria:** widget streams over SSE with session tokens + Art. 50 disclosure; public API OpenAI-compatible; admin UI complete; harness internal-only.

### P7-1 — Widget (customer surface)
**Status:** `[ ]` · **Depends:** P1-8, P4-1 · **Arch:** §5, §6.4
**Subtasks:**
- [ ] SSE streaming client: `Last-Event-ID` reconnect + backoff, typing indicator, message states.
- [ ] Session-token bootstrap (P1-8); per-device anonymous identity; **no `api-key` attribute exists**.
- [ ] Bot-disclosure notice ("You are chatting with an AI") — Art. 50 (ties P5-11).
- [ ] Accessibility WCAG 2.2 AA.
- [ ] Feedback capture (thumbs up/down + free text → eval datasets).
- [ ] Input length caps, sanitized rendering, CSP-friendly embed, no PII in URLs.
- [ ] Theming; i18n later.
- [ ] Tests: reconnect, token refresh, disclosure visible, sanitization.
**Acceptance:** production-safe on untrusted pages with no static secrets.

### P7-2 — Hosted chat page
**Status:** `[ ]` · **Depends:** P7-1 · **Arch:** §5
**Subtasks:**
- [ ] Tenant-branded page on tenant domain/subdomain reusing the widget engine; multi-device continuity via channel-based threads (P1-8).
- [ ] Same token + SSE path; per-tenant theme/brand assets.
**Acceptance:** tenant points their own domain at the hosted page.

### P7-3 — Public chat API (OpenAI-compatible)
**Status:** `[ ]` · **Depends:** P4-1, P4-9 · **Arch:** §5
**Subtasks:**
- [ ] OpenAI-compatible envelope: chat completions (non-stream + SSE), tool calls, structured output; tenant API keys on this surface only.
- [ ] Envelope versioning; additive-only; OpenAPI published.
- [ ] Tests: compatibility fixtures, streaming parity.
**Acceptance:** tenants integrate with standard OpenAI SDKs.

### P7-4 — Admin UI
**Status:** `[ ]` · **Depends:** P5-2, P6-2 · **Arch:** §5
**Subtasks:**
- [ ] Pages: tenant config editor (P5-1), policy editor (draft→review→publish, diffs, simulation) (P5-2), traces explorer (P6-1), escalation queue, audit log viewer, model catalog UI, evals UI (P6-6), usage/billing view (P3-5), circuit-breaker state.
- [ ] SSO (OIDC) + MFA for privileged roles; role-aware navigation.
- [ ] Tests: route coverage, RBAC rendering.
**Acceptance:** operators manage everything via UI.

### P7-5 — Harness workbench (internal only)
**Status:** `[ ]` · **Depends:** P1-5, P6-7 · **Arch:** §13, §5
**Subtasks:**
- [ ] Provider testing, model comparison, prompt iteration and A/B, session fork/replay, redacted-trace replay, adversarial sweeps — on the same session/context engine.
- [ ] Evaluate Claude Agent SDK `resume`/`fork_session` + `SessionStore` for Claude-routed workbench flows vs the OpenCode-derived fork/replay; record decision (D-10).
- [ ] AGENTS.md boundary enforced: operator sessions never serve customer traffic, never bypass runtime authorization; no production tenant state in OpenCode/harness.
- [ ] Tests: harness traffic tagged internal; cannot reach customer endpoints.
**Acceptance:** workbench is a real internal tool with zero customer-runtime exposure.

---

## 13. Phase 8 — Data plane & scaling readiness (Arch §11, §15)

**Goal:** L1 solid, L2/L3 designed for now so no rewrite later. **Exit criteria:** write discipline in place; replicas designed; sharding design doc exists; residency honored.

### P8-1 — Write-minimization discipline
**Status:** `[ ]` · **Depends:** P1-6 · **Arch:** §11
**Subtasks:**
- [ ] High-frequency updates ("last active", read receipts) batched through Redis, never per-event writes.
- [ ] Audit: primary writes are conversation appends + config/spend only.
- [ ] Tests: read-path writes stay on hot tier.
**Acceptance:** primary write load minimized by construction.

### P8-2 — Schema discipline audit
**Status:** `[ ]` · **Depends:** P1-1 · **Arch:** §11
**Subtasks:**
- [ ] ULID/sequence ordering verified; vertical partitioning confirmed; composite `(tenant_id, ...)` index audit.
**Acceptance:** index audit recorded; no scan-heavy tenant queries.

### P8-3 — Read replicas (L2)
**Status:** `[ ]` · **Depends:** P6-9 · **Arch:** §11, §15 L2
**Subtasks:**
- [ ] Postgres replicas + read routing (history → replicas; read-your-writes window → primary); high/low-priority pools; connection pooling.
**Acceptance:** read traffic scales off the primary; lag bounded.

### P8-4 — Turn-log sharding design (L3 readiness)
**Status:** `[ ]` · **Depends:** — · **Arch:** §11, §15 L3
**Subtasks:**
- [ ] Design doc: turn-log sharding by tenant hash, trigger conditions, migration path. No code; schema already shard-compatible (P1-1).
**Acceptance:** design reviewed; L1 schema shard-compatible.

### P8-5 — Multi-region & residency
**Status:** `[ ]` · **Depends:** P5-12 · **Arch:** §6.5, §15
**Subtasks:**
- [ ] L1: residency pinning verified (P5-12).
- [ ] L2+: active-active design doc (regional primaries, replica fan-out); deferred per §17.
**Acceptance:** residency honored today; active-active a documented L2+ plan.

### P8-6 — Self-hosted inference tier (L3, no code)
**Status:** `[ ]` · **Depends:** — · **Arch:** §15 L3
**Subtasks:**
- [ ] Evaluation note: KV-cache cancellation, continuous batching — only if Neryva runs its own GPUs (D-8).
**Acceptance:** decision recorded.

---

## 14. Phase 9 — Non-goals / explicitly deferred (do NOT schedule)

- SCIM provisioning, multi-org parent/subtenant hierarchy (P1 features, post-L1).
- Multi-agent supervisor topologies (P2).
- Dedicated vector service (Qdrant) until the pgvector/pgvectorscale ceiling is hit (`stack.md` decision flow).
- Batch API, SLA tiers, chargeback reports, SDKs, sandbox, docs portal (post-L1, revenue-driven).
- SIEM export, compliance presets (HIPAA/PCI) until a customer demands them.
- Hybrid retrieval + cross-encoder reranking (feature-matrix 7.2/7.3, P1): single-stage retrieval ships first; add BM25/tsvector fusion + rerank when recall evals (P6-6) demand it.
- Prompt management portal (versioned prompts, A/B in UI): harness A/B (P7-5) covers iteration today; portal is a P1 item.
- MCP client integration (feature-matrix 6.3, P1): MCP servers become a tool source behind the P5-3 gate; prototype via the harness first.
- Helpdesk channel integrations (Zendesk/Jira/ServiceNow) beyond generic webhook/Slack/Teams/SMTP (feature-matrix 8.2, P1).

---

## 15. Open decisions log (Arch §17)

| ID | Decision | Blocks | Status |
|---|---|---|---|
| D-1 | Gateway: build bespoke vs adopt LiteLLM (re-costed vs Rust core) | P3-2…P3-8 | [ ] |
| D-2 | Memory: bespoke worker vs Anthropic first-party memory tool | P2-8 | [ ] |
| D-3 | Streaming moderation window latency knob default | P4-3 | [ ] |
| D-4 | Semantic cache invalidation strategy + tests | P3-7 | [ ] |
| D-5 | Multi-region timing (L2+ trigger) | P8-5 | [ ] |
| D-6 | Regulatory re-verification cadence | P5-11 | [ ] |
| D-7 | LangGraph server economics re-check | P5/P6 | [ ] |
| D-8 | Self-hosted inference tier trigger (L3) | P8-6 | [ ] |
| D-9 | RLS timing (now vs L2) | P5-6 | [ ] |
| D-10 | Harness fork/replay: OpenCode-derived vs Claude Agent SDK | P7-5 | [ ] |
| D-11 | Guardrail service registries: per-process (L1) vs Redis-backed now | P0-13/P5-5 | [ ] |
| D-12 | Thinking clearing: Anthropic context-editing API vs bespoke | P2-7 | [ ] |

---

## 16. Traceability matrix (triple-check: architecture requirement → tasks)

Every load-bearing statement in `architecture-v2.md` must appear here. If a requirement is missing from this table, add a task before closing the phase.

| Arch § | Requirement | Tasks |
|---|---|---|
| §1 | One session/context engine, two surfaces, governance plane | P1, P2, P5, P7 |
| §2.1 | Engine = session log | P1-1, P1-2 |
| §2.2 | Model-agnostic | P3-1 |
| §2.3 | No single filter is a boundary (layered) | P4-6, P5-4, P5-9 |
| §2.5 | Deny by default | P0-4, P5-1 |
| §2.6 | Never send what the model never needs (redaction) | P0-5, P5-4 |
| §2.7 | Deterministic before probabilistic | P4-6 |
| §2.8 | Authorize separate from verify | P5-3 |
| §2.9 | Every stage a test target | P6-6, P3-10, P2-10 |
| §2.10 | Operator tooling separate | P7-5 |
| §3 | Reference foundations adoption | P1 (session), P2 (context), P3 (gateway), P4 (streaming), P8 (data) |
| §4 | L0-L7 layers | P0-P8 as mapped |
| §5 | Five surfaces | P7-1…P7-5 |
| §6.1 | Hierarchy platform>tenant>surface>end-user | P1-9, P1-1 |
| §6.2 | Data ownership matrix (tenant-scoped rows) | P1-1, P5-5 |
| §6.3.1 | Single tenant-context resolution step | P5-5 |
| §6.3.2 | Row-level isolation | P1-1, P5-6 |
| §6.3.3 | Vector namespace isolation | P5-5 |
| §6.3.4 | Cache key isolation | P5-5, P1-6 |
| §6.3.5 | Object storage prefixes | P5-5, P1-7 |
| §6.3.6 | Trace isolation | P6-1, P5-5 |
| §6.3.7 | Eval isolation | P6-7 |
| §6.3.8 | Budget hierarchy (4 levels) | P3-6, P5-7 |
| §6.3.9 | Secret isolation (BYOK) | P0-8 |
| §6.3.10 | Endpoint isolation | P5-8 |
| §6.4 | End-user model | P1-8, P5-10 |
| §6.5 | Deployment shapes + residency | P5-12, P8-5 |
| §7.1 | Append-only log, parts, sequences, pagination, dedup, regeneration, fork | P1-1…P1-5 |
| §7.2 | Session coordinator | P1-3 |
| §7.3 | Hot/durable/cold layout | P1-6, P1-7, P8-1 |
| §8.1 | Context assembler: budget, redacted-only, token estimate | P2-1, P2-2, P0-5 |
| §8.2 | Compaction: triggers, mechanics, atomicity, cache discipline, breaker/truncate, chunk-and-merge, instant | P2-3…P2-6 |
| §8.3 | Tool-result clearing (+ thinking) | P2-7 |
| §8.4 | Memory opt-in, PII-filtered, retrieval-based | P2-8 |
| §9.1 | SSE path: gate, admit, assemble, buffer, moderate, complete, reconnect, cancel | P4-1…P4-7 |
| §9.2 | Non-streaming parity | P4-9 |
| §9.3 | Failure semantics (degrade, fail-closed, escalate) | P0-6, P4-8 |
| §10 | Gateway: interface, routing <30ms, tiering, 3 fallback classes, breakers, usage, ledger, quota, caches, tenancy mapping, build-vs-adopt | P3-0…P3-10 |
| §11 | Data plane: hot/durable/cold, write discipline, replicas, schema discipline | P1-6, P1-7, P8-1…P8-4 |
| §12 | Governance: config is product, PII rules, tool gate, evidence/audit | P5-1…P5-9 |
| §13 | Harness: operator sessions, config pipeline, observability, quality monitoring | P7-5, P5-2, P6-1…P6-6 |
| §14 | Lifecycle: onboarding, DSR, offboarding, regulatory | P5-10, P5-11 |
| §15 | L1/L2/L3 | P8-3…P8-6 |
| §16 | Build plan phases | P0-P6 aligned |
| §17 | Open risks | §15 decisions + P4-3, P2-10, P3-7, P5-11 |

---

## 17. Cross-phase verification strategy

| Layer | Tooling | Where |
|---|---|---|
| Unit | pytest (backend), vitest (frontend) | every task's tests |
| Integration | real pipeline through the new API (Postgres in compose) | phase exit |
| Contract | JSON Schema validation + OpenAPI pin test | every contract change |
| Isolation | cross-tenant negative tests per store | P5-5 |
| Security | Garak/PyRIT sweep + red-team release gate | P6-6 |
| Quality | LLM-as-judge sampled, RAGAS | P6-4, P6-6 |
| Performance | k6 p95<1.5s, routing <30ms | P6-9, P3-2 |
| Compliance | evidence replay, audit export, DSR drills | P5-9, P5-10, P5-11 |
| Disaster | restore drill, rollback drill, failover drill | P6-5, P6-9 |
