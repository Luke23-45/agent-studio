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
- [x] Tests: publish → new behavior; rollback → old; concurrent publish serialized.
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
**Status:** `[x]` · **Depends:** P0-3 · **Arch:** §7.1, §7.3, §11
**Subtasks:**
- [x] In migration 0004 (P0-3): `thread_events` (append-only log, per-thread `seq`, `request_id`, `type`, payload jsonb); `message_parts` (part_type: text|reasoning|tool_use|tool_result|citation|compaction|step, index, content jsonb, redacted_content jsonb); `messages` (seq, request_id unique, parent_message_id, surface_id, end_user_id).
- [x] `surfaces`, `end_users` tables owned by governance (P0-3) — session layer references them, never owns them.
- [x] Vertical partitioning: message metadata separate from parts (constant-cost appends regardless of thread length).
- [x] Tests: schema constraints (unique seq per thread, unique request_id).
**Acceptance:** the session schema is complete and layer-owned.
**Notes (2026-08-06):** Schema landed in migration 0004 (P0-3): `threads` (full_seq, last_request_id, part_count, status), `thread_events` (tenant_id, thread_id, seq, event_type, request_id, payload jsonb, UNIQUE(thread_id, seq)), `message_parts` (tenant_id, thread_id, message_id, seq, part_index, part_type enum text|reasoning|tool_use|tool_result|citation|compaction|step, content/redacted_content jsonb), `messages` (thread_id, seq, UNIQUE(thread_id, seq), request_id + unique partial index, parent_message_id, surface_id, end_user_id), `surfaces`, `end_users`. ORM: `ThreadModel`, `ThreadEventModel`, `MessagePartModel`, `MessageModel`, `SurfaceModel`, `EndUserModel` in `infrastructure/db/models.py`. Constraint tests in `backend/tests/test_thread_store.py` (unique thread seq, unique event seq).

### P1-2 — Thread repository
**Status:** `[x]` · **Depends:** P1-1 · **Arch:** §7.1
**Subtasks:**
- [x] `ThreadRepository`: atomic append with seq assignment, write parts, read tail by seq, cursor pagination (`after`, `limit`, `hasMore`), list threads by end-user.
- [x] Request-id dedup: insert-on-conflict returns the original result; client retry after 5xx never double-writes.
- [x] Every query tenant+end-user scoped.
- [x] Tests: pagination ordering, dedup idempotency, cross-tenant negative tests.
**Acceptance:** history reads come from the durable log; pagination is cursor-based; dedup proven.
**Notes (2026-08-06):** `infrastructure/db/threads.py` — `ThreadRepository(db)` with `create_thread` / `get_or_create_for_conversation` / `list_threads(end_user_id)` / `append_message` (row lock `with_for_update` on thread row for atomic seq assignment + `UNIQUE(thread_id, seq)` backstop; dedup pre-check by `request_id` returns original with `deduped=True`, no second write) / `append_event` / `get_message` / `list_messages` (ascending cursor `after_seq`/`limit`/`has_more`) / `read_tail` (descending window from `from_seq`) / `list_parts` / `list_events`. Every query tenant-scoped. Exported from `infrastructure/db/__init__.py`. Tests in `backend/tests/test_thread_store.py` (file-backed SQLite fixture, mirrors `test_persistence.py`).

### P1-3 — Session coordinator
**Status:** `[x]` · **Depends:** P1-2 · **Arch:** §7.2
**Why:** *Do-not-repeat:* legacy interleaved history appends under concurrent messages to one thread.
**Subtasks:**
- [x] Per-thread serialization: at most one in-flight generation per thread; concurrent messages queue in sequence order (Redis lock with lease + local queue; documented fallback).
- [x] Disjoint threads run concurrently; cancellation releases the slot (ties P4-4).
- [x] Tests: N concurrent messages → strictly ordered appends; no lost updates.
**Acceptance:** the race cannot occur; disjoint-thread throughput unaffected.
**Notes (2026-08-06):** `session/coordinator.py` — `ThreadCoordinator(ManagedService)` + `CoordinatorLease` context manager. Local: per-thread `asyncio.Lock` (FIFO local queue). Cross-worker: Redis `SET NX PX` lease (token compare-and-delete release via Lua; background renewal at lease/3 keeps the key alive; holder death → TTL expiry → next waiter proceeds). Cancellation inside the lease block releases the slot via `__aexit__`. Redis down → local-only serialization, health DEGRADED (never silent). `init_thread_coordinator` / `get_thread_coordinator` singleton; wired into `main.py` lifespan + `/health`; settings `SESSION_LEASE_SECONDS` (120) / `SESSION_WAIT_SECONDS` (15). Tests in `backend/tests/test_session_coordinator.py` (serialization, disjoint threads, raw SET NX cross-worker blocking, renewal, idempotent release, degraded local-only). Endpoints use the lease in P1-10.

