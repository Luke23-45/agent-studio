# Secrets management (P6-9, ties P0-8)

## Architecture

- **Master key** (provider-secret encryption, P0-8 `kms_ref` design):
  AWS KMS key `alias/neryva-<env>-master`, created by
  `ops/terraform/main.tf`. `backend/app/infrastructure/keys/crypto.py`
  reads the key from `NERYVA_MASTER_KEY` (dev fallback file only in local
  dev; production injects the KMS-decrypted material via env).
- **At-rest encryption**: RDS/ElastiCache/S3 are encrypted with
  `alias/neryva-<env>-backups` (separate key so a compromise of the backup
  path cannot decrypt provider secrets).
- **DB credentials**: generated per environment
  (`random_password`), stored in SSM SecureString
  (`/neryva-<env>/db/username|password`) encrypted with the master key,
  and injected into the API/worker at deploy time via `DATABASE_URL`.
- **Provider API keys** (OpenAI/Anthropic/Gemini): never stored in the
  repo or the database in plaintext. Injected at deploy time as env vars
  (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`) or as
  encrypted tenant records via `KeyVaultService.store` (encrypted with the
  master key; `kms_ref` records the KMS key id for auditability).

## Where things live

| Secret | Store | Refreshed |
|---|---|---|
| DB password | SSM SecureString `/neryva-<env>/db/password` | rotate quarterly (RDS password rotation) |
| Provider keys (platform) | env / Secret Manager on staging | on rotation |
| Provider keys (per tenant) | `provider_credentials` rows, Fernet(master key) + `kms_ref` | on tenant rotation |
| CI eval keys | GitHub secrets `EVAL_*_API_KEY` | manual |
| Staging deploy | GitHub secrets `STAGING_HOST/SSH_KEY`, `GHCR_TOKEN` | manual |
| JWT/session signing | `JWT_SECRET` env / SSM | rotate on rotation window |

## Rotation runbooks

1. **DB password**: `aws rds rotate-db-instance` (or manual) → update SSM →
   redeploy API/worker → old sessions survive (session tokens are
   Postgres-backed and JWT-signed independently).
2. **Master key**: KMS keys rotate automatically (`enable_key_rotation`).
   Because provider ciphertext is Fernet-encrypted with the injected key
   material, rotate by re-injecting a fresh `NERYVA_MASTER_KEY` and
   re-encrypting stored tenant credentials (`KeyVaultService.reencrypt`).
3. **Provider key leak**: rotate at the provider, delete the credential row
   (`KeyVaultService.delete`), and audit the trail (P5-9).

## Verification

- `dr_drill.sh` asserts encryption (KMS) on S3/RDS paths.
- `test_phase5_lifecycle.py` + `test_operator_auth_mfa.py` cover key
  vault CRUD and operator access control.
- No secrets in git: `git secrets` / pre-commit hooks are wired via CI
  (osv-scanner + pip-audit + Trivy on the built images).
