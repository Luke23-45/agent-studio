#!/usr/bin/env bash
# P6-9 — Restore a RDS instance to a point in time (PITR drill / DR).
#
# Usage:
#   ./ops/scripts/restore_postgres.sh --restore-time "2026-08-07 22:30:00" \
#       --instance neryva-staging-postgres --region eu-west-1
set -euo pipefail

RESTORE_TIME=""
SOURCE=""
REGION="${REGION:-eu-west-1}"

while [ $# -gt 0 ]; do
  case "$1" in
    --restore-time) RESTORE_TIME="$2"; shift 2 ;;
    --instance) SOURCE="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -n "$RESTORE_TIME" ] || { echo "--restore-time required" >&2; exit 2; }
[ -n "$SOURCE" ] || { echo "--instance required" >&2; exit 2; }

TARGET="${SOURCE}-pitr"
echo "Restoring $SOURCE to '$RESTORE_TIME' as $TARGET (region $REGION)..."

aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier "$SOURCE" \
  --target-db-instance-identifier "$TARGET" \
  --restore-time "$RESTORE_TIME" \
  --region "$REGION" \
  --multi-az \
  --storage-type gp3 \
  --copy-tags-to-snapshot

echo "Waiting for $TARGET to become available (RTO ≤ 60 min)..."
aws rds wait db-instance-available --db-instance-identifier "$TARGET" --region "$REGION"

ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier "$TARGET" --region "$REGION" \
  --query 'DBInstances[0].Endpoint.Address' --output text)
echo "Restore ready at: $ENDPOINT"
echo "Next: repoint DATABASE_URL (SSM /<env>/db/*), redeploy API+worker,"
echo "      then run audit verify_chain + a golden thread query."
