#!/usr/bin/env bash
# P6-9 — Offsite logical backup guard (independent of RDS snapshots).
# Daily 04:00 UTC via cron/scheduler: pg_dump -> encrypt -> S3 (KMS).
#
# Usage:
#   ./ops/scripts/backup_postgres.sh \
#       --host <endpoint> --db neryva --user neryva \
#       --bucket neryva-staging-archives --region eu-west-1
set -euo pipefail

HOST="" DB="neryva" USER="neryva" BUCKET="" REGION="eu-west-1"
while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --db) DB="$2"; shift 2 ;;
    --user) USER="$2"; shift 2 ;;
    --bucket) BUCKET="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
[ -n "$HOST" ] && [ -n "$BUCKET" ] || { echo "--host and --bucket required" >&2; exit 2; }

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
DUMP="/tmp/neryva-${STAMP}.dump"

echo "Logical dump -> $DUMP"
PGPASSWORD="${DB_PASSWORD:-$(aws ssm get-parameter --name /neryva-${ENV:-staging}/db/password --with-decryption --query Parameter.Value --output text --region "$REGION")}" \
  pg_dump --host "$HOST" --dbname "$DB" --username "$USER" \
  --format=custom --no-owner --file "$DUMP"

echo "Upload (KMS-encrypted at rest) -> s3://$BUCKET/logical/$STAMP.dump"
aws s3 cp "$DUMP" "s3://$BUCKET/logical/$STAMP.dump" \
  --sse aws:kms --region "$REGION"

echo "Prune logical dumps older than 30 days"
aws s3 rm --recursive "s3://$BUCKET/logical/" \
  --exclude "*" --include "*.dump" \
  --region "$REGION" --only-show-errors 2>/dev/null || true
aws s3 ls "s3://$BUCKET/logical/" --region "$REGION" | while read -r line; do
  date=$(echo "$line" | awk '{print $1}')
  if [ -n "$date" ] && [ "$(date -d "$date" +%s)" -lt "$(( $(date +%s) - 30*86400 ))" ]; then
    key=$(echo "$line" | awk '{print $4}')
    [ -n "$key" ] && aws s3 rm "s3://$BUCKET/logical/$key" --region "$REGION"
  fi
done

rm -f "$DUMP"
echo "Logical backup complete: $STAMP"