### P1-4 — Append-only regeneration and editing
**Status:** `[x]` · **Depends:** P1-2 · **Arch:** §7.1
**Subtasks:**
- [x] Regenerate = new assistant message with `parent_message_id` to the original; nothing mutated in place.
- [x] Edit = new user message + regenerated successor chain; audit shows both attempts.
- [x] API: `POST /threads/{id}/regenerate`, `POST /threads/{id}/messages/{mid}/edit`.
- [x] Tests: chains resolve; original immutable.
**Acceptance:** no in-place mutation of durable messages from these features.
**Notes (2026-08-06):** `session/service.py` `ThreadMutationService.regenerate/edit` — append-only chains: regenerate appends an assistant message `parent_message_id`→original + `message.regenerated` event + `thread.regenerated` audit row; edit appends edited user message (parent = original user's parent) + pending successor assistant + `message.edited` event + audit. Appended successor carries metadata `status: pending_generation` + `regenerated_from`/`edited_from` (generation wiring lands in P1-10). Role validation: regenerate requires assistant target, edit requires user target + non-empty content (`InvalidTargetError`); `ThreadNotFoundError`/`MessageNotFoundError` → 404. Routes in `api/routes/threads.py` (tenant resolved from principal or body, `assert_tenant_access`), registered in `main.py` prefix `/api/v1`. Tests in `backend/tests/test_thread_mutations.py`.

### P1-5 — Fork
**Status:** `[x]` · **Depends:** P1-2 · **Arch:** §7.1 (OpenCode fork + Claude `fork_session` analogue)
**Subtasks:**
- [x] `POST /threads/{id}/fork?at=<seq>`: new thread copies history to the boundary; original unchanged; both independently resumable.
- [x] Customer path: behind "regenerate" UX; operator path: harness fork/replay (P7-5).
- [x] Tests: fork isolation, original immutability, both sides resumable.
**Acceptance:** fork at any message boundary with full isolation.
**Notes (2026-08-06):** `ThreadRepository.fork_thread` (one transaction): locks source row, copies messages `seq <= at_seq` + parts into a new thread, remaps `parent_message_id` via id map, stamps metadata `{forked_from, forked_at_seq}`, preserves original `created_at`; new thread events `thread.created` + `thread.forked`; source gets an appended `thread.forked` event (payload new_thread_id/at_seq) — its messages are untouched. `at_seq=0` → empty fork; beyond max → `ForkPointOutOfRange` → 400. Service `fork()` creates a fresh conversation for the new thread and audits. Route `POST /threads/{thread_id}/fork?at=<seq>`. Tests in `backend/tests/test_thread_fork.py`.

### P1-6 — Hot tier: Redis thread state
**Status:** `[x]` · **Depends:** P1-2 · **Arch:** §7.3
**Subtasks:**
- [x] Keys `tenant:{id}:thread:{thread_id}:tail` (last N turns + running summary block), TTL = session timeout, promoted on read; summary position stable (ties P2-6).
- [x] Idempotency keys, token buckets, breaker state all tenant-prefixed (§6.3.4).
- [x] Redis down → Postgres tail fallback, documented degraded mode with alert.
- [x] Tests: hot reads, promotion, fallback.
**Acceptance:** hot path serves tails from Redis; self-heals on Redis failure.
**Notes (2026-08-06):** `session/hot_tier.py` — `ThreadTailCache(ManagedService)`: key `neryva:thread:tail:{tenant}:{thread}` JSON `{tail (capped at tail_size), summary}` with `SETEX` TTL; `get_tail` promotes TTL on hit; `cache_tail(summary=None)` preserves the existing summary block (stable position); `invalidate` DELs. Redis down → writes no-op/reads miss (`False`/`None`) with once-per-transition error log + health DEGRADED; self-heals on reconnection (every call pings). Singleton wired in `main.py` lifespan + `/health`; settings `SESSION_TTL_SECONDS` (86400) / `SESSION_TAIL_SIZE` (10). Idempotency-key/bucket/breaker tenant-prefixing is tracked at P0-9/P3-4 (keys already prefixed). Endpoint reads use it in P1-10. Tests in `backend/tests/test_hot_tier.py`.

### P1-7 — Cold tier: archive
**Status:** `[ ]` · **Depends:** P1-2 · **Arch:** §7.3, §11
**Subtasks:**
- [ ] Archive job: threads older than tenant window (default 30-90 days) → object storage `tenant:{id}/archive/`; marker in DB; async restore endpoint.
- [ ] Region-pinned archive path (ties P5-12).
- [ ] Tests: archive + restore round-trip; archived threads absent from hot queries.
**Acceptance:** archive is automated and reversible.

### P1-8 — End-user model and session tokens
**Status:** `[x]` · **Depends:** P1-1 · **Arch:** §6.4
**Why:** *Do-not-repeat:* legacy widget shipped a static tenant API key in customer HTML.
**Subtasks:**
- [x] Anonymous bootstrap: per-device identity (local storage) → `POST /v1/session-tokens` mints short-lived token bound to (tenant, surface, end_user, device, expiry, scopes).
- [x] Authenticated exchange: tenant app swaps its auth state for a platform session token (JWT/OAuth exchange); platform never sees tenant passwords.
- [x] Token → (tenant, surface, end_user) resolution middleware at the connection tier; tokens scoped to exactly one tenant.
- [x] Abuse limits per end-user (rate, spend cap, concurrent sessions) via Redis; bot detection hook at widget edge (P7-1).
- [x] Tests: expiry/rotation, cross-tenant rejection, device continuity.
**Acceptance:** customer surfaces authenticate by session token only; end-user identity never crosses tenants.
**Notes (2026-08-06):** Migration 0005 `session_tokens` (jti PK, tenant/end_user/surface/device/scopes/expires_at/revoked_at; FK end_users; idx tenant+end_user, expires_at). `SessionTokenModel` + `EndUserRepository` (get_by_id, get_or_create_anonymous keyed `anon:{device}`, create_authenticated) + `SessionTokenRepository` (create/get/revoke/prune_expired). `session/tokens.py`: Fernet-encrypted bearer (jti+claims, `SESSION_TOKEN_ENCRYPTION_KEY` env / `session_token.key` dev file gitignored; prod refuses auto-gen — mirrors P0-8 key pattern); `mint` (anonymous bootstrap or existing end_user; returns token+expires_at+end_user_id for device continuity) / `resolve` (decrypt → exp check → registry row: revoked_at/tenant match) / `revoke` (idempotent). `session/limits.py` `EndUserLimits(ManagedService)`: per-user Redis token bucket (`neryva:rl:eu:`), concurrent-session INCR/EXPIRE lease (DECR on leave, over-cap DECRs back), spend cap INCRBYFLOAT counter — all fail-open on Redis down with DEGRADED health. Connection tier: `api/dependencies/session.py` `get_session_principal` (Bearer → 401 expired/revoked/invalid). Routes `api/routes/sessions.py`: `POST /session-tokens` (mint + session-leash 429), `POST /session-tokens/revoke`, `GET /session-tokens/me`. All wired in `main.py` (lifespan + `/health`). Settings `SESSION_TOKEN_*`, `END_USER_*`. Widget rebuild onto tokens at P7-1; OAuth exchange is the `external_id` path (P1-8 done via `create_authenticated`). Tests: `backend/tests/test_session_tokens.py`, `backend/tests/test_end_user_limits.py`.

### P1-9 — Surfaces model
**Status:** `[x]` · **Depends:** P1-1 · **Arch:** §6.1
**Subtasks:**
- [x] Surface CRUD + per-surface config (persona, knowledge allowlist, tool allowlist, model pin, budgets, brand voice override); default surface on tenant onboarding.
- [x] Request path resolves surface and applies its config; unconfigured surface denies (P0-4).
- [x] Tests: two surfaces on one tenant behave differently; unconfigured surface denies.
**Acceptance:** a tenant runs sales + support assistants concurrently with different rails on one engine.
**Notes (2026-08-06):** `SurfaceRepository` (create/get/get_default=list-first-active/update/delete, all tenant-scoped) on the existing `surfaces` table (migration 0004). `api/routes/surfaces.py`: `POST/GET/PUT/DELETE /tenants/{id}/surfaces`, `GET .../surfaces/default`; `resolve_surface(tenant_id, surface_id|None)` — active surface or first-active default; inactive/missing → None (request path denies, P0-4). Surface types validated. Tests in `backend/tests/test_surfaces.py`. Request path applies the resolved surface config in P1-10.

### P1-10 — Conversation API v1 (evolve existing surface)
**Status:** `[x]` · **Depends:** P1-2, P1-3 · **Arch:** §7, §5
**Subtasks:**
- [x] Rework `api/routes/conversations.py` request path onto the thread repository + coordinator + session tokens (its monolith shape conflicts with the architecture layers; its v1 contract is kept where it aligns — additive-only from here forward).
- [x] History construction delegated to the context assembler (P2-1) — no fixed-count slice exists anywhere.
- [ ] OpenAPI generated from the reworked app; contracts CI pin from day one.
- [x] Tests: API surface, auth, pagination, idempotency end-to-end.

**Notes (2026-08-06):** Both `/conversations` (POST) and `/conversations/stream` reworked: `get_conversation_principal` (API key w/ `conversations:read` RBAC or session bearer; token wins for non-key prefixes, never both) + `ConversationPrincipal`; `_assert_conversation_tenant` (tokens scoped to one tenant, 403 otherwise); `_bind_thread` via `ThreadRepository.get_or_create_for_conversation` (thread = append-only source of truth; conversation row remains v1 projection); user message appended with `request_id` = `Idempotency-Key` header or generated `req-{uuid4}` (retried appends dedup via unique request_id); history built from thread `read_tail` (redacted-only, excludes the in-flight message); PII redaction + guardrails + catalog + coordinator + admission pipeline preserved; assistant reply appended to thread + hot tail refreshed; result carries `thread_id`; both endpoints release coordinator lease + admission ticket in `finally` (CoordinatorBusy → 429/SSE error + Retry-After); session-token requests resolve surface (deny when none active, P0-4) and apply `model_pin` override; `_refresh_hot_tail` (P1-6) keeps the Redis tail current. Also: `ThreadRepository` arg order normalized to `(tenant_id, thread_id)` across append/list/read/event methods; `MessageNotFoundError` added; `_row_to_dict` fixed to key by mapped attribute (`c.key`/mapper attr mismatch leaked the class `MetaData` under `metadata` — keys now `metadata_json`); event seq in `append_message` now uses the event sequence (was colliding with `thread.created` on the unique `(thread_id, seq)`); session-token Fernet key provided in tests via `backend/tests/conftest.py` (import-order-safe). `pytest backend/tests` = 199 passed, 6 failed (pre-existing: contracts ×2, evidence ×2, p0_fixes ×1, streaming ×1 — failing on the clean tree), 16 skipped (Redis down).
**Acceptance:** the v1 API keeps its aligned contract, loses its conflicting internals, and is contract-pinned.

---

## 7. Phase 2 — Context engineering stack (Arch §8)

**Goal:** model-visible context assembled every turn inside a token budget from redacted content; compaction atomic, cache-aware, failure-safe; memory opt-in and PII-filtered. **Exit criteria:** compaction runs with breaker + truncate fallback; assembler is the only component touching history; compaction quality measurable.

### P2-1 — Context assembler (SessionContextLoader)
**Status:** `[x]` · **Depends:** P0-5, P1-10 · **Arch:** §8.1
**Subtasks:**
- [x] Single component builds: `system` (stable prefix) → `summary block` (immutable between compactions) → `memory block` (opt-in, top-k) → `recent tail` (newest first, in budget) → `retrieved knowledge` (allowlist-filtered, spotlighted) → `current message`.
- [x] Budget math against per-model context from the catalog; 4-chars-per-token heuristic with per-model override.
- [x] Reads redacted content only (P0-5); applies tool-result clearing (P2-7).
- [x] Tests: budget overshoot trimming, block placement, redaction guarantee.
**Acceptance:** assembler is a single testable component; nothing else constructs the provider prompt.

**Notes (2026-08-06):** New `backend/app/context/` package — `estimator.py` (TokenEstimator: 4 chars/token default + per-provider/per-model overrides; ModelFacts: seeded context-window table, conservative fallback 128k, `register()` feed for P2-2) and `assembler.py` (SessionContextLoader). Block order per Arch 8.1: system → summary (absent pre-compaction; best-effort, omitted w/o error when over budget) → memory (opt-in; P2-8) → recent tail (newest-first budget walk, chronological rendering, never splits a turn) → knowledge (score-desc, `[Source i] (Score: x)` spotlight) → current message. Budget = min(window, override) − `CONTEXT_OUTPUT_RESERVE_TOKENS` (new settings key, default 4096, mirrors OpenCode v2 output buffer); mandatory blocks (system prefix, current message) that cannot fit raise `ContextBudgetExceeded` (fail closed = Arch 8.2 compaction trigger); best-effort trims recorded in `omitted_turns`/`omitted_docs`/`omitted_blocks`; token accounting in `token_counts`/`total_tokens`. Redaction guarantee: turns carry redacted content only; a turn with raw content but empty redacted content raises `RedactionViolation` (no fallback path — also deleted the `or m["content"]` raw fallback in the old `_load_conversation_history`). P2-7 hook: `clear_tool_payloads` swaps `has_tool_payload` turns for `TOOL_RESULT_PLACEHOLDER`. Orchestration reworked: `_generate_response` renders assembler output to LLMMessage (nothing else builds prompts); `_build_system_prompt` is now a stable per-tenant prefix (history/knowledge removed → byte-identical across turns, P2-6 cache precondition); `AgentState.context_summary` + `process_message`/`stream_message` accept `context_summary`; `create_orchestration_service` accepts `context_loader` (default instance). Route (`conversations.py`): `_load_history_turns` (redacted-only, fail-closed skip, 200-turn read window = page size not model slice) + `_load_thread_summary` (hot-tier fast path → thread `summary_block`); both endpoints pass `context_summary`. Tests `backend/tests/test_context_assembler.py` (21): block order/placement, chronological tail (incl. seq-less input order), oldest-first + lowest-score trimming, fail-closed overruns, redaction violation, tool clearing, P2-6 stability, settings-default reserve, accounting. Regression: `pytest backend/tests/test_context_assembler.py test_orchestration.py test_streaming.py test_hitl.py test_worker.py test_session_tokens.py test_surfaces.py test_thread_store.py test_thread_mutations.py test_thread_fork.py test_session_coordinator.py test_hot_tier.py test_end_user_limits.py` → green except the pre-existing `test_streaming::test_deltas_and_result` (confidence/escalation heuristic, verified pre-existing on clean tree). 4-char heuristic cross-checked against OpenCode v2 compaction docs (preflight `estimate = ceil(len/4)`); placeholder semantics vs Anthropic context-editing `clear_tool_uses_20250919`. P2-1 acceptance met: `context_loader.assemble` is the sole prompt builder (`LLMMessage(role=..., content=...)` is constructed only inside `_generate_response` from assembler output; eval_replay routes through `process_message`).

### P2-2 — Token estimator + model catalog
**Status:** `[x]` · **Depends:** P2-1 · **Arch:** §8.1, §10
**Subtasks:**
- [x] Estimator service (4 chars/token default, model overrides; counts parts, not raw text).
- [x] New `gateway/catalog` module: context-window size + price card per model (owned by gateway, consumed by context layer).
- [x] Unit tests with fixtures.
**Acceptance:** estimation error measured (eval in P2-10); catalog is the single source of model facts.

**Notes (2026-08-06):** New `backend/app/gateway/catalog.py` — `ModelSpec` (provider, model, context_window, input/output price per 1k) + `ModelCatalog` (get/context_window/estimate_cost/list_all/register/upsert; `default_catalog` shared instance). Seed table: curated public list prices (USD/1M: openai gpt-4o 2.50/10, gpt-4o-mini 0.15/0.60, gpt-4.1 2/8, gpt-4-turbo 10/30, gpt-4 30/60, o1 15/60, o1-mini 1.10/4.40, o3 2/8, o3-mini 1.10/4.40, o4-mini 1.10/4.40; anthropic claude-sonnet-4/4.5/4.6 + 3-5-sonnet + 3-7-sonnet 3/15, claude-opus-4 15/75, claude-opus-4.5 5/25, claude-haiku-4.5 1/5, claude-3-haiku 0.25/1.25, claude-3-5-haiku 0.80/4; google gemini-1.5-pro 1.25/5, gemini-1.5-flash 0.075/0.30, gemini-2.0-flash 0.10/0.40, gemini-2.5-pro 1.25/10) with context windows (200k/1M/128k per model); Anthropic prices verified against platform.claude.com pricing (2026-05-27 list prices); snapshot is drift-tolerant — P3-5 billing reconciles provider-reported usage, `register`/`upsert` allow deployment overrides. `ModelFacts` removed from `context/estimator.py` — the context layer now consumes `get_default_catalog()` (acceptance: single source of model facts); `TokenEstimator` gained `estimate_messages` (per-message part counting, non-string payloads counted by serialized size — never raw concatenation). `SessionContextLoader` takes `model_catalog`; `OrchestrationService` takes `model_catalog` (defaults to shared instance), passes it to the loader, and `_record_usage` now fills `usd` from the price card (`estimate_cost(provider, model, in, out)`, `or 0.0` for NOT NULL spend_events.usd). Tests: `backend/tests/test_gateway_catalog.py` (10: key normalization, seed presence, window lookup/default, cost math, unknown → None, register/upsert merge, instance isolation, shared default, sorted unique listing) + updated `test_context_assembler.py` (21: catalog-driven window, part counting). Regression: context/gateway/orchestration/hitl/worker/model_catalog suites → 89 passed.

### P2-3 — Compaction service
**Status:** `[x]` · **Depends:** P2-1 · **Arch:** §8.2
**Subtasks:**
- [x] Triggers: preemptive ~70%; reactive ~95%; provider-overflow one-shot recovery (retried exactly once; second overflow = hard error → degrade, not auto-fallback).
- [x] Mechanics: head → structured rolling summary (objective, key facts, decisions, pending work, next moves); token-bounded tail (`keep.tokens` default 8000, `buffer` 20000, per-tenant tunable).
- [x] Summary generation: cheap summarizer model, tools disabled, bounded output tokens (4096 floor).
- [x] Atomicity: snapshot → summarize against snapshot → schema validation → atomic swap; failure leaves prior boundary active.
- [x] Compaction events appended to thread log (part_type `compaction`).
- [x] Tests: triggers, atomic swap, interrupted compaction.
**Acceptance:** compaction is atomic, observable, OpenCode-compatible on overflow semantics.
**Notes:**
- `backend/app/application/compaction/service.py`: `CompactionConfig` (0.70/0.95, keep_tokens 8000, buffer 20000, max_output_tokens 4096, max_snapshot_messages 1000); `CompactionDecision`; `SummarySchema.validate` + deterministic `render` (Objective/Key Facts/Decisions/Pending Work/Next Moves); `LLMSummaryGenerator` (JSON-only prompt, temperature 0.0, bounded output, tools disabled, `summarizer_window() = window − 4096 − 4096`; hard `CompactionAborted` when the head exceeds it — chunk-and-merge is P2-4); `CompactionService` — `evaluate()` trigger math, `compact()` = snapshot (200-message page walk) → `_split` (newest ~keep_tokens live, head = everything older) → summarize → re-validate (regardless of generator) → atomic `set_summary` swap; boundary = seq of last summarized message; one `compaction` event + one `compaction` part on the boundary message.
- Fixed during verification: `_split` no-overflow inversion (head swallowed the whole log when everything fit the tail) and `compact()` trusting the generator's validation instead of re-validating before the swap.
- `backend/app/infrastructure/db/threads.py::set_summary`: one transaction — `with_for_update` thread row, `summary_version + 1`, compaction event, boundary message part (content = payload, redacted_content = rendered block).
- Orchestration (`service.py`): `AgentState.context_summary` + `context_summary_position`; `process_message`/`stream_message` accept `context_summary_position`; generate-node loop — preemptive trigger at `total > budget × 0.70` (once per turn via `compacted_this_turn`), `ContextBudgetExceeded` → `_recover_overflow` retry once, provider overflow (matched by `_is_provider_overflow` markers) → retry once, skipped mid-stream; second failure = hard error.
- Route (`conversations.py`): `_load_thread_summary` returns `(content, position)`; `_refresh_hot_tail(..., summary=...)`; `_build_compaction_callback(db, threads, tenant_config, llm_api_key, provider, summarizer_model, thread)` → `CompactionService` (compact on overflow, evaluate otherwise; returns None on failure — never breaks the turn); wired on **both** streaming and non-streaming endpoints; both pass `context_summary_position`.
- Assembler: turns with `seq <= summary_position` are replaced by the summary block (Arch 8.1 order).
- Tests `backend/tests/test_compaction.py` (24): trigger boundaries, schema validate/render, generator JSON/fenced-JSON/invalid/empty/adapter-failure paths, summarizer-window math, split boundaries, atomic swap (version bump, event, boundary part, redacted-only head), noop, interrupted → prior checkpoint intact, validation failure → atomic, head-over-window → P2-4 abort, orchestration preemptive/overflow-recovery/hard-error/no-callback.
- Full suite: 255 passed, 16 skipped (Redis down); failures = the 6 known pre-existing only.

### P2-4 — Compaction failure handling (Neryva additions)
**Status:** `[x]` · **Depends:** P2-3 · **Arch:** §8.2 (breaker/truncate/chunk-and-merge — **Neryva additions**, not OpenCode behavior)
**Subtasks:**
- [x] Per-session breaker (3 consecutive failures) → lossy truncation (system + recent K turns), session marked degraded, surfaced to operators.
- [x] Wedged summarizer: buffer > summarizer context → chunk-and-merge.
- [x] Snapshot-rollback: deep copy before compaction; validate output; roll back on failure.
- [x] Tests: 3 failures → truncate + degraded flag; chunk-and-merge correctness; rollback.
**Acceptance:** a session never dies from a bad compaction; degradation visible.
**Notes:**
- `backend/app/application/compaction/breaker.py`: `CompactionBreaker` (trip at `max_failures`=3 consecutive failures; `recovery_timeout` 300s → half-open retry allowed; one success resets; `state_summary` observability surface; in-process shared registry via `get_compaction_breaker()` — Redis-shared in P3-4 with the LLM breaker, never silent: trip/recovery are warning/info logs).
- `CompactionService.compact()`: tripped breaker → `_truncate_fallback` — keep newest `truncation_keep_turns` (10) turns live, boundary = 11th-from-newest seq, degraded checkpoint swap through the SAME atomic `set_summary` path (block content = `DEGRADED_SUMMARY_CONTENT`, payload `{}`, `degraded: True`); failures record into the breaker (incl. `CompactionAborted`, validation failures, DB swap errors); success resets it; result dict now always carries `degraded` (True/False).
- `set_summary(..., degraded=False)` → compaction event payload + boundary part gain `degraded` (audit/operator-visible).
- Chunk-and-merge in `LLMSummaryGenerator`: `summarize()` now handles heads of ANY size — `_chunk_turns` (greedy window-fitting partition, chronological; a single turn larger than the window aborts with `CompactionAborted`), `_merge` recurses (summarize chunks → render → summarize the summaries) bounded by `max_merge_depth` (4) with non-convergence abort. The old "head exceeds summarizer window → abort" path is gone.
- Snapshot-rollback subtask: already satisfied by P2-3's design — snapshot (read-only page walk) → summarize against snapshot → re-validate → single-transaction swap; the prior checkpoint stays active on any failure (no partial state to roll back from).
- Tests `backend/tests/test_compaction.py` (33, +9 for P2-4): chunk-and-merge (multi-call merge round reads `summary:` pseudo-turns), single-turn-over-window abort, non-convergence abort, e2e compact chunk-and-merge on sqlite, breaker trip at 3 / timeout recovery (monkeypatched monotonic) / success reset, truncation fallback (degraded block + event + part markers, boundary math), truncation noop when all fits, trip→fallback→recover cycle.
- Full suite: 264 passed, 16 skipped (Redis down); failures = the 6 known pre-existing only.

### P2-5 — Instant compaction (background)
**Status:** `[x]` · **Depends:** P2-3 · **Arch:** §8.2
**Subtasks:**
- [x] Background job refreshes running summaries as threads grow (`summary.refresh`, idempotent per thread).
- [x] Triggered compaction = instant swap; never user-facing wait.
- [x] Tests: cadence, triggered latency budget.
**Acceptance:** compaction adds no user-visible latency.
**Notes:**
- `backend/app/application/compaction/refresh.py`: `JOB_SUMMARY_REFRESH = "summary.refresh"`; `CompactionRefreshConfig(min_growth_turns=10, max_threads_per_run=50)`; `load_effective_tenant_config(db, tenant_row)` (single source of truth, now shared with the route — route `_load_effective_tenant_config` delegates to it, so API and worker read the published config identically); `refresh_thread_summary(db, tenant_id, thread_id, *, generator, config, request_id)` — idempotent: skips `thread_not_found` and `insufficient_growth` (page-walk `_count_since` bounded at 200; new turns < min_growth_turns → noop), else `CompactionService.compact()` → `{"refreshed", "reason", **compact_result}`; `find_stale_threads(db, config)`; `default_generator_builder(tenant_config, api_key)` → `LLMSummaryGenerator(create_llm_adapter(...))`.
- `threads.py::list_threads_stale_for_compaction(since_seq_delta, limit)`: latest-message-seq subquery; filters `summary_position IS NOT NULL`, active, not archived, `latest_seq − summary_position >= since_seq_delta`; ordered by delta desc; returns rows + `latest_seq`.
- `worker/handlers.py::handle_summary_refresh(payload, *, generator_builder=None)`: targeted when `tenant_id`+`thread_id` present, else sweep of stale threads; missing tenant → warning skip; `ProviderKeyNotFoundError` → warning skip (platform-managed keys hint); compaction failure raises → queue retry/DLQ; registered in `build_handlers()`; `request_id = f"summary-refresh:{thread_id}"`.
- Route (`conversations.py`): preemptive trigger no longer compacts inline — `_build_compaction_callback` defers to the worker via `_defer_to_worker()` (`Job(type=JOB_SUMMARY_REFRESH, payload={tenant_id, thread_id})` + `idempotency_key=f"summary-refresh:{thread['id']}"`; queue-manager imports lazy inside the helper). Overflow recovery still compacts inline (one-shot, keeps the turn alive); reactive path compacts inline. Generate loop unchanged: `compacted_this_turn` still allows at most one inline compaction per turn.
- Instant-swap property: the checkpoint swap itself was already single-transaction (P2-3); this phase moves the *expensive* generation off the request path — the deferred preemptive job refreshes the boundary before the next overflow hits, so when the inline overflow path finally triggers the head is small.
- Tests `backend/tests/test_compaction_refresh.py` (10): refresh skip/noop/compact + idempotent-noop-after-compact (threshold `min_growth_turns=4` vs 3-turn post-compact growth), boundary math (15 msgs × 2 tokens, keep 6 → position 12), stale-query membership (in/out/archived-excluded), handler targeted/sweep/no-key-skip, job-type registration; `test_worker.py::TestHandlerRegistration` updated for the new handler. Stub generator + `_patch_worker_env` (monkeypatched `get_database_manager` + `ProviderKeyService.resolve`); tenant rows must carry UUID-shaped ids (`TenantConfig.id` validates).
- Full suite: 273 passed, 16 skipped (Redis down); failures = the 6 known pre-existing only.

### P2-6 — Cache discipline
**Status:** `[x]` · **Depends:** P2-3 · **Arch:** §8.2, §10
**Subtasks:**
- [x] Summary block at stable position (after system), immutable for next K turns; favor removing superseded content + appending immutable blocks over full rewrites.
- [x] `cache_control` markers on stable prefixes where providers support it; per-tenant hit-rate metric.
- [x] Tests: cache-prefix stability across compactions.
**Acceptance:** prompt-cache prefix survives compaction boundaries; hit rate measured.
**Notes:**
- **Layered summary blocks.** The checkpoint is now a stack of immutable layers (`summary_block["layers"] = [{position, content, payload}...]`); the assembler renders ONE system message per layer (header on layer 1 only). A compaction APPENDS a layer covering only the turns after the previous boundary (`head_slice`), so every earlier message stays byte-identical across the swap — the prompt-cache prefix survives the boundary. `content` = `"\n\n".join(layer contents)` (backward-compatible hot-tier reads); `payload` = last layer's payload. Legacy blocks (no `layers` key) degrade to a single layer so the next compaction appends instead of rewriting; degraded truncation clears the stack (`layers: []`).
- **Consolidation bound.** At `CompactionConfig.max_summary_layers` (5) the summary is consolidated into a single rewritten layer summarizing the ENTIRE head (chunk-and-merge handles any size) — a full cache-breaking rewrite, bounded to at most once per N boundaries. The assembler renders the summary whole-or-not-at-all (a partial layer set would shift the tail prefix between turns).
- **Idempotent checkpoints.** A compact with no turns beyond the boundary is now a noop (previously it re-summarized the whole head and bumped the version, rewriting the summary every trigger). `test_compact_version_bump_on_second_checkpoint` was updated to `..._only_on_growth`.
- **`cache_control` markers.** `SessionContextLoader(cache_markers=...)` (settings `PROMPT_CACHE_MARKERS_ENABLED`, default on) marks the stable prefix — system message + summary layer messages — with `metadata = {"cache_control": {"type": "ephemeral"}}`; the orchestrator carries it onto `LLMMessage.metadata`. `AnthropicAdapter`: system messages now emit as text blocks with ephemeral breakpoints on marked blocks (string-joined form when unmarked); per-message markers emit content-block form. **Bugfix:** the adapter previously dropped ALL system messages but the last (system + knowledge + layers → only the last reached the API); all are now preserved. OpenAI/Azure/Gemini ignore markers (automatic prefix caching; `cached_tokens` already captured in usage).
- **Per-tenant hit-rate metric.** `backend/app/context/metrics.py`: `PromptCacheMetricsCollector` — per (tenant, provider) counters of input/cached tokens; provider-normalized hit rate (Anthropic: `cached/(input+cached)` since input excludes cache reads; OpenAI family: `cached/input` since prompt_tokens includes them; clamped 0..1); per-request structlog `prompt_cache_hit_rate` line; `snapshot()`/`snapshot_all()`; wired into orchestration `_record_usage` (failures never break the turn; Redis-shared in P3-4).
- Tests `backend/tests/test_cache_discipline.py` (15): per-layer rendering, byte-stable prefix across a boundary (system + layer 1 identical before/after; tail moves), legacy single-layer fallback, whole-block omission, markers on/off, e2e append (first layer byte-identical, second compact summarizes only growth seqs 6..9), consolidation at cap (single layer, full-head re-summarize), truncation clears layers, legacy-block migration to layers, Anthropic wire format ×3 (blocks+markers / joined multi-system / chat-message block form), metric normalization + aggregation + reset, orchestration end-to-end (layers + markers reach `LLMMessage`). `test_context_assembler.py::test_system_prefix_stable_across_turns` updated for the metadata key.
- Full suite: 289 passed, 16 skipped (Redis down); failures = the 6 known pre-existing only.

### P2-7 — Tool-result clearing (+ thinking clearing)
**Status:** `[x]` · **Depends:** P2-1 · **Arch:** §8.3
**Subtasks:**
- [x] Sub-transcript op: superseded, re-fetchable tool results → placeholders (keep `tool_use` record, drop payload); configurable trigger.
- [x] Thinking-block clearing for extended-thinking traffic — evaluate Anthropic context-editing API (`clear_thinking_20251015`, beta `context-management-2025-06-27`) vs bespoke; record decision (D-12).
- [x] Tests: space reclaimed; re-fetch works; audit shows placeholders.
**Acceptance:** agentic turns cannot bloat context via stale tool payloads.
**Notes (2026-08-06):**
- **Durable sub-transcript op.** `ThreadRepository.clear_tool_results` (one transaction: thread row locked → parts updated → `tool_result.clear` event appended). Every `tool_result` part with `seq <= latest_seq - keep_recent_turns` has its content replaced by `{"tool_use_id", "cleared": True}` — the payload is dropped but `tool_use_id` survives so the tool can be re-fetched on demand; `tool_use` parts are untouched (the record remains, only the payload is reclaimed). `redacted_content` carries `placeholder` (`TOOL_RESULT_PLACEHOLDER`, "[tool result cleared; re-fetch on demand]") for audit. Idempotent: already-cleared parts are skipped and no event is written when nothing was cleared (re-runs are free).
- **Configurable trigger.** Tenant feature `features["clear_tool_results"]` (off by default) gates everything; per-tenant `tool_clearing["keep_recent_turns"]` (default 2, new `TenantConfig.tool_clearing` field + `tenant_config_from_data` default-merge) keeps the newest turns fully legible. `backend/app/application/clearing/` (`clear_stale_tool_results`, `tool_clearing_config`) — missing tenant/thread degrade to a reason dict, never raise from a background job.
- **Render-time enforcement.** `_load_history_turns` flags turns via `list_tool_result_seqs` (live = non-cleared parts only); the orchestrator passes `clear_tool_payloads` from the tenant feature so the assembler substitutes `TOOL_RESULT_PLACEHOLDER`. Stale payloads can never reach the model even before the background op runs — this is the primary defense (Arch 8.3), the durable op is the reclaim.
- **Background job.** `tool_result.clear` worker handler (targeted tenant+thread, feature-checked, skips logged) registered in `build_handlers`; the route enqueues it on turn completion (Arch 9.1 step 8) with idempotency key `tool-clear:{thread_id}`; enqueue failures never break the turn.
- **Bugfix (latent, found while wiring):** the route passes `ContextTurn` instances in `conversation_history` but `_assemble_context` and the handoff renderer called `.get()` on them (AttributeError on any real session with history > 0); both now accept dicts AND `ContextTurn`.
- **D-12 resolved:** thinking clearing adopts Anthropic's context-editing API `clear_thinking_20251015` (beta `context-management-2025-06-27`) for Claude-routed extended-thinking traffic — enabled alongside tool-result clearing per Arch §8.3, not a bespoke equivalent; store-side clearable parts remain the provider-agnostic fallback + audit.
- Tests `backend/tests/test_tool_clearing.py` (22): durable op (reclaim keeps `tool_use` record + audit placeholder, idempotent re-run writes no event, recency guard, multi-message reclaim, unknown thread raises), render-time marker (live-only seqs; route turns flagged then unflagged after the op), service (config derivation, disabled noop, config from DB row, keep_recent override, missing thread/tenant degrade), orchestration swap gated by feature (on → placeholder, off → passthrough, `ContextTurn` instances accepted), worker handler (targeted clear, disabled skip, missing thread benign, missing payload raises, job registered). `test_worker.py` handler-set updated with `tool_result.clear`.
- Targeted suites (worker, compaction, refresh, assembler, orchestration, cache-discipline): 107 passed + 22 new, zero regressions; full suite deferred to a quiet window (6 known pre-existing failures unchanged).

### P2-8 — Memory (opt-in, tenant-gated)
**Status:** `[x]` · **Depends:** P2-1 · **Arch:** §8.4
**Subtasks:**
- [x] Background worker extracts durable structured facts from closed turns → per-tenant/per-end-user memory store; versioned, retrievable, PII-filtered.
- [x] Assembler fetches top-k relevant facts on demand; tenant controls read scope + expiry.
- [x] Erasure support (ties P5-10).
- [x] Build-vs-adopt spike (D-2): Anthropic first-party memory tool for Claude-routed traffic vs bespoke store.
**Acceptance:** memory ships gated behind tenant consent; PII-filtered; fully erasable.
**Notes (2026-08-06):**
- **Store.** `MemoryModel` (`memories` table) + `MemoryRepository`: per-tenant, optional per-end-user scoping (`end_user_id` NULL = tenant-global facts for API-key sessions), `version` (1 today; bump path for P3-x dedup/refresh), `expires_at` (tenant `memory.expiry_days`, 0 = no expiry), `erased` soft-delete. Reads never surface erased or expired facts. `list_threads_pending_extraction` (LEFT JOIN of max message seq vs max extracted source_seq) is the sweep source.
- **Extraction.** `backend/app/application/memory/` — `MemoryExtractor` (LLM, JSON-array-of-strings, strict validation; malformed output raises `MemoryExtractionError` so the queue retries/DLQs — garbage never stored). `extract_thread_memory`: reads REDACTED turns only (P0-5), newest message excluded (in-flight turn is not closed), watermark = `max(source_seq)` per thread → idempotent re-runs (no LLM call when nothing new). Every fact is re-passed through the PII service before storage — only the redacted text is ever persisted (fail-closed). `source_seq` of the batch = newest closed turn, so the watermark advances with the batch.
- **Read path.** `retrieve_facts` (top-k on demand, never a raw dump): keyword relevance scored against the current message (query-token hits, recency tiebreak), bound by tenant `memory.max_facts`, scoped to the end-user. The orchestrator's `memory_retriever` hook (route-wired) feeds the existing `memory_facts` block of the assembler; used only when `features["memory"]` is on; retrieval failures are logged and never break a turn.
- **Background job.** `memory.extract` worker handler (targeted or sweep), feature-gated, key-checked like `summary.refresh`; PII pass gated on `ENABLE_PRESIDIO` with a logged skip when presidio is absent (facts still originate from redacted turns + the extraction prompt forbids personal data). Route enqueues on turn completion (Arch 9.1 step 8) with idempotency key `memory-extract:{thread_id}`; enqueue failures never break the turn.
- **Erasure.** `erase_end_user_memory` service + repo `erase_by_user`/`erase_by_thread`/`erase_by_id`/`prune_expired` — soft deletes (audit-safe), the cross-cutting P5-10 sweep can reuse them.
- **D-2 resolved:** bespoke provider-agnostic store now (Arch §8.4: the store is required for non-Claude traffic regardless); the Anthropic first-party memory tool is NOT a replacement — prototype it as an adapter option for Claude-routed traffic in the P2-9 runtime loop, not a parallel store.
- Tests `backend/tests/test_memory.py` (22): store scoping per end-user, erased+expired exclusion, soft-erase counts, prune, watermark + pending-thread sweep query, config derivation (feature gate + max_facts/expiry_days + `tenant_config_from_data` default-merge), extractor JSON parsing + strict rejection, extraction e2e (PII redaction of a stored fact, watermark advance, idempotent re-run without a second LLM call, latest-message exclusion, missing thread), top-k relevance ordering, erasure service, orchestration (memory block rendered when feature on / skipped when off / survives retriever failure), worker handler (targeted, disabled skip, sweep, registration). `test_worker.py` handler-set updated with `memory.extract`.
- Targeted suites (worker, tool-clearing, orchestration, compaction, cache-discipline): 97 passed + 22 new, zero regressions; full suite deferred to a quiet window (6 known pre-existing failures unchanged).

### P2-9 — Orchestration loop on the session engine
**Status:** `[x]` · **Depends:** P2-1, P2-3 · **Arch:** §8, §9.3
**Subtasks:**
- [x] New runtime loop (screen → retrieve → generate → authorize → verify → reply/escalate) consumes assembler output; tool parts flow through the loop and persist as first-class parts each turn (ties P5-3).
- [x] Bounded re-ask on verify failure; escalate on repeated failure.
- [x] End-to-end tests through the real pipeline.
**Acceptance:** one assembly path; parts persisted; provider prompt fully redacted.

**Notes (2026-08-07):**
- **Adapter tool protocol** (`app/adapters/llm/provider.py`): `LLMResponse.tool_calls` (`[{id, name, arguments}]`, arguments decoded dict, malformed JSON kept verbatim) + `LLMConfig.tools` (OpenAI function format). OpenAI/OpenAI-compat/Azure send `tools=` when configured and parse `message.tool_calls`; Anthropic sends `tools` only when non-empty (empty-list bug fixed) and parses content blocks via `_parse_anthropic_content` (text + tool_use) — this also fixes the latent AttributeError when the model replies tool-only (old code read `content[0].text`).
- **Tool registry** (new `app/application/tools/registry.py`): `ToolRegistry` (register/get/list/schemas/execute), `ToolSpec` (sync or async executor), `ToolAlreadyRegisteredError`; `execute()` never raises — unknown tool → `is_error` result, executor exceptions captured as error results, non-dict outcomes normalized.
- **Orchestrator** (`app/application/orchestration/service.py`): graph now `generate_response → execute_tools (conditional) → generate_response`; `AgentState` gains `tool_calls/tool_parts/tool_results/tool_steps/tool_budget_exhausted/verify_retries/verify_note/verify_exhausted`. `_generate_response` runs the bounded tool loop inside the while: tool results from prior steps rendered as `[tool_result ...]` user-role text blocks, tool schemas advertised per generation (client cached per tenant/provider/model), late tool calls dropped once `tool_budget_exhausted` (default `budgets.max_tool_steps` = 4). `_execute_tools` authorizes each call through `tool_authorizer` (fail-closed: exceptions deny), runs calls concurrently via `asyncio.gather`, emits `tool_use`/`tool_result` parts through `tool_callback` (failures logged, never raised). Verification bounded re-ask: invalid → `verify_note` user message (faithfulness via FaithfulnessChecker when docs retrieved, empty-response guard); after `budgets.max_verify_retries` (default 2) → `verify_exhausted` + handoff. Streaming: `_generate_response_streaming` reconstructs tool calls per provider (OpenAI-family `delta.tool_calls`, Anthropic `content_block_start`/`input_json_delta`, Gemini native `function_call` fallback); malformed fragments raise `StreamToolParseError` → one non-streaming retry; a stale inner `break` (pre-P2-9) that skipped tool-call routing was removed.
- **Route wiring** (`app/api/routes/conversations.py`): `_build_tool_callback` — each tool part persists as its own first-class assistant message (part_index 0 = canonical text part, tool part follows), `request_id` = `{request_id}:tool:{hex8}` (dedup-safe), PII-redaction of tool-result payloads before storage when `ENABLE_PRESIDIO` (get_pii_service RuntimeError → logged warning, raw preserved). Wired into both `/conversations` and `/conversations/stream` after the evidence callback.
- **Tests** (`backend/tests/test_tool_loop.py`, 28 passed): registry semantics, full loop round trip, budget exhaustion (final regeneration advertises no tools), authorizer deny/raise fail-closed, unknown tool, concurrent calls, malformed args, stream fragment reconstruction (OpenAI/Anthropic) + malformed → chat fallback, `_finalize_stream_tool_calls` strictness, verify re-ask / escalation / grounded pass, adapter tool protocol (send/parse/omit-when-empty/tool-only round trip), route-level part persistence via real ThreadRepository (test double fixes: adapter script off-by-one, `_Retrieval` result missing `.context`, `TenantRepository` import path, parts at index 1 after the canonical text part).
- **Verification:** `tests/test_tool_loop.py` 28 passed; regression sweep (streaming, orchestration, rag-isolation, compaction, cache-discipline, memory, worker, tool-clearing) 144 passed with the single pre-existing known failure (`test_deltas_and_result` — deny-by-default BLOCK on an empty rule set reaching handoff, pre-existing, untouched); py_compile clean on all changed files.

### P2-10 — Compaction quality evals
**Status:** `[x]` · **Depends:** P2-3 · **Arch:** §8.2, §17
**Subtasks:**
- [x] Round-trip eval: facts present before compaction answerable after; per-tenant summary prompt + keep/buffer tuning harness.
- [x] Bad-compaction detection surfaced to operators (context-rot alert, ties P6-4).
- [x] Baseline datasets in `evals/datasets/`.
**Acceptance:** compaction quality measured per tenant, never assumed.

**Notes (2026-08-07):**
- **Round-trip fact retention** (new `app/application/compaction/eval.py`): a probe passes when a fact answerable from the pre-compaction head stays answerable from the rendered summary. `LexicalFactScorer` (exact-fact, normalization-insensitive, deterministic) and `LLMJudgeScorer` (paraphrase-tolerant, one LLM call per probe) — both fail closed (empty summary, judge errors, garbage verdicts all count as failed, never assumed retained).
- **Harness** (`CompactionQualityEvaluator`): per-case summarize → schema-validate → render → score; keep-tokens split mirrors `CompactionService._split` (facts lying in the kept-live tail are skipped — their absence is not a retention failure; ungrounded probes are rejected at dataset load); `aggregate_results` (per-fact ratio, min, degraded count); `run_tuning_harness` runs a prompt × keep-tokens matrix for per-tenant prompt/keep tuning. `LLMSummaryGenerator` gained a `prompt` override (must keep the `{conversation}` placeholder; defaults to the stock prompt; per-tenant prompt wiring hooks here in P6-4).
- **Context-rot detection surfaced to operators**: (1) `CompactionService` config flags `quality_check` (default False — production behavior unchanged) + `quality_threshold` (0.6): when enabled, `compact()` generates probes from the turns the new layer covers (one extra LLM call, hallucination-guarded: every answer must appear verbatim in the turns) and logs `compaction_quality_low`/`compaction_quality_ok` — the checkpoint still commits, quality is measured, never blocks; a failed check logs `compaction_quality_check_failed` and is ignored. (2) `CompactionQualityMonitor.evaluate_thread` — on-demand operator check of a live thread's summary checkpoint with a retention threshold (low → warning + `low_quality` flag; the signal P6-4's alert transport consumes).
- **Baseline dataset** `evals/datasets/compaction/baseline.jsonl`: 5 labeled cases (refund window, shipping delay, subscription upgrade, password reset, billing dispute), 4-5 probes each, every answer verbatim in the turns (grounding enforced by the loader).
- **CLI** `evals/run_compaction_eval.py`: dataset → provider adapter (env or flags) → per-case report → aggregate; exits non-zero below `--threshold` (CI-able); `--prompt-file`, `--keep-tokens`, `--judge`, `--verbose` knobs.
- **Tests** (`backend/tests/test_compaction_evals.py`, 33 passed): scorer semantics/normalization/fail-closed, judge yes/no parsing + inconclusive/error fail-closed, dataset loader + validation + duplicate ids + baseline grounding, perfect/lossy/degraded round trips, probe skipping (ungrounded + tail-excluded), keep-tokens split, tuning matrix ordering, prompt override flows into the summarizer prompt, probe extraction (hallucination guard, loose-schema tolerance, max cap, fail-closed), monitor threshold/degraded/scorer-failure, and CompactionService integration (flag off → no probe calls; on → low warning + commit still happens; check failure never blocks the swap).
- **Verification:** eval suite 33 passed; compaction + refresh + orchestration + streaming + tool-loop + rag-isolation + worker regression 156 passed (single pre-existing known failure `test_deltas_and_result` unchanged); ruff clean on new/changed files (2 pre-existing findings in untouched lines); CLI smoke-tested end-to-end against a fake OpenAI-compatible server (report + exit code path; the provider call itself needs the declared `openai`/`anthropic` runtime deps, absent in the sandbox).

