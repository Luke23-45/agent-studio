# Turn-log sharding design (P8-4, L3 readiness)

Design only — **no code ships**. The L1 schema is already shard-compatible
(P1-1, P8-2); this document records the sharding plan, trigger conditions,
and migration path so L3 requires no rewrite.

## Trigger conditions (when to shard)

Sharding is an L3 exercise. Enter it when any of these hold for a sustained
period (post-L2, with read replicas live):

1. Primary write throughput exceeds single-Postgres capacity
   (> ~10 k tps sustained, or WAL generation outgrows replica catch-up).
2. `messages` + `thread_events` row count exceeds ~2–3 B rows combined and
   index maintenance cost (autovacuum, page churn) starts showing in p95
   append latency.
3. A single tenant's hot thread volume monopolizes buffer cache (hot-tenant
   problem) and the replica + RYW architecture can no longer absorb it.

Until a trigger holds, **do not shard**: replicas (P8-3) plus vertical
partitioning (P8-2) are cheaper and operationally simpler.

## Shard key: tenant hash

- Shard by `hash(tenant_id) % N` (consistent hashing with a fixed ring,
  `N` chosen as a power of two; virtual nodes if rebalance matters).
- `tenant_id` is already a non-null column on every data table
  (`messages`, `message_parts`, `thread_events`, `threads`, `memories`,
  `spend_events`, ...). Postgres 16+ `pg_hash` / `md5` → integer mapping;
  deterministic, no per-row storage.
- Routing is **application-side**: a shard resolver maps
  `tenant_id → shard DSN` from a config table/versioned JSON (same pattern
  as `deployment_shapes` dedicated-pool selection — config, never a fork).
- Read-your-writes and replica routing (P8-3) compose per shard: each
  shard owns its own primary + replicas; the `ReplicaRouter` key model
  (tenant, thread, ...) is unchanged.

## What moves (and what does not)

| Table | Shard | Why |
|---|---|---|
| `messages`, `message_parts`, `thread_events`, `threads` | yes | the turn log — 100% tenant-scoped access |
| `memories` | yes | tenant/end-user scoped |
| `spend_events` | yes | tenant-scoped; rollups can fan out per shard |
| `quota_state` | yes | tenant-scoped reservation state |
| `conversations` | yes | tenant-scoped |
| `event_outbox` | yes (per shard) | tenant-scoped delivery |
| `tenants`, `policy_sets`, `model_catalog`, `api_keys`, `audit_events`, `session_tokens`, `webhook_subscriptions`, `surfaces`, `tool_registry`, `tenant_provider_keys` | no | control plane, low volume, global invariants (hash chain, unique key hashes) |

Cross-shard joins are designed out today: `messages → message_parts` and
`thread_events` are keyed by `thread_id` (globally unique UUID) — all turn
log reads enter through `(tenant_id, thread_id)` and stay within one shard.

## Schema compatibility (verified in P8-2)

- `(tenant_id, thread_id, seq)` composites on `messages`/`thread_events`
  (migration 0010) give Postgres partition pruning the leading column it
  needs; `(thread_id, seq)` uniqueness keys are globally unique and survive
  the move unchanged.
- No cross-tenant FK exists: `messages.conversation_id` is a soft link;
  `message_parts.thread_id` is an FK to `threads` *within* the shard —
  recreate per-shard, not cross-shard.
- ID generation is app-side UUIDv4 (String(36)) — no shared sequences to
  shard.

## Migration path (when triggered)

1. **Cold start**: create shard `N` databases; replicate the L1 schema
   (Alembic head) with `SERIALIZABLE`-safe constraints.
2. **Backfill**: tenant-ordered copy of the tables above from primary →
   shards, batched by `(tenant_id, created_at, seq)` cursor, checksummed
   per batch; the tenant's RYW window pins its reads to the primary during
   its copy window (existing mechanism).
3. **Cutover per tenant**: flip the shard resolver entry
   `tenant_id → shard N` behind a canary percentage; cutover is per-tenant,
   reversible by flipping back (source of truth remains the resolver).
4. **Verify + drain**: per-tenant row-count + max-seq reconciliation before
   and after; keep the primary copy for one retention window as the
   rollback path.
5. **Retire**: drop copied tenants from the primary in
   `delete_by_end_user`-style batch sweeps (existing DSR pattern) once the
   retention window lapses.

Append atomicity (idempotent `request_id` dedupe, `(thread_id, seq)`
uniqueness, outbox-in-transaction) is preserved per shard: the unit of
atomicity is the tenant, and the resolver guarantees a tenant's writes
never span shards mid-cutover.

## Acceptance

Design reviewed (this doc), L1 schema shard-compatible (P8-2 audit +
migration 0010). No code ships until a trigger above holds.
