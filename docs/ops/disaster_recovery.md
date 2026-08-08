# Disaster Recovery & Backup Runbook (P6-9)

## RPO / RTO targets

| Tier | RPO | RTO | Mechanism |
|---|---|---|---|
| Postgres (hot data, threads, audit) | ≤ 5 min | ≤ 60 min | RDS automated backups + PITR (30-day retention), Multi-AZ standby |
| Object storage (archive, exports) | ≤ 24 h | ≤ 4 h | S3 versioning + cross-region restore path |
| Redis (streams, session tokens) | ≤ 15 min | ≤ 30 min | snapshot_retention_limit=7 (15-min autosnapshots) + cluster-mode multi-AZ |
| Provider secrets (P0-8) | — | ≤ 1 h | KMS master key + SSM SecureString; re-inject via env at deploy |

## HA layout (terraform baseline, `ops/terraform`)

- RDS Postgres 18 + pgvector: Multi-AZ primary + 1 read replica (EKS-read via
  `backend/app/infrastructure/db/replicas.py`), storage + PITR encrypted with
  the backups KMS key, `deletion_protection` in production.
- ElastiCache Redis 7.1: cluster mode enabled (2 shards × 1 replica),
  automatic failover + Multi-AZ, TLS + at-rest encryption.
- API: ALB across 2 AZs → ECS/EC2 fleet; WAF rate limit 2000 req/IP/min.
- Traces/events ride on Postgres + Redis — no standalone trace store.

## Backup jobs

| Job | Cadence | Where | Restore |
|---|---|---|---|
| RDS automated snapshot + transaction logs | continuous / daily | AWS | `ops/scripts/restore_postgres.sh` (PITR) |
| Logical dump (offsite guard) | daily 04:00 UTC | S3 `*-archives/logical/` | `pg_restore` (manual) |
| Redis snapshot | every 15 min | AWS ElastiCache | `restore` on new replication group |
| Cold archive tiering | daily (worker `retention`) | S3 `*-archives` (GLACIER_IR after 90 d) | `download_file` → restore thread |

## Drills

Run `ops/scripts/dr_drill.sh` (expects AWS credentials; checks every
component against the RPO/RTO targets above and exits non-zero on breach).
Drill calendar: **quarterly** failover + restore, **monthly** backup-age
assertion, **annual** full region restore tabletop. Every drill run is an
audit event (`AuditRepository.add("dr.drill", ...)`).

### Postgres failover drill (quarterly)
1. `aws rds failover-db-instance --db-instance-identifier <env>-postgres`
2. Wait for `multi-az: primary` on the standby AZ; API must keep serving
   (read replica path via `replicas.py` absorbs the blip).
3. Record RTO; roll back by failing over again.

### PITR restore drill (quarterly)
1. `ops/scripts/restore_postgres.sh --restore-time <UTC> --instance <name>`
2. Run `verify_chain` on the audit table + a golden thread query; mismatch
   aborts the drill.

### Archive restore drill (quarterly)
1. `backend` CLI: `archive restore --tenant <id> --thread <id>` on a cold
   thread; assert hot queries see it again (covered by `test_thread_archive.py`).

## Failover runbooks

### RDS primary loss
1. RDS auto-fails to the Multi-AZ standby (≈60–120 s; clients with
   `replicas.py` read-path keep serving reads via the replica).
2. If standby is unavailable: `aws rds restore-db-instance-to-point-in-time`
   to the last 5-min window (see `restore_postgres.sh`).
3. Repoint `DATABASE_URL` (SSM `/env/db/*`) and redeploy API + worker.

### Redis cluster loss
1. If Multi-AZ failover hasn't recovered shards: create a new replication
   group from the newest snapshot (`snapshot-*-cache-...`).
2. Stream buffers are durable (chunk overflow tier in Postgres) — the API
   replays from `stream_buffer_chunks` on reconnect; hot sessions resume.
3. Session tokens live in Postgres (`session_tokens`), not Redis — no
   re-login wave.

### Region loss (annual tabletop)
1. Restore RDS from the latest cross-region-capable backup (30-day PITR).
2. Restore S3 buckets from versioning history.
3. Deploy API/worker from GHCR images (`latest` tags) into the DR region.