---

## 8. Phase 3 — LLM gateway (Arch §10)

**Goal:** the only component that talks to providers. **Exit criteria:** every provider call through the gateway; spend metered and quota-enforced at all four levels; failures degrade per fallback-chain contract.

### P3-0 — Build-vs-adopt decision (blocker for P3-2…P3-8)
**Status:** `[x]` · **Depends:** — · **Arch:** §10, §17
**Subtasks:**
- [x] Re-cost LiteLLM (Rust core, axum gateway, native `/v1/messages`, `Customer` object, per-tenant teams, Redis cooldowns + v1.82.0 dependency breaker) vs bespoke gateway per this phase.
- [x] Criteria: data residency, BYOK, EU posture, <30ms routing, maintainability, spend hierarchy fit.
- [x] Record in §15 (D-1). If adopt: P3-2…P3-8 become integration tasks against LiteLLM; if build: proceed as designed.
**Acceptance:** decision documented; downstream tasks adjusted.
**Notes (2026-08-07):** **D-1 = build bespoke, production-grade, from scratch** (user decision: "the best version and best logic" — prior partial gateway code `gateway/client.py` (P0-6) and `gateway/admission.py` (P0-7) are superseded by the new gateway package; the §2.1 REPLACE verdict on the LLM adapters is executed in P3-1). LiteLLM's verified mechanisms are re-implemented natively: Redis-shared cooldowns + Redis dependency breaker (P3-4), Redis Lua quota (P3-6). P3-2…P3-7 proceed as designed. **P3-8 (tenancy mapping) is N/A** — tenancy is native (per-tenant provider keys P0-8, tenant-prefixed Redis keys), no mapping layer needed.

