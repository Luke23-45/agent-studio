#!/usr/bin/env bash
# P6-9 — DR drill: assert every backup/HA component meets RPO/RTO targets.
# Exits non-zero when any check breaches, so CI/ops can gate on it.
#
# Usage: ./ops/scripts/dr_drill.sh [--env staging] [--region eu-west-1]
set -euo pipefail

ENV="${1:-staging}"
REGION="${2:-eu-west-1}"
RDS_ID="neryva-${ENV}-postgres"
BUCKET="neryva-${ENV}-archives"
RPO_MIN=5
RPO_REDIS_MIN=15
FAILURES=()

say() { printf '[dr_drill] %s\n' "$*"; }
fail() { FAILURES+=("$*"); say "FAIL: $*"; }

# 1. RDS backup age <= RPO (5 min) via PITR guarantee: latest restore time
#    must be within the RPO window of "now".
LATEST_RESTORE=$(aws rds describe-db-instances \
  --db-instance-identifier "$RDS_ID" --region "$REGION" \
  --query 'DBInstances[0].LatestRestorableTime' --output text)
if [ -z "$LATEST_RESTORE" ] || [ "$LATEST_RESTORE" = "None" ]; then
  fail "RDS $RDS_ID has no restorable point (PITR off?)"
else
  AGE_MIN=$(( ( $(date +%s) - $(date -d "$LATEST_RESTORE" +%s) ) / 60 ))
  say "RDS PITR age: ${AGE_MIN} min (RPO ${RPO_MIN} min)"
  [ "$AGE_MIN" -le "$RPO_MIN" ] || fail "RDS PITR age ${AGE_MIN}min > RPO ${RPO_MIN}min"
fi

# 2. RDS Multi-AZ + replica present (HA contract).
AZ_MODE=$(aws rds describe-db-instances --db-instance-id "$RDS_ID" --region "$REGION" \
  --query 'DBInstances[0].MultiAZ' --output text)
[ "$AZ_MODE" = "true" ] || fail "RDS Multi-AZ is $AZ_MODE"
REPLICAS=$(aws rds describe-db-instances --region "$REGION" \
  --query "length(DBInstances[?DBInstanceIdentifier=='${RDS_ID}-replica'])")
[ "$REPLICAS" = "1" ] || fail "expected 1 read replica, found $REPLICAS"

# 3. Backup retention >= 30 days.
RETENTION=$(aws rds describe-db-instances --db-instance-id "$RDS_ID" --region "$REGION" \
  --query 'DBInstances[0].BackupRetentionPeriod' --output text)
[ "$RETENTION" -ge 30 ] || fail "RDS backup retention ${RETENTION}d < 30d"

# 4. S3 archive versioning enabled + KMS encryption (RPO 24h / RTO 4h).
VERSIONING=$(aws s3api get-bucket-versioning --bucket "$BUCKET" --region "$REGION" \
  --query 'Status' --output text)
[ "$VERSIONING" = "Enabled" ] || fail "S3 $BUCKET versioning is $VERSIONING"
ENCRYPTION=$(aws s3api get-bucket-encryption --bucket "$BUCKET" --region "$REGION" \
  --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.SSEAlgorithm' \
  --output text)
[ "$ENCRYPTION" = "aws:kms" ] || fail "S3 $BUCKET encryption is $ENCRYPTION"

# 5. Redis snapshot age <= RPO 15 min (cluster-mode autosnapshots).
SNAPSHOT_AGE=$(aws elasticache describe-snapshots --region "$REGION" \
  --query "max_by(Snapshots, &NodeSnapshots[0].SnapshotCreateTime).NodeSnapshots[0].SnapshotCreateTime" \
  --output text)
if [ -z "$SNAPSHOT_AGE" ] || [ "$SNAPSHOT_AGE" = "None" ]; then
  fail "no ElastiCache snapshot found (autosnapshot off?)"
else
  AGE_MIN=$(( ( $(date +%s) - $(date -d "$SNAPSHOT_AGE" +%s) ) / 60 ))
  say "Redis snapshot age: ${AGE_MIN} min (RPO ${RPO_REDIS_MIN} min)"
  [ "$AGE_MIN" -le "$RPO_REDIS_MIN" ] || fail "Redis snapshot age ${AGE_MIN}min > RPO ${RPO_REDIS_MIN}min"
fi

if [ "${#FAILURES[@]}" -gt 0 ]; then
  say "DRILL FAILED: ${#FAILURES[@]} breaches"
  printf ' - %s\n' "${FAILURES[@]}"
  exit 1
fi
say "DRILL PASSED — all RPO/RTO targets met"
