# Schema discipline audit (P8-2, Arch §11)

Audit of `backend/app/infrastructure/db/models.py` as of 2026-08-07 against
the P8-2 checklist: ordering, partitioning, and composite `(tenant_id, ...)`
index coverage.

## Ordering

- The turn log (`messages`, `thread_events`) is ordered by **per-thread
  integer `seq`**, enforced unique via `(thread_id, seq)` — ULID-equivalent
  append order with zero clock-skew risk. All pagination (cursor
  `after_seq`, tail reads) uses `seq`, never wall-clock `created_at` for
  correctness.
- Listing order (threads, evidence, audit, spend) uses `created_at` with
  matching composite indexes (`tenant_id, created_at`); these are
  presentation queries, not correctness-critical.

## Partitioning

- **Vertical** partitioning is confirmed by construction: heavy content
  lives in `message_parts` (typed `content`/`redacted_content` JSONB),
  message metadata in `messages`, and mutations in the append-only
  `thread_events` log — a reader fetching a page of message rows never
  pulls parts payloads.
- **Hot/durable/cold** (Arch 7.3): hot = Redis tail cache + last-active
  buffer; durable = Postgres; cold = region-pinned archives (P5-12).
- **Horizontal** (P8-4): schema is tenant-hash-shard-compatible — every
  data table carries `tenant_id` and thread-scoped uniqueness keys live on
  `(thread_id, seq)` which stay globally unique after sharding.

## Composite index coverage

Every tenant-scoped access path is served by an index leading with
`tenant_id` (or by a globally unique key that subsumes it):

| Table | Index | Serves |
|---|---|---|
| `messages` | `(tenant_id, thread_id, seq)` | thread reads + pagination (new, P8-2) |
| `messages` | unique `(thread_id, seq)` | append + seq bounds |
| `messages` | `(tenant_id, created_at)` | tenant listings |
| `messages` | `(conversation_id, created_at)` | conversation-scoped reads |
| `messages` | unique `request_id` | idempotent appends (P1-1) |
| `thread_events` | `(tenant_id, thread_id, seq)` | event log reads (new, P8-2) |
| `thread_events` | unique `(thread_id, seq)` | event append bounds |
| `threads` | `(tenant_id, created_at)`, `(tenant_id, end_user_id)` | listings / DSR |
| `message_parts` | unique `(message_id, part_index)`, `(thread_id, part_index)` | part reads |
| `memories` | `(tenant_id, end_user_id)`, `(thread_id, source_seq)` | retrieval / source link |
| `spend_events` | `(tenant_id, created_at)` | usage rollups |
| `quota_state` | unique `(scope_type, tenant_id, surface_id, end_user_id, window)` | reservation lookups |
| `session_tokens` | `(tenant_id, end_user_id)`, `(expires_at)` | resolution + pruning |
| `event_outbox` | `(status, created_at)` | relay drain |
| `audit_events` / `guardrail_evidence` | `(tenant_id, created_at)` | trail queries |

Residual predicates: a handful of thread-scoped queries filter `tenant_id`
in addition to a globally unique key (`thread_id` is a UUID); the planner
uses the unique key and applies `tenant_id` as a post-filter — correct
today and pruned by `(tenant_id, ...)` composites after P8-4 sharding.

## Known scans (accepted)

- `list_threads_stale_for_compaction` aggregates `max(seq)` per thread —
  a scan over active threads. Background worker, batched, bounded by
  `limit`; accepted and re-visited with sharding (per-shard scans shrink
  linearly).

## Follow-up shipped with this audit

- Migration `0010_phase8_schema_discipline`: additive `(tenant_id,
  thread_id, seq)` composites on `messages` and `thread_events` (CREATE
  INDEX only; downgrade drops them). No existing query plan changes.