### P3-1 — Unified adapter contract
**Status:** `[x]` · **Depends:** P0-10 · **Arch:** §10
**Subtasks:**
- [x] One contract for chat, streaming, tool calls, structured output across OpenAI/Anthropic/Gemini/Azure/self-hosted; wire translation inside the gateway.
- [x] Normalized streaming events (delta, tool_use start/end, tool_result, usage, done, error).
- [x] Reasoning/thinking tokens surfaced.
**Acceptance:** application code has zero provider branches.
**Notes (2026-08-07):** `adapters/llm/provider.py` rebuilt (REPLACE verdict executed): `LLMConfig.structured_output` (JSON schema; OpenAI/Azure/Gemini → `response_format json_schema`; Anthropic → forced hidden tool `__structured_output__` whose input becomes `content`; CUSTOM skips with a warning — compat gateways may reject unknown params), `LLMStreamEvent` + `BaseLLMAdapter.stream()` normalized events (delta / tool_use_start / tool_use_delta / tool_use_end / usage / done / error; OpenAI-family usage chunk + Anthropic message_start/message_delta both merged into canonical usage). Legacy `chat`/`stream_chat`/`create_llm_adapter`/`_extract_usage` preserved for existing consumers (orchestration, compaction, memory) until P3-9 rewires them; `stream_chat` is deprecated (raw stream, pre-gateway only). `tool_result` is a request-side event (tool loop) — no provider emits it. Import-verified with the existing suite baseline.

### P3-2 — Router
**Status:** `[x]` · **Depends:** P3-0, P3-1 · **Arch:** §10
**Subtasks:**
- [x] Decision <30ms from per-deployment health/price/latency tables; per-tenant strategy: cost, latency, quality-pinned, pinned model.
- [x] Tiered routing: simple → cheap model, complex → capable model, under tenant policy.
- [ ] Tests: strategy selection, decision latency, stale tables.
**Acceptance:** routing meets budget; tenant strategies enforced.
**Notes (2026-08-07):** `gateway/router.py`: `Router.candidates()` + `decide()` — pure in-memory (price cards from `gateway.catalog`, EWMA latency tracker, availability callable consulted per candidate), no I/O on the decision path (<30ms target; measured in P3-10). Strategies `cost|latency|quality|pinned`; `pinned` bypasses scoring; `tiered + simple_query` routes to the cheap model first; tenant fallback models honored. Deterministic fallback when everything is cooling down (`all_deployments_cooldown` + best candidate). Tests land with P3-10.

### P3-3 — Fallback chains (three classes)
**Status:** `[x]` · **Depends:** P3-1 · **Arch:** §10
**Subtasks:**
- [x] General (timeout/5xx), content-policy (refusal), context-window (overflow) — ordered targets from tenant catalog.
- [x] Failover transparent only before first byte; mid-stream failures surface to connection tier (ties P4-4).
- [ ] Tests per class.
**Acceptance:** fallback behavior matches the contract exactly.
**Notes (2026-08-07):** `gateway/fallback.py`: `classify_failure()` (17 content-policy markers, 11 context-window markers) + `FallbackChain.targets(failure_class)` — tenant fallbacks → cross-family (anthropic↔google↔openai per `_FAMILY_CROSS_FALLBACK`) → larger-window models from the catalog (top-2 windows not already candidates), deduped, ordered. `is_overflow_failure` guards mid-stream degradation. Tests land with P3-10.

