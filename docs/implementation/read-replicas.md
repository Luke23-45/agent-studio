# Read replicas & read routing (P8-3, Arch §11, §15 L2)

## Design

The data plane serves **history reads** (thread messages, parts, events,
tails) from Postgres read replicas while the single-writer primary keeps
conversation appends, config, and spend (OpenAI's own ChatGPT pattern,
Arch 11). Writes and correctness-critical reads (thread identity, mutation
targets, RLS-bound lookups) always use the primary.

Routing contract (`backend/app/infrastructure/db/replicas.py`,
`ReplicaRouter`):

- **Eligible** → round-robin across healthy replicas.
- **Not eligible** (no replicas configured, none healthy, or read-your-writes
  hit) → primary.
- **Read-your-writes**: a key written by this process routes to the primary
  for `REPLICA_READ_YOUR_WRITES_WINDOW_SECONDS` (default 5 s). The repository
  marks `(tenant_id, conversation_id, thread_id, message_id)` on every
  write, so a thread its own request just appended to is always read back
  fresh — replica lag is never observable on your own writes.
- **Failure semantics**: a replica connection failure marks it unavailable
  and serves the primary for the request — reads never hard-fail because a
  replica is down. `/health` reports `DEGRADED` (never silent) until a
  health cycle pings it back.

Replica sessions run as `READ ONLY DEFERRABLE` transactions on Postgres
(advisory; sqlite is skipped) so replica load never competes with the
primary's write path.

## Current status (2026-08-07)

Implemented and tested (`tests/test_read_replicas.py`, full suite green):

- `ReplicaRouter` (ManagedService): one async engine per replica URL,
  `async_sessionmaker` per engine, `pool_pre_ping`, round-robin selection,
  outage marking, health metadata (`configured/healthy/unavailable`).
- RYW window (in-process, L1 approximation), bounded key table
  (`_MAX_TRACKED_WRITE_KEYS = 10_000`).
- Routing wired into `ThreadRepository` (`read_router`), covering
  `list_messages`, `read_tail`, `list_parts`, `list_events`,
  `list_tool_result_seqs`, `list_threads`; writes stay primary and mark
  their keys (`create_thread`, `append_message`, `set_summary`,
  `clear_tool_results`, `fork_thread`, `append_event`,
  `delete_by_end_user`).
- Lifespan wiring in `main.py`: `init_replica_router` with
  `primary=db` (fallback sessions land on the same manager the repository
  writes to), initialize/close, `/health` component.

### Configuration

```
REPLICA_DATABASE_URLS='["postgresql+asyncpg://user:pass@replica-1:5432/neryva", "..."]'
REPLICA_READ_YOUR_WRITES_WINDOW_SECONDS=5.0
DATABASE_POOL_SIZE=20        # primary + replica pools
DATABASE_MAX_OVERFLOW=10
DATABASE_POOL_RECYCLE=3600
DATABASE_STATEMENT_TIMEOUT_MS=30000
```

Empty `REPLICA_DATABASE_URLS` = L1 shape (primary only); enabling replicas
is a config change, never a behavior fork.

## High/low-priority pools (deployment)

The application pools are homogeneous by design; priority is enforced at
the **pgBouncer** layer so a replica-query stampede can never starve the
write path:

| Pool | Targets | Discipline |
|---|---|---|
| `primary_hp` | primary (writes, RYW reads, correctness reads) | transaction pooling, small server_reset_query cost, bounded `max_client_conn`; never shared with bulk/analytics |
| `replica_lp` | replicas (history reads) | transaction pooling, larger pool allowed, `default_pool_size` tuned for read concurrency |

Deployment rules:

1. One `replica_lp` pool per replica (or per replica group), plus one
   `primary_hp` pool — never route replica queries through the primary
   pool.
2. The app's `DATABASE_POOL_SIZE` is a per-worker ceiling; pgBouncer
   pools size the aggregate. Keep
   `pool_size × workers < pgBouncer default_pool_size` for the HP pool so
   writes never queue behind reads.
3. Timeouts: `DATABASE_STATEMENT_TIMEOUT_MS` (30 s) already bounds
   runaway replica reads; lower it on `replica_lp` if lag-sensitive.
4. Replica URLs in `REPLICA_DATABASE_URLS` point at `replica_lp` (never
   directly at Postgres) so pool semantics hold at L2.

## Cross-process RYW (L2)

The shipped window is per-API-replica (in-process): a key written by
*this* process is pinned for `window_seconds`. At L2 with multiple API
replicas, move the window to Redis (`neryva:ryw:{key} → ts`, TTL =
window): write path SETs, read path GETs before choosing a replica.
Router API stays identical (`mark_write` / `get_read_session(*keys)`) —
the swap is internal to `ReplicaRouter`.

## Ops

- Replica lag: monitor `pg_stat_replication` (`sent/replay_lag`); alert
  when replay lag exceeds the RYW window for > 60 s (lag beyond the window
  becomes observable staleness for *other* tenants' reads).
- `/health` `replica_router` component lists `unavailable` URLs and
  degrades readiness when no replica is healthy (reads still succeed via
  primary fallback — capacity, not availability, is what degrades).
- Deployment order: provision replicas (streaming from primary),
  set `REPLICA_DATABASE_URLS`, roll the API fleet; verify `/health` shows
  `healthy: N` before removing capacity from the primary.
- Reads never hard-fail on replica outage by design — there is no
  operational sequence that "turns off" history reads.

## Related

- `docs/implementation/schema_discipline.md` (P8-2) — index coverage the
  replica queries rely on.
- `docs/implementation/turn-log-sharding.md` (P8-4) — L3 evolution of the
  same data plane.
