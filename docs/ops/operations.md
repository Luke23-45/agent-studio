# Multi-worker safety & operations (P0-13)

State model, init order, and failure semantics for running multiple API/worker
processes. This is the contract that makes horizontal scaling safe from day one.

## Shared state lives in Redis / Postgres — never in a process

| Component | Shared medium | In-process fallback (degraded, never silent) |
|---|---|---|
| Queue (`infrastructure/queue`) | Redis lists/zsets `neryva:queue:*`, idempotency claims | in-memory queue; DEGRADED health |
| Cache (`infrastructure/cache`) | Redis | in-memory dict; DEGRADED health |
| Rate limits (`patterns/rate_limiter`) | Redis Lua token buckets `neryva:rl:*` | per-process token buckets; alert log + DEGRADED health |
| Admission (`gateway/admission`) | Redis slot counters `neryva:admission:*` (TTL lease) | per-process semaphores; DEGRADED health |
| Tenants / policies / keys / config versions / conversations / spend | Postgres (source of truth) | n/a (direct reads) |
| Vector store | pgvector / storage | in-memory / local fs; DEGRADED health |

Request-path state (sessions, message parts, spend events) is written to
Postgres/Redis; nothing request-scoped lives in process memory.

## Init order (mirrors `backend/app/main.py` lifespan)

1. Tenant config service (filesystem path only).
2. Database: `init_database` → `initialize` → (optional) migrations → bootstrap API key.
3. Infrastructure managers: cache, queue, storage, admission gate, rate limiter.
   All five are initialized with `asyncio.gather` — each one independently
   falls back to its in-process mode when its backend is unreachable.
4. Shutdown mirrors init: db, cache, queue, storage, admission close in one
   gather (best-effort; failures are logged).

## Failure semantics (contract)

- Startup never fails because Redis/S3 are down: every manager degrades.
- Degradation is **never silent**: transition log at error level + DEGRADED in
  `/health` (readiness), so operators alert on `status: degraded`.
- `/health/live` = process alive (no dependency checks). `/health` = readiness
  across db/queue/cache/storage/engines/rate_limiter/admission.
- Provider credentials decrypt with the KMS-injected master key; if the key is
  missing in production the key service refuses to start (no auto-generation).
- LLM calls run through the resilient gateway (timeout → breaker → retry);
  the breaker and the per-tenant provider-client registry are in-process today
  and become Redis-shared in P3-4.

## Verification

`backend/tests/test_multi_worker.py`:

- Two OS processes share queue state through Redis (enqueue in A, dequeue in B).
- With Redis unreachable, two processes still boot and fall back in-process
  (exit code 0), and the limiter/admission report DEGRADED.