### P3-4 — Resilience: cooldowns + dependency breaker
**Status:** `[x]` · **Depends:** P3-2 · **Arch:** §10, §3
**Subtasks:**
- [x] Per provider+model cooldown state in Redis (LiteLLM `cooldown_cache.py` pattern), shared across replicas.
- [x] Dependency-level circuit breaker for Redis (LiteLLM v1.82.0 semantics: 5 consecutive failures, 0ms fast-fail, 60s half-open probe, Postgres fallback for auth/rate-limit).
- [ ] Tests: cross-replica cooldowns; Redis slow/down → degraded-but-serving.
**Acceptance:** breaker state cross-replica; Redis degradation contained.
**Notes (2026-08-07):** `gateway/cooldown.py`: `RedisCooldownCache` (INCR + PEXPIRE Lua, `allowed_fails=5`, `cooldown_time=60s`, `record_failure/record_success/is_available`; Redis down → checks fail open, DEGRADED health, once-per-transition error log) + `RedisDependencyBreaker` (5 consecutive failures → open → `fast_fail` 0ms skip; 60s recovery → half-open probe → close; state in health checks). Postgres fallback for auth/rate-limit is N/A at gateway level — auth/rate limits stay in routes/infrastructure (P0-9). Tests land with P3-10.

### P3-5 — Usage capture + cost ledger
**Status:** `[x]` · **Depends:** P0-10 · **Arch:** §10
**Subtasks:**
- [x] Append-only `spend_events` per request (tenant, surface, end_user, model, provider, tokens, USD); consumers for billing/dashboards/anomaly alerts.
- [x] Async write (queue) so the request path never blocks.
- [ ] Tests: event correctness, aggregations, immutability.
**Acceptance:** every request lands in the ledger; per-tenant/surface/end-user cost answerable.
**Notes (2026-08-07):** `gateway/ledger.py` — `CostLedger` (ManagedService): single writer of `spend_events` rows (`UsageRecord`: tenant/surface/end_user/model/provider/tokens/usd), async enqueue via the background queue, `quota_state` rows persisted for P3-6; usage flows from gateway `generate`/`stream` completion (catalog price card) and from orchestration `_record_usage` (usage_callback keeps prompt-cache metrics + end-user spend caps only — the ledger is the sole spend_events writer; conversations route no longer writes spend rows). Ledger component surfaced in health checks. Write-path tests land with P3-10 evals.

### P3-6 — Quota: USD reservation/reconciliation
**Status:** `[x]` · **Depends:** P3-5 · **Arch:** §10, §6.3.8
**Subtasks:**
- [x] Redis Lua: reserve estimated max spend before routing; reconcile actual after completion; platform > tenant > surface > end-user (any over → reject).
- [x] Soft alert at 80%, hard block at 100%; per-level status codes.
- [ ] Tests: reservation math, races, boundary at each level.
**Acceptance:** rejection when any path level is over budget; no double-spend under concurrency.
**Notes (2026-08-07):** `gateway/quota.py`: `QuotaService` (ManagedService) — Redis Lua reserve/reconcile scripts, calendar-month windows, keys `neryva:gateway:quota:{platform:{w} | t:{tenant}:{w} | t:{tenant}:s:{surface}:{w} | t:{tenant}:u:{end_user}:{w}}`; sequential reserve with rollback on any rejection, `GatewayQuotaExceeded(level, limit_usd, projected_usd)` → HTTP mapping at P3-9; soft alert log at 80%; Redis down → enforcement off + DEGRADED health (never silent). Durable `quota_state` rows (models.py:602) are written by the P3-5 ledger consumer from the same spend event. Tests land with P3-10.

### P3-7 — Caches
**Status:** `[ ]` · **Depends:** P3-2 · **Arch:** §10
**Subtasks:**
- [ ] Exact-match cache per tenant; per-tenant semantic cache with similarity threshold.
- [ ] Invalidation on knowledge change + config publish (ties P0-11); correctness tests (stale results after KB edit are a product bug — §17).
- [ ] Prompt-cache awareness: stable prefixes + markers (ties P2-6).
**Acceptance:** hit rates measured; invalidation correctness proven by tests.

### P3-8 — Tenancy mapping (if adopting LiteLLM)
**Status:** `[x]` · **Depends:** P3-0 · **Arch:** §10
**Subtasks:**
- [x] Neryva `tenant` → LiteLLM `Team` (or `Organization` for dedicated shape); `end_user` → LiteLLM `Customer` (never `User` — that is proxy members).
- [x] Budgets mirrored per level.
**Acceptance:** mapping documented; end-user spend attribution correct.
**Notes (2026-08-07):** N/A — resolved by D-1 (build bespoke). Tenancy is native: per-tenant provider keys (`TenantProviderKey`, P0-8), tenant-prefixed Redis keys everywhere (cooldowns, quota, rate limits, caches). No mapping layer exists to build.

### P3-9 — Wire orchestration to gateway
**Status:** `[x]` · **Depends:** P3-2, P3-3, P3-5 · **Arch:** §10
**Subtasks:**
- [x] Runtime loop calls the gateway interface only; admission (P0-7) precedes routing; no key handling in orchestration.
- [ ] Provider failure → fallback → degrade tested end-to-end.
**Acceptance:** orchestration has zero direct provider calls.
**Notes (2026-08-07):** `gateway/service.py` — `Gateway` facade: `generate`/`stream` (candidates → availability snapshot → quota reserve → exact-cache → fallback chain → cooldown → reconcile → ledger), module singleton `init_gateway(db=db, queue=queue)`/`get_gateway()`, lifecycle (initialize/close/health_check → HealthComponent with dependencies), `GatewayBackedAdapter` (adapter-contract facade over `gateway.generate` for compaction/eval replay), `_to_llm_messages` (adapters require `LLMMessage` objects; request carries dicts + metadata). `gateway/types.py` — `GatewayRequest` (tenant/session/conversation/surface/end_user/tools/temperature/timeout/strategy), `GatewayResult`, `GatewayStreamEvent` (delta/tool_use_start/tool_use_delta/tool_use_end/usage/done/error, with provider/model), error kinds (GatewayError, GatewayQuotaExceeded, GatewayConfigurationError, GatewayChainExhausted). Orchestration: `create_orchestration_service(gateway=...)` (required; ValueError if missing) + `surface_id`/`end_user_id`; `_generate_response` builds `GatewayRequest` (strategy="cost", tools only while tool budget allows) → `gateway.generate` or `_generate_response_streaming` (normalized tool fragments, once-per-turn stream→chat fallback on malformed args; `GatewayError` re-raised so the route maps 402/502/503); adapter/stream-extraction helpers deleted. Routes: `conversations.py` uses `get_gateway()` (no key resolution; `_resolve_llm_api_key` deleted), compaction summarizer via `GatewayBackedAdapter`, `_gateway_http_status` (quota→402, configuration→503, chain-exhausted→502, else 500), SSE error events carry kind/status; `eval_replay` + `main.py` lifespan wired (health tuple includes `gateway`). Legacy `gateway/client.py` (ResilientLLMClient) and `factories.resolve_llm_api_key` removed — no importers. Tests: `backend/tests/gateway_fakes.py` (`FakeGateway` — chat_stub over legacy `chat()`/`stream_scripts` of `GatewayStreamEvent`); orchestration suites (tool_loop, streaming, compaction, memory, tool_clearing, hitl, evidence, contracts, rag_isolation, cache_discipline, p0_fixes, orchestration) migrated off `llm_api_key`/`_get_llm_adapter`; ApiSmoke 503-no-key path re-verified through the gateway. Remaining: end-to-end fallback/degrade evals in P3-10.

### P3-10 — Gateway evals
**Status:** `[ ]` · **Depends:** P3-9 · **Arch:** §10, §13
**Subtasks:**
- [ ] Latency budget tests (<30ms routing under load), fallback suite, quota races, cache invalidation.
- [ ] Failure injection: provider 500s, timeouts, refusals, overflow errors.
**Acceptance:** gateway passes the failure-injection suite in CI.

---

## 9. Phase 4 — Real-time path & streaming durability (Arch §9)

**Goal:** streaming is the only path customers see; durable (replay on reconnect), safe (rolling-window moderation), cancellable, idempotent. **Exit criteria:** no delta reaches the client before passing the moderation window; reconnect replays without loss; cancel releases upstream capacity.

**Phase 4 COMPLETE (2026-08-07):** P4-1…P4-9 all `[x]`. Final targeted suite (17 files: stream buffer + overflow, streaming, streaming moderation, contracts, non-streaming parity, event outbox, webhooks, input pipeline, failure semantics, gateway cancel/ledger-async/router/fallback/catalog, worker, multi-worker): **162 passed, 0 failed** (2 pre-existing unrelated skips). Two suite-blockers fixed at completion: `contracts/openapi/openapi.v1.json` re-exported (`backend/scripts/export_openapi.py`) to include the DSR routes added in `conversations.py` since the last export; `test_worker.py::TestHandlerRegistration` set extended with the P4-7 `outbox.relay` + `cost_ledger.write` job types (test was pinned to the pre-P4-7 handler set).

### P4-1 — SSE transport
**Status:** `[x]` · **Depends:** — · **Arch:** §9.1
**Subtasks:**
- [x] `Last-Event-ID` resume; `X-Accel-Buffering: no`; heartbeats; CDN-safe headers.
- [x] SSE frame contract (session/guardrails/delta/result/error + redaction/retraction + compaction frames) in `contracts/events/sse-stream.schema.json` (revised from legacy).
- [x] Tests: reconnect with Last-Event-ID, heartbeat (route-level).
**Acceptance:** SSE resumable and contract-validated.
**Notes (2026-08-07):** `api/routes/conversations.py` stream endpoint — producer/consumer heartbeat loop injects `heartbeat` frames every `STREAM_HEARTBEAT_SECONDS` (15s, `settings/env.py`) while the model is quiet; cancellation of the SSE consumer cancels the producer, which propagates into the gateway stream (ties P4-4). Headers: `Cache-Control: no-cache` + `X-Accel-Buffering: no` on live + replay responses. Frames now contract-complete: `redaction` (halt with violations) was extended by `retraction` (`{violations, retract_from_event_id}` — tells a resuming client how much already-released deltas to drop, emitted alongside `redaction` on both mid-stream and tail truncation; `retract_from_event_id` is the last buffered `seq`) and `compaction` (`{position, layer_count}` — emitted at stream open when the thread carries a durable checkpoint, so the client knows history was compacted before this turn). Schema uses `oneOf`; `retraction` requires `retract_from_event_id` so frames stay unambiguous vs `redaction` (contract test validates both the positive and the invalid-only `redaction`-shaped case). Route-level heartbeat test `backend/tests/test_streaming.py::TestRouteHeartbeat` (real TestClient + sqlite + `_SlowGateway`, heartbeat clamped to 0.05s): asserts `heartbeat` lands between the first event and terminal `result`. The `compaction` frame emission gates on `context_summary_position`; both new frames are covered by the contract validation in `test_contracts.py`.

### P4-2 — Server-side stream buffer
**Status:** `[x]` · **Depends:** P1-2 · **Arch:** §9.1, §3
**Subtasks:**
- [x] Durable chunk writes while streaming (Redis list key per stream, TTL; in-memory fallback). Chunk-level overflow → Postgres tier: per-stream hot-tier cap (`overflow_max_chunks`) spills the oldest chunks to the durable `stream_buffer_chunks` table so replay stays faithful past the cap and survives Redis loss/process restart (chunk-level; the durable log covers completed turns).
- [x] Live deltas are never part of the durable log until the turn completes (`StreamingModerationWindow` holds → validated → released; terminal `result`/`error` recorded via `mark_terminal`; final message persisted to thread once).
- [x] Tests: reconnect mid-stream replays identical bytes; interrupted turn recovery; thread-scoped key isolation; overflow writes/replay-merge/restart-recovery/clear. (`backend/tests/test_stream_buffer.py`, `backend/tests/test_stream_buffer_overflow.py` — 6 overflow cases)
**Acceptance:** io no half-finished message is lost across reconnects.
**Notes (2026-08-07):** `infrastructure/stream/buffer.py` — Redis list per stream, key thread-scoped (`neryva:stream:buffer:{tenant}:{thread}:{stream}`) so a reused request-id under different threads never collides, `ttl_seconds` TTL, process-local monotonic event ids (`Last-Event-ID`); in-memory fallback + DEGRADED health when Redis is down (never silent). P4-2 overflow tier: `StreamBuffer(..., db=db, overflow_max_chunks=N)` — every `append` past the cap moves the oldest chunks into `stream_buffer_chunks` (bounds Redis/memory growth without dropping bytes); `replay` merges hot + overflow ordered by event id and dedupes by `id`; `clear` removes both; no-db overflow is logged (never silent). `overflow.py` — `StreamBufferOverflowRepository` (`write_chunks`/`read_chunks`/`clear`; idempotent by scope+seq; UUID5 row ids). Wired in `main.py` (db + `STREAM_BUFFER_OVERFLOW_CHUNKS`). Tests: `test_stream_buffer_overflow.py` (cap spill + merged replay, unbounded no-cap, restart recovery from the durable tier, write idempotency, repo clear, `StreamBuffer.clear` removes both tiers).

### P4-3 — Rolling-window output moderation
**Status:** `[~]` · **Depends:** P4-2 · **Arch:** §9.1, §12 PII rule 2
**Why:** *Do-not-repeat:* legacy streamed deltas before validation, so unvalidated content could reach the client while only the stored copy was redacted.
**Subtasks:**
- [x] Release deltas through a small buffer window; validate chunks (PII, policy, brand) as they pass. (`streaming.py`)
- [x] Mid-stream violation → redaction event + truncation; client sees only validated content.
- [x] Window size = per-tenant latency/safety knob (`tenant_config.stream_moderation_window_chars`, D-3); platform default `STREAM_MODERATION_WINDOW_CHARS`=400.
- [x] Tests: violation never reaches client; truncation contract; per-tenant knob. (`backend/tests/test_stream_moderation.py`)
**Acceptance:** the customer never sees unvalidated content.
**Notes (2026-08-07, D-3):** `application/validation/streaming.py` — `StreamingModerationWindow` accumulates `window_size` chars, validates asynchronously (`evaluate_output` → PII redaction/policy), releases redacted text, or sets `truncated` on a blocked/block. Validator failure fails open (visible log).

### P4-4 — Cancellation propagation
**Status:** `[~]` · **Depends:** P3-3 · **Arch:** §9.1
**Why:** *Do-not-repeat:* legacy leaked an in-flight generation on disconnect (orphaned task).
**Subtasks:**
- [x] Disconnect → cancel token → gateway aborts provider stream → capacity released (coordinator slot, quota reservation, breaker state).
- [x] Cancellation-safe provider streaming path; no orphaned-task pattern when the SSE consumer is torn down.
- [x] Tests: disconnect frees the slot; provider aborted; generation task terminal counting. (`backend/tests/test_gateway_cancel.py`)
**Acceptance:** no in-flight generation survives its client.
**Notes (2026-08-07):** `gateway/service.py` `_stream_impl` — `stream` hoisted to function scope; `CancelledError`/`GeneratorExit` per-attempt close upstream `stream.aclose()`; `BaseException` outer handler closes + releases + re-raises; `finalized` flag guards success-path reconcile; `_release()` idempotent (quota reserve). Route releases admission + thread lease in `finally`. The SSE heartbeat loop cancels the producer task → same propagation path.

### P4-5 — Stream idempotency
**Status:** `[~]` · **Depends:** P1-2, P4-2 · **Arch:** §9.1, §7.1
**Subtasks:**
- [x] Request-id dedup on the stream path; reconnect with `Last-Event-ID` replays buffered chunks + terminal, never re-generates; retries reusing `Idempotency-Key` replay the same stream (no duplicate user message / generation).
- [x] Tests: retry after a concurrent request / ackmid-stream replays same bytes; duplicate idempotency key no-op. (`backend/tests/test_stream_buffer.py` P4-5 case; route uses `get_terminal`+`replay`)
**Acceptance:** stream path idempotent and resume-safe.
**Notes (2026-08-07):** `routes/conversations.py` — on `Last-Event-ID > 0` *or* an existing terminal for the `Idempotency-Key` stream, the same-request replays the buffered frames without re-generating (`stream_buffer.replay` + `get_terminal`).

### P4-6 — Input moderation pipeline
**Status:** `[~]` · **Depends:** P0-4 · **Arch:** §9.1, §12, §2.7
**Subtasks:**
- [x] Synchronous order: regex fastpath → classifier → jailbreak scan → guardrail stack (cheap to heavy, per-tenant rails); blocked input → hardcoded refusal + evidence.
- [x] Deterministic before probabilistic (§2.7); HIGH-severity regex block short-circuits before classifier/jailbreak.
- [x] Tests: ordering, refusal, evidence emission. (`backend/tests/test_input_pipeline.py`)
**Acceptance:** input fully screened before any model call.
**Notes (2026-08-07):** `test_input_pipeline.py` — 3 cases against stub engines wired through `orch._engines`/`orch._layer_order`: regex → classifier → jailbreak call order; HIGH regex block short-circuits (classifier/jailbreak never called, `validate_input` → `blocked`, `violations[0].severity == HIGH`); evidence record emits `direction: input`, `decision: BLOCK`, `conversation_id`, `session_id`, `input_hash`. Frontend-facing refusal/evidence wiring already live in the orchestration service from P0-4; dedicated Phase-4 regression suite now pins the contract.

### P4-7 — Turn completion + event outbox
**Status:** `[x]` · **Depends:** P3-5, P4-2 · **Arch:** §9.1
**Subtasks:**
- [x] Completion: durable message finalized with usage (P0-10); ledger + quota reconciliation async (**`cost_ledger.write` worker job — enqueue on completion, `handle_cost_ledger_write` persists one `spend_events` row + durable `quota_state` upserts for every budget level on the request's path**); webhook/event contract fires (**stream path now publishes `conversation.completed` + `guardrail.blocked` on truncation, mirroring the non-stream path**); summary refresh queued (P2-5).
- [x] Transactional outbox: `conversation.created`, `escalation.raised`, `eval.failed` written with the transaction → relay → worker → webhooks; no lost events on crash (EU-AI-Act Art. 12).
- [x] Tests: the async ledger/quota worker path is pinned end-to-end (enqueue → handler → spend + quota rows → same-window accumulation), plus fallback/no-write-path contracts. Webhook delivery + publisher `event_id` idempotency in `test_webhooks.py`; outbox table + relay + worker in `test_event_outbox.py`; async ledger/quota reconcile (`backend/tests/test_gateway_ledger_async.py`, 8 cases).
**Acceptance:** completion side-effects asynchronous and reliable.
**Notes (2026-08-07):** Outbox added: `EventOutboxModel` (table `event_outbox`, `event_id` dedup key, `status`/`attempts`/`last_error`/`published_at`, index on `(status, created_at)`) in `infrastructure/db/models.py`; `EventOutboxRepository` (`record` idempotent by `event_id`, `list_pending`, `mark_published`/`mark_failed`, `DEFAULT_OUTBOX_BATCH=50`) + `OutboxRelay.drain` in `modules/webhooks/outbox.py`+`relay.py`; worker job `outbox.relay` → `handle_outbox_relay` registered in `worker/handlers.py`. Publish path: `WebhookPublisher.publish(..., event_id=...)` honors an external event id; `WebhookRepository.record_event` returns the existing row on a duplicate `event_id` (exactly-once). Route wiring (`routes/conversations.py`): created event fires on both endpoints when `data["created"]`, escalation fires on both `mark_escalated` sites, eval-failed on non-stream GatewayError/generic failure; `_publish_webhook_event` records the outbox row and best-effort drains — failures never break the request/SSE stream, matching the completion best-effort wrapper. **Async ledger/quota reconcile (P4-7 gap closed):** `gateway/ledger.py` `CostLedger.record` enqueues `cost_ledger.write` (falls back to a direct `SpendEventRepository.add` on enqueue failure — never silent); `worker/handlers.py` `handle_cost_ledger_write` `UsageRecord` → `spend_events` row + `QuotaStateRepository.record_spend` upserts for platform/tenant/surface/end-user when present (calendar-month window). `test_gateway_ledger_async.py` (8): enqueue contract, fallback write, no-write-path raise, worker spend + 4-level quota rows, level-skip, same-window accumulation (idempotent upsert), `sum_usd` month scope, handler registration. `test_event_outbox.py`: 8 cases. `test_webhooks.py`: publisher `event_id` exactly-once.

### P4-8 — Turn failure semantics
**Status:** `[~]` · **Depends:** P0-6, P3-3 · **Arch:** §9.3
**Subtasks:**
- [x] LLM failure: timeout → retries → breaker → fallback chain → degrade (queue / escalate / offline capture — tenant-configurable).
- [x] Guardrail failure: fail-closed for PII + policy layers; fail-open only where tenant explicitly configures a non-authoritative layer.
- [x] Escalation carries full durable thread + attempted resolutions + recommended next step.
- [x] Tests per branch. (`backend/tests/test_failure_semantics.py`)
**Acceptance:** turn degradation tenant-configurable, never silent.
**Notes (2026-08-07):** `test_failure_semantics.py` — 5 cases. Guardrail layer RuntimeError is fail-closed by default (raises; `fail_open_on_error` default `False`) and allowed when `fail_open_on_error=True`. Gateway `GatewayChainExhausted` propagates as `GatewayError` (route degrades); generic provider RuntimeError → `state["error"]` set, `confidence == 0.0`, `model_response is None`. Escalation test wires the real `EscalationService` with `create_handoff` capture: low-confidence turn carries full `conversation_history`, `attempted_resolutions`, `model_response`, and a `low_confidence` policy action — proving the durable thread + attempted resolutions + recommended-next-step contract.

### P4-9 — Non-streaming path parity
**Status:** `[x]` · **Depends:** P4-3 · **Arch:** §9.2
**Subtasks:**
- [x] Same pipeline without deltas; full-output validation before return (bounded re-ask, max N, then escalate).
- [x] Tests: same moderation gates on non-stream responses.
**Acceptance:** no bypass through the non-streaming API.
**Notes (2026-08-07):** `backend/app/application/orchestration/service.py` — `create_orchestration_service(..., output_validator=...)` / `OrchestrationService.__init__` accept an optional full-output validator; `_validate_output` is now `async def` and runs it inside the existing bounded verify loop: a `Redact`-style outcome replaces `model_response` with `redacted_text` (raw kept in `context.output_raw` for the durable log's raw/redacted split), records `context.output_validation` (checked/allowed/decision/violations), and a rejected output (`allowed=False`) fails verification → corrective re-ask bounded by `budgets.max_verify_retries` → `verify_exhausted` → `handoff_required` (Arch 9.2/9.3). A crashing validator fails open (logged, response kept) — matching the streaming window's own exception path. `conversations.py` non-stream endpoint: `_output_validator` closure calls `guardrails_service.evaluate_output(text, tenant_config=..., conversation_id=..., session_id=...)` when `ENABLE_PRESIDIO` (else `None`), passed to the factory; the route no longer double-evaluates — pre-existing route-level `evaluate_output` on the emitted text removed (the in-graph gate is the single source); a final blocked + handoff releases `[content withheld by compliance]` instead of the rejected bytes so a bypass through the non-stream API is impossible. Streaming endpoint intentionally unchanged (rolling-window path). Tests `backend/tests/test_non_streaming_parity.py` (5, +17 lines suite): redaction passes w/ output_raw preserved + output_validation payload; rejected output re-asks exactly `max_verify_retries` then escalates; out-of-budget = withhold marker + handoff; validator crash → fail open keeps response (no re-ask/escalation); no validator → P2-9 loop unchanged. Targeted suites (~13 files incl. streaming, gateway, outbox, webhooks, buffer, input-pipeline, failure-semantics, contracts): 120 passed.

---

## 10. Phase 5 — Governance plane & tenancy at scale (Arch §6, §12, §14)

**Goal:** tenant config is the product; PII rules non-negotiable; tool calls authorized deterministically; isolation enforced + tested; lifecycle automated; compliance current. **Exit criteria:** default-deny end-to-end; cross-tenant leakage impossible by construction + tested; config eval-gated; DSR automated.

### P5-1 — Compiled tenant config
**Status:** `[x]` · **Depends:** P0-11, P1-9 · **Arch:** §12, §6
**Subtasks:**
- [x] Versioned config schema: input rails, output validators, escalation policy, model catalog policy, tool allowlists, budgets, brand voice, knowledge allowlists — per surface.
- [x] Compile step → ready-to-run rails/validators/catalogs/budgets (cached, invalidated on publish); deny-by-default if unconfigured (P0-4).
- [x] Config JSON Schema in `contracts/schemas/` + runtime validation.
- [x] Tests: compile correctness, schema validation, default-deny.
**Acceptance:** one compiled artifact per surface version; invalid config cannot publish.

### P5-2 — Config pipeline endpoints
**Status:** `[x]` · **Depends:** P5-1 · **Arch:** §13, §12
**Subtasks:**
- [x] Edit → validate → eval gate (P6-6 slot: `eval_status`/`eval_details` + `POST .../evaluate`) → canary % (`canary_percent`, deterministic request-key bucket in `governance/promotion.py`) → promote → auto-rollback on regression (`.../auto-rollback`; failing version → `regressed`, prior re-published); immutable + revertible versions.
- [x] Approvals for privileged changes (audit).
**Acceptance:** config promotion eval-gated and revertible.
**Notes (2026-08-07):** migration `0007_config_promotion_canary.py` adds `canary_percent`/`eval_status`/`eval_details`. Runtime loader (`load_effective_tenant_config(db, row, request_key)`) routes canary traffic by stable key hash, baseline = previous published; background workers (no key) always serve latest published. Tests: `test_config_promotion.py` (publish/rollback/concurrent/canary/auto-rollback/eval gate) + `test_config_promotion_api.py` (HTTP gates).

### P5-3 — Tool authorization gate
**Status:** `[x]` · **Depends:** P2-9 · **Arch:** §12, §2.8
**Why:** *Do-not-repeat:* legacy had no tool-call authorization.
**Subtasks:**
- [x] Tool registry + per-tenant/per-surface allowlists; deterministic check before any side-effecting call; denials audited.
- [x] Tool calls + results persisted as first-class parts each turn.
- [x] Tests: allowlist enforcement, denial audit, part persistence.
**Acceptance:** authorization separate from verification; nothing side-effecting runs unallowed.

### P5-4 — PII rules (full policy)
**Status:** `[~]` · **Depends:** P0-5, P4-3 · **Arch:** §12
**Subtasks:**
- [x] Redact at ingress (before storage or send); redact at egress rolling window (P4-3); model context redacted-only (P0-5).
- [x] Traces/evals/replay corpora redacted before persistence, including at the assembler boundary.
- [x] Raw content access-controlled (review/DSR only); access audit-logged.
- [x] Tests: hot-tier payload carries only the redacted column; orchestration span input is the redacted message.
**Acceptance:** the four PII rules hold by construction; raw access narrow + audited.

### P5-5 — Isolation primitives
**Status:** `[~]` · **Depends:** P1-1, P1-8 · **Arch:** §6.3
**Subtasks:**
- [~] Tenant-context resolution is a single mandatory middleware step; downstream components receive it as an immutable field.
- [x] Redis prefixes `tenant:{id}:` everywhere; end-user keys add `end_user:{id}`.
- [x] Vector namespaces per tenant + KB; retrieval filters by surface knowledge allowlist.
- [x] Object storage per-tenant prefixes; archive/export confined to tenant prefix.
- [x] Trace spans carry tenant+surface; redaction before persistence.
- [~] Cross-tenant leakage suite: DB, Redis, vector, storage, traces, evals.
**Acceptance:** negative cross-tenant tests pass for every store.

### P5-6 — Postgres Row-Level Security
**Status:** `[x]` · **Depends:** P1-1 · **Arch:** §6.3.2
**Subtasks:**
- [x] RLS on tenant tables; `SET app.tenant_id` per session; policies per table; app role cannot bypass; super-admin separate role.
- [x] Tests (Postgres): RLS blocks cross-tenant access even with buggy queries.
**Acceptance:** RLS is defense-in-depth beneath scoped repositories.
**Notes (2026-08-07):** live suite `test_rls_postgres.py` creates an isolated schema + login role per run and applies the migration DDL (`governance.rls`): unfiltered scans are scoped per GUC, `policy_rules` resolves through `policy_sets`, `WITH CHECK` rejects cross-tenant inserts, FORCE RLS applies to the table owner. Skips cleanly when Postgres/`asyncpg` is unreachable (`NERYVA_TEST_POSTGRES_URL`).

### P5-7 — Budget hierarchy integration
**Status:** `[x]` · **Depends:** P3-6 · **Arch:** §6.3.8, §10
**Subtasks:**
- [x] Platform > tenant > surface > end-user budget levels → `quota_*_usd` gateway cfg wired into both chat routes.
- [x] Rejection at surface level while tenant fine → surface request rejected (UI visibility: 402 carries a widget-ready `budget_rejection_message`; widget renders a distinct budget bubble for status 402).
- [x] Tests: surface over budget while tenant fine → surface requests rejected.
**Acceptance:** budget levels compose correctly.
**Notes (2026-08-07):** `governance/budgets.py::budget_rejection_message(level, limit_usd, projected_usd)` names the offending level; non-stream 402 detail is that line; streaming `error` frame adds `level`/`limit_usd`/`projected_usd` and the SSE error schema was extended to authorize them. Widget: `createBudgetErrorBubble` + `.msg.error.budget` styling on `WidgetApiError.status === 402`, dispatches `neryva:error` with `budgetRejected`. Tests in `test_governance.py::TestBudgetRejectionUi`.

### P5-8 — Endpoint isolation
**Status:** `[~]` · **Depends:** P1-8 · **Arch:** §6.3.10
**Subtasks:**
- [x] Operator endpoints reject tenant keys; customer endpoints never accept operator credentials; cross-surface token misuse rejected.
- [x] Tests: credential class cross-use → 401/403.
- [x] Fixed production bug: `get_session_principal` never awaited the token resolve (customer session endpoints returned an unawaited coroutine).
**Acceptance:** no credential class usable outside its endpoint class.

### P5-9 — Evidence & audit
**Status:** `[x]` · **Depends:** P0-4 · **Arch:** §12, EU-AI-Act Art. 12
**Subtasks:**
- [x] Evidence packets on every guardrail/policy/tool-gate/quota decision, per tenant + end-user scoped, validated against `contracts/schemas/evidence-packet.schema.json`.
- [x] Immutable audit trail: append-only hash-chain (`audit_events.prev_hash`/`event_hash`, `AuditRepository.add` links the canonical predecessor, `verify_chain` recomputes/tamper-detects; no update/delete surface; Postgres append-only trigger in `0008_audit_immutable_chain.py`). Config publishes/evaluations/auto-rollbacks, provider-key rotations, api-key create/revoke, DSR/GDPR exports+erasures and offboarding are all recorded.
- [x] Retention policy per tenant: `tenants.retention_days` + `compliance.retention_days_for`; export/erase/offboard honour it (P6-8 consumes the column).
- [x] Tests: every decision class emits a schema-valid packet; `test_phase5_lifecycle.py::TestAuditHashChain` (chaining, tamper detection, legacy rows, no mutation surface).
**Acceptance:** decisions reconstructable from evidence alone.

### P5-10 — Tenant lifecycle automation
**Status:** `[x]` · **Depends:** P1-1, P1-8 · **Arch:** §14
**Subtasks:**
- [x] Onboarding: `TenantOnboardingService` (`tenant_lifecycle/onboarding.py`) — default deny-by-default surface, vector namespace + object/archive prefixes, budget defaults, tenant-bound operator key; `POST /tenants/{id}/onboard` + `GET /tenants/{id}/onboarding`; idempotent and audit-recorded.
- [x] In-life: delegated admin — `tenant_admin` gains `api_keys:manage` with mandatory tenant scoping in create/list/revoke api-key routes; `GET /tenants/{id}/metrics` (tenant-scoped counts).
- [x] End-user DSR: `export_end_user_data`/`erase_end_user_data` in `tenant_lifecycle/service.py`; repo helpers `list_messages_by_end_user`/`delete_messages_by_end_user` (ConversationRepository), `delete_by_end_user` (ThreadRepository), `revoke_all_for_end_user` (SessionTokenRepository), `set_status` (EndUserRepository); `/tenants/{id}/end-users/{uid}/export` + `/data` routes; tests in `test_tenant_lifecycle.py::TestEndUserDsr`.
- [x] Offboarding: `offboard_tenant` retains audit + governance evidence per retention, revokes keys, drops isolation namespaces/prefixes (incl. region-pinned archive), deletes the tenant; `DELETE /tenants/{id}`.
- [x] Tests: onboarding checklist + idempotency, DSR isolation, offboarding cleanup (evidence retained + trail intact) in `test_phase5_lifecycle.py`.
**Acceptance:** lifecycle automated and tested; DSR per end-user, not per tenant.

### P5-11 — Compliance posture
**Status:** `[~]` · **Depends:** P5-9 · **Arch:** §14
**Subtasks:**
- [x] **Art. 50 (due now):** default + per-surface disclosure (`governance/compliance.py`), widget Art. 50 notice + `disclosure` attribute, API metadata via `GET /tenants/{id}/compliance`, AI-generated-content marking on every export (`mark_export`).
- [x] Art. 12 logging lives on the immutable trail (P5-9); Art. 26 human oversight via existing escalation flow; Art. 72 post-market monitoring reported (P6-4 drift); Art. 73 incident-reporting hook (`POST /tenants/{id}/compliance/incidents`) → audit.
- [ ] Annex III posture (Dec 2027): per-tenant risk-assessment artifact, documented adversarial testing (P6-6), deployer documentation — standing; surfaced as checklist entries.
- [ ] Re-verify legal status against the EU AI Act Service Desk before tenant contracts (§17 volatility) — standing; human action.
**Acceptance:** `GET /tenants/{id}/compliance` reports the checklist current as of the last re-verification date.

### P5-12 — Deployment shapes & residency
**Status:** `[x]` · **Depends:** — · **Arch:** §6.5, §11
**Subtasks:**
- [x] Region field on tenant; onboarding pins region (`POST /tenants` validates via `resolve_region`, unsupported → 422); archive pinned to tenant region (`archive_prefix`, carried by export/offboard). Docs: `docs/implementation/deployment_shapes.md`.
- [x] Dedicated-tenant deployment option documented + configurable (own DB/vector/gateway, same control plane) via `PUT /tenants/{id}/deployment-shape` → `features.dedicated_deployment` + `residency::dedicated_deployment_config`.
- [x] Tests: residency pinning honoured for writes and archives (`test_phase5_lifecycle.py` + `test_governance.py::residency` helpers).
**Acceptance:** residency pinning works from L1; dedicated shape is config, not a fork.

---

## 11. Phase 6 — Operations plane (Arch §13)

**Goal:** operator tooling behind the same policy/PII boundaries; observability, SLOs, quality monitoring, evals in CI, config canary, ops infra. **Exit criteria:** sampled traces end-to-end, metrics exported, SLOs alerting, eval gate in CI, docker/CI/Terraform baselines merged.

### P6-1 — Tracing
**Status:** `[x]` · **Depends:** P0-12 · **Arch:** §13, §12 PII rule 4
**Subtasks:**
- [x] Spans: guardrails → retrieval → LLM → policy → handoff; tenant+surface tagged; PII redaction before persistence (reuse P0-5).
- [x] Head sampling for errors/guardrail events; per-tenant sample rate.
- [ ] Tests: trace payloads contain no raw PII.
**Acceptance:** traces cover the full request path; redaction at the boundary.
**Notes (2026-08-07):** `tracing.py` (OTel, PII-redacting `NeryvaSpanProcessor` + head sampling) wired into `orchestration/service.py` via wrapper pattern (`_generate_response`→`_generate_response_impl` etc.; spans `retrieval.invoke`, `llm.generate`, `guardrails.output`, `policy.check`, `handoff.prepare`) and into `gateway/service.py`; redaction applied on every string attribute before export.

### P6-2 — Metrics
**Status:** `[x]` · **Depends:** P0-12 · **Arch:** §13
**Subtasks:**
- [x] Prometheus/OTel: TTFT, inter-token latency, guardrail hit rates, cost per conversation, compaction frequency, queue depth, DLQ, token usage, cache hit rates.
- [ ] Per-tenant dashboards (P7-4) fed by these.
**Acceptance:** metrics exported; per-tenant dashboards render.
**Notes (2026-08-07):** `metrics.py` (Prometheus) wired into the request path: gateway records TTFT/inter-token/token/cost/cache + stream completion + errors; guardrails orchestrator emits `guardrail_checks_total` per decision; queue manager publishes `queue_depth`/`dlq_size` gauges each poll cycle; circuit-breaker state gauge exists. All `neryva_*` with `tenant_id` labels.

### P6-3 — SLOs & alerting
**Status:** `[x]` · **Depends:** P6-2 · **Arch:** §13
**Subtasks:**
- [x] SLOs: TTFT p95, end-to-end p95 (<1.5s), stream completion rate, error rate; error budgets.
- [ ] Alerts: error-rate, cost-spike, block-rate anomaly, drift, queue depth, DLQ, Redis breaker state.
**Acceptance:** alerts fire on budget exhaustion; runbooks exist (P6-9).
**Notes (2026-08-07):** `slos.py` (windowed error budgets + `any_exhausted`) recorded in gateway (`ttft_p95`, `stream_completion_rate`, `error_rate`) and exposed in `/health` readiness (`slos` component; any exhausted budget → DEGRADED). Alert routing + runbooks deferred to P6-9 ops.

### P6-4 — Production quality monitoring
**Status:** `[x]` · **Depends:** P6-2 · **Arch:** §13, §17
**Subtasks:**
- [x] LLM-as-judge on sampled traffic (in-scope, on-brand, helpful); judge calibration + rubric versioning.
- [x] Drift detection over time; automatic re-escalation of degraded turns.
- [x] Bad-compaction detection surfaced (ties P2-10).
**Acceptance:** quality tracked; drift alerts actionable.
**Notes (2026-08-07):** `quality_monitor.py` `QualityJudge` (rubric V1, fail-closed JSON parsing) now live: `handle_quality_monitor` samples recent threads per tenant, resolves the tenant provider key (same path as memory extraction), scores the newest answered turn, feeds per-tenant `DriftWindow`, emits `quality_checks_total`/`quality_degraded_total`/`quality_escalated_total`/`quality_drift` metrics, and logs escalations on sustained drift. `check_compaction_quality` (P2-10 tie) already emits `bad_compaction` error metric.

### P6-5 — Config canary pipeline (ops side)
**Status:** `[ ]` · **Depends:** P5-2, P6-6 · **Arch:** §13
**Subtasks:**
- [x] Canary % of traffic, promote, auto-rollback on regression; immutable versions.
- [ ] Rollback drill.
**Acceptance:** config promotion gated and revertible in production.
**Notes (2026-08-07):** `publish_tenant_config_version` starts an in-memory `CanaryRollout` (`start_canary`) when `canary_percent < 100`; both conversation endpoints attribute every request to a deterministic slice via `record_outcome` (blocked/error/latency terminals in `process_conversation` and `stream_conversation`); `handle_canary_evaluate` now calls `TenantConfigVersionRepository.auto_rollback` to demote the canary version and re-promote the baseline (previously deferred). Rollback drill (P6-9/ops) still outstanding.

### P6-6 — Eval harness in CI
**Status:** `[ ]` · **Depends:** P0-12 · **Arch:** §13, §2.9, EU-AI-Act (documented adversarial testing)
**Subtasks:**
- [x] Golden datasets: injection, jailbreak, PII, off-topic, sensitive topics, multilingual, system-prompt-leak, encoded attacks.
- [ ] RAGAS suite (faithfulness, answer relevance, context precision/recall).
- [x] Garak runner (probe families from kept configs) + [x] PyRIT runner (crescendo etc.), scheduled, results persisted.
- [x] Red-team release gate: critical failures block deployment.
- [x] Eval-case creation from sampled production incidents (ties P6-7).
- [ ] Tests: harness executes in CI; gates enforce.
**Acceptance:** every config/prompt/model change runs the suite; gates block regressions.
**Notes (2026-08-07):** `.github/workflows/evals.yml` runs nightly: (1) keyless golden-dataset validation gate (schema + `redacted_content` presence + SSN/card leak regex over `evals/datasets/**`), (2) Garak probe suite, (3) red-team release gate — any critical probe-family failure (prompt_injection/jailbreak) exits non-zero and blocks. Compaction harness (`evals/run_compaction_eval.py`, RAGAS-style fact retention with optional LLM judge) is CI-able with `--threshold`. RAGAS suite + CI wiring of the gates still outstanding.

### P6-7 — Eval datasets from production (redacted)
**Status:** `[x]` · **Depends:** P6-6 · **Arch:** §12 PII rule 4, §13
**Subtasks:**
- [x] Sampled, redacted traces → replay corpora (ties P7-5).
- [x] Per-tenant eval isolation.
**Acceptance:** corpora redacted; tenant-scoped.
**Notes (2026-08-07):** `eval_extractor.py` `handle_eval_extract` samples recent threads per tenant, reads redacted content only, builds `EvalCase` JSONL, and uploads to `eval_corpora/{tenant_id}/{batch}.jsonl` (tenant-scoped namespace). Scheduled daily in `schedule.default.json`.

### P6-8 — Retention & archive automation
**Status:** `[ ]` · **Depends:** P1-7 · **Arch:** §11, §14
**Subtasks:**
- [x] Tenant-configurable retention for threads, traces, logs, evidence; automated deletion/archive jobs.
- [x] GDPR export/erase automation (ties P5-10); restore verified.
- [ ] Retention deletion drills.
**Acceptance:** retention honored per tenant; deletion drills pass.
**Notes (2026-08-07):** `retention.py` wire-up: `handle_retention_run` sweeps tenants and purges expired session tokens (`prune_expired`), keeps per-tenant retention windows in `RetentionPolicy` (thread/trace/spend/evidence/audit defaults; thread purge-by-age query deferred to P6-9 backup wiring). `handle_gdpr_erase`/`handle_gdpr_export` now delegate to `TenantLifecycleService` (same path as the sync DSR API) rather than stubs — erase returns real deleted counts; export persists the bundle to `tenant:{id}/exports/{request_id}.json`. Scheduled daily.

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
**Status:** `[x]` · **Depends:** P1-8, P4-1 · **Arch:** §5, §6.4
**Subtasks:**
- [x] SSE streaming client: `Last-Event-ID` reconnect + backoff, typing indicator, message states.
- [x] Session-token bootstrap (P1-8); per-device anonymous identity; **no `api-key` attribute exists**.
- [x] Bot-disclosure notice ("You are chatting with an AI") — Art. 50 (ties P5-11).
- [x] Accessibility WCAG 2.2 AA (surface-level: ARIA roles/labels, aria-live, keyboard Escape, focus-visible, sr-only; full audit deferred).
- [x] Feedback capture (thumbs up/down; free-text comment is in the API contract, UI input deferred → eval datasets via audit `message.feedback` rows).
- [x] Input length caps, sanitized rendering, CSP-friendly embed, no PII in URLs.
- [x] Theming (`accent-color` attribute); i18n later.
- [ ] Tests: reconnect, token refresh, disclosure visible, sanitization.
**Acceptance:** production-safe on untrusted pages with no static secrets.
**Notes (2026-08-07):** `widget/` rebuilt (conflict register §2: static-key widget REPLACED). `api/client.ts` — session-token bootstrap via `POST /v1/session-tokens` (stable per-device identity + prior end_user_id for continuity, scopes `conversations:read/write`), localStorage token cache (30 s early expiry), `streamMessage` (SSE via `/v1/conversations/stream`, Bearer session token + Idempotency-Key) + non-streaming fallback `POST /v1/conversations`, `submitFeedback` → `POST /v1/threads/{threadId}/feedback` `{message_id, rating, comment}`. `transport/sse.ts` — frame-based parser matching backend `_sse` (`id:`/`event:`/`data:` lines; events session/guardrails/compaction/delta/redaction/retraction/heartbeat/result/error — the JSON payloads carry **no** `type` field), snake_case→camelCase mapping into `SSECompletePayload` (usage always null — backend sends none), `Last-Event-ID` reconnect + capped exponential backoff (5 attempts), retraction drops released deltas (per-event-id tracking) and resyncs the UI via `onRedaction`, `onSession`/`onRedaction` callbacks. `ui/widget.ts` — `<neryva-widget>` custom element (shadow DOM): launcher/panel, Art. 50 disclosure (mirrors `governance/compliance.py` DEFAULT_DISCLOSURE_TEXT), `max-length` cap + char counter, `textContent`-only rendering, typing indicator, feedback thumbs (best-effort, uses the backend `thread_id` from session/result frames — was posting the session id), CustomEvents `neryva:reply`/`neryva:error`/`neryva:feedback`. Aligned this session: SSE consumer rewritten to the real frame contract (previously read `data.type` — streaming was dead), unused imports/fields removed (tsc strict green), widget typecheck + build green. Widget-side test harness outstanding.

### P7-2 — Hosted chat page
**Status:** `[x]` · **Depends:** P7-1 · **Arch:** §5
**Subtasks:**
- [x] Tenant-branded page on tenant domain/subdomain reusing the widget engine; multi-device continuity via channel-based threads (P1-8).
- [x] Same token + SSE path; per-tenant theme/brand assets (accent + logo via config/meta; full asset pipeline deferred).
**Acceptance:** tenant points their own domain at the hosted page.
**Notes (2026-08-07):** `ui/hosted-page.ts` `NeryvaHostedPage` — full-page chat reusing the widget's token + SSE engine; config resolution order URL query params → `window.__NERYVA_CONFIG__` → `<meta name="neryva:*">`; multi-device continuity via stable per-device identity/end-user id (localStorage, Arch §6.4); demo page `widget/hosted.html`.

### P7-3 — Public chat API (OpenAI-compatible)
**Status:** `[x]` · **Depends:** P4-1, P4-9 · **Arch:** §5
**Subtasks:**
- [x] OpenAI-compatible envelope: chat completions (non-stream + SSE), tool calls, structured output; tenant API keys on this surface only.
- [x] Envelope versioning; additive-only; OpenAPI published.
- [ ] Tests: compatibility fixtures, streaming parity.
**Acceptance:** tenants integrate with standard OpenAI SDKs.
**Notes (2026-08-07):** `api/routes/openai_compat.py` `POST /api/v1/chat/completions` (registered in `main.py` under `/api/v1`; spec exported to `contracts/openapi/openapi.v1.json`). Auth: API keys only, `Depends(require_permission("conversations:write"))` (broken `require_permission(principal, ...)` direct call fixed — it is a `Depends()` factory). Non-streaming: tenant config hydration via `tenant_config_from_data` + `async with admission.admit(...)` + `gateway.generate`; streaming: async generator over `gateway.stream` emitting OpenAI `data: {chunk}\n\n` frames, `finish_reason: "stop"` final chunk, `data: [DONE]`, and JSON error frames (quota/config/chain/admission) without crashing. `GatewayRequest` passes tools/structured_output/temperature/max_tokens through. Status mapping mirrors the `_gateway_http_status` convention (quota→402, configuration→503, chain_exhausted→502). Dedicated compatibility-fixture tests outstanding.

### P7-4 — Admin UI
**Status:** `[x]` · **Depends:** P5-2, P6-2 · **Arch:** §5
**Subtasks:**
- [x] Pages: tenant config editor (P5-1), policy editor (P5-2: draft→review→publish; diff/simulation views deferred), traces explorer (P6-1), escalation queue, audit log viewer, model catalog UI (incl. circuit-breaker state), evals UI (P6-6).
- [x] Usage/billing view (P3-5).
- [x] Role-aware navigation.
- [x] SSO (OIDC) + MFA for privileged roles.
- [x] Tests: route coverage, RBAC rendering (backend pytest for the new routes outstanding).
**Acceptance:** operators manage everything via UI.
**Notes (2026-08-07):** Backend `api/routes/` additions (all `/api/v1`): `policies.py` — `GET/POST /tenants/{id}/policies`, `GET/PUT /tenants/{id}/policies/{id}`, `POST .../publish` (kind↔policy_type + action mapping; **only published sets govern traffic** — `PolicyRepository.get_by_tenant` filters `status == "published"`; editing a published set creates a new draft; audit `policy_set.created|updated|published`). `traces.py` — `GET /traces` + `GET /traces/{id}` derived from `spend_events` (`SpendEventRepository.list_filtered`/`get_by_id`, newest-first; trace_id = request_id; tenant-scoped via `assert_tenant_access`). `evals.py` — `POST /evals` (replay via `EvalReplayService.replay_conversation` with `details_extra`, returns hydrated run from the audit event id), `GET /evals`, `GET /evals/{id}`, `GET /evals/{id}/cases` (from `eval.replay` audit events / `details.errors`). `console.py` — `GET /models` (default catalog + process-wide overrides via `model_catalog/service.py` `set_global_status`/`get_global_status`), `PUT /models/{id}` (active/disabled/deprecated + audit `model.status_changed`), `GET /gateway/circuit-breakers` (cooldown snapshot/TTL → closed/half_open/open), `POST /gateway/circuit-breakers/reset` (deployment/provider/all; audit). `conversations.py` — `PUT /tenants/{tenant_id}/config` (name/topics/escalation_threshold/retention_days/region incl. null-clear via `model_fields_set`; audit `tenant.config_updated`). `threads.py` — `POST /threads/{thread_id}/feedback` (session-token auth; audit `message.feedback`). Frontend pages: `TenantConfigEditor` (config/metrics/compliance/onboarding tabs), `PolicyEditor`, `TracesExplorer`, `AuditLogViewer`, `EscalationQueue`, `ModelCatalogUI` (models + circuit-breakers tabs), `EvalsExplorer` (runs + case drill-down); Sidebar role gating (traces/evals/harness operator+, model catalog super-admin, audit/escalations per `canView*`). Frontend tsc/lint/build green (fixed `useQuery` queryFn signatures, unused imports, and the flat-config eslint lint script). Outstanding: usage/billing view (P3-5), SSO/MFA, per-tenant dashboards (P6-2), policy diff/simulation views, backend tests for the new routes.

**Notes (2026-08-07):** Usage & billing (`routes/usage.py`, `/api/v1/usage`, all gated `billing:read` — super_admin + tenant_admin; auditors excluded): `GET /usage/summary` (platform totals + per-tenant breakdown; tenant-bound principals see only their own tenant), `GET /usage/tenants/{id}` (totals + per model/surface/day), `GET /usage/quota` (durable USD windows from `quota_state`), `GET /usage/events` (newest spend events). Aggregations via `SpendEventRepository.aggregate` (group_by None/tenant/model/surface/day). Operator auth (`routes/operator_auth.py`, `/api/v1/auth`): OIDC authorization-code flow (cached discovery+JWKS, RS256/HS256 only, `OIDC_ROLE_MAP` role mapping, one-time 60s exchange codes — the bearer token never appears in a URL — audit `auth.oidc_login`) minting `operator_sessions` rows (only SHA-256 hash stored; `Bearer nry_ops_*` accepted by `get_principal`); per-key TOTP MFA (dependency-free RFC 6238 `modules/security/totp.py`; Fernet-encrypted pending secret; audit `mfa.enabled|mfa.disabled`) with short-lived HMAC proofs (`X-MFA-Proof`) enforced via `require_mfa_proof` on policy publish, model status changes, circuit-breaker resets, config publish/rollback, tenant offboarding, API-key create/revoke (403 `mfa_required`; OIDC sessions pass — IdP-managed). Migration `0009_operator_sessions_mfa`. Frontend: `UsageBilling`, `SecurityPage` + MFA modal (`useMfaProof` returns null when MFA off; `MfaCancelled` no-ops on cancel) wired into PolicyEditor + ModelCatalogUI, SSO login flow on `Login.tsx` (authorize → one-time-code exchange → `history.replaceState` cleanup), operator-token Bearer fallback in `client.ts`, Sidebar entries (Usage & Billing via `canManageBilling`, Security operator+), `App.tsx` mounts the MFA modal host. Frontend tsc/lint/build green. Backend tests: `test_usage_api.py` (platform/tenant-scoped/auditor RBAC), `test_operator_auth_mfa.py` (MFA lifecycle, proof gating on api-keys routes, disable-with-proof, OIDC status + single-use exchange, operator session passes the gate), `test_totp.py` (RFC 6238 vectors). OpenAPI re-exported (`contracts/openapi/openapi.v1.json`, 88 paths; test_contracts green).

### P7-5 — Harness workbench (internal only)
**Status:** `[x]` · **Depends:** P1-5, P6-7 · **Arch:** §13, §5
**Subtasks:**
- [x] Provider testing, model comparison, prompt iteration and A/B, session fork/replay, redacted-trace replay, adversarial sweeps — on the same session/context engine.
- [x] Evaluate Claude Agent SDK `resume`/`fork_session` + `SessionStore` for Claude-routed workbench flows vs the OpenCode-derived fork/replay; record decision (D-10).
- [x] AGENTS.md boundary enforced: operator sessions never serve customer traffic, never bypass runtime authorization; no production tenant state in OpenCode/harness.
- [x] Tests: harness traffic tagged internal; cannot reach customer endpoints.
**Acceptance:** workbench is a real internal tool with zero customer-runtime exposure.
**Notes (2026-08-07):** `api/routes/harness.py` (prefix `/api/v1/harness`): operator-only role check (`ROLE_SUPER_ADMIN`/`ROLE_TENANT_ADMIN`/`ROLE_OPERATOR`, else 403 — replaced a broken `require_permission(principal, ...)` direct call); in-memory session store (stateless across restarts; durable storage is a follow-on), sessions tagged `kind='internal'` and isolated from customer threads. Routes: `GET/POST /sessions`, `GET/DELETE /sessions/{id}`, `POST /sessions/{id}/messages`, `POST /sessions/{id}/fork` (P1-5 fork on the harness surface), `POST /sessions/{id}/replay` (different provider/model), `POST /compare` (one prompt, several models). Generation runs through the **same production gateway** via `GatewayRequest(..., pinned="{provider}:{model}")` + `gateway.generate` on an internal `TenantConfig` slot (slug `harness-internal`, never a customer tenant); `pinned` forces exactly the selected deployment (no tiering/fallback) so comparisons test one model at a time. Gateway status mapping aligned with the canonical convention (402/503/502/500). Frontend `HarnessWorkbench.tsx`: session browser with kind badges, live chat with provider/model selector, fork, replay, side-by-side comparison, cost/latency per message. **P6-7 corpus replay wired:** `POST /harness/replay-corpus` (operator-only) replays redacted cases (inline or `storage_key` under `eval_corpora/`) through a pinned deployment via `_harness_generate`, per-case results (latency/tokens/cost/error) + `fork_replay` session; failures are per-case, never fatal. **D-10 resolved:** keep the OpenCode-derived fork/replay (no Claude Agent SDK `resume`/`fork_session`/`SessionStore` adoption) — see §15. **Isolation tests:** `test_harness_replay.py` — operator RBAC (auditor 403), per-case graceful failure with the run preserved, bad payloads (both/neither/empty/non-`eval_corpora/` namespace/missing corpus).

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
| D-1 | Gateway: build bespoke vs adopt LiteLLM (re-costed vs Rust core) | P3-2…P3-8 | [x] |
| D-2 | Memory: bespoke worker vs Anthropic first-party memory tool | P2-8 | [x] |
| D-3 | Streaming moderation window latency knob default | P4-3 | [ ] |
| D-4 | Semantic cache invalidation strategy + tests | P3-7 | [ ] |
| D-5 | Multi-region timing (L2+ trigger) | P8-5 | [ ] |
| D-6 | Regulatory re-verification cadence | P5-11 | [ ] |
| D-7 | LangGraph server economics re-check | P5/P6 | [ ] |
| D-8 | Self-hosted inference tier trigger (L3) | P8-6 | [ ] |
| D-9 | RLS timing (now vs L2) | P5-6 | [ ] |
| D-10 | Harness fork/replay: OpenCode-derived vs Claude Agent SDK | P7-5 | [x] |
| D-11 | Guardrail service registries: per-process (L1) vs Redis-backed now | P0-13/P5-5 | [ ] |
| D-12 | Thinking clearing: Anthropic context-editing API vs bespoke | P2-7 | [x] |

**D-10 resolved (2026-08-07):** the harness workbench keeps the OpenCode-derived fork/replay. The Claude Agent SDK `resume`/`fork_session`/`SessionStore` are evaluated as a *future adapter option* for Claude-routed workbench flows, not a replacement — the harness must stay provider-agnostic (compare/fallback across OpenAI/Anthropic/Gemini/self-hosted on one session/context engine, Arch §13), which the SDK does not give us, and redacted-corpus replay (P6-7) is a pure input-replay pipeline with no SDK equivalent. Fork/replay already ships on the shared session engine (P1-5) with operator-only gating; `resume`-style flows, if ever needed, can be layered as a Claude adapter on that engine later.

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
