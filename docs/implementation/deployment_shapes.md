# Deployment shapes & residency (P5-12, Arch §6.5, §11)

Tenant residency and deployment shape are **configuration**, never a code
fork. Every tenant is provisioned with a pinned region at onboarding, and
its archives/exports are confined to that region's storage prefix.

## Residency pinning (L1)

- `region` is selected at tenant creation (`POST /api/v1/tenants`, optional
  `region` field). Unsupported values are rejected (`422`). When omitted, the
  platform default applies (`eu-west-1`).
- Supported regions: `eu-west-1`, `us-east-1`, `us-west-2`, `ap-southeast-1`
  (`backend/app/governance/residency.py::SUPPORTED_REGIONS`).
- Archives and exports use the region-pinned prefix
  `tenant/{tenant_id}/archive/{region}/` (`residency.archive_prefix`), carried
  by the GDPR/DSR export bundles and recorded at offboarding. Onboarding
  computes the object prefix and vector namespace with the shared isolation
  scheme (`governance/isolation.py`).

## Dedicated vs shared

| Shape      | Database | Vector      | Gateway         | Control plane |
|------------|----------|-------------|-----------------|---------------|
| shared     | shared pool | shared namespace-per-tenant | shared, per-tenant quota | shared |
| dedicated  | own pool (`tenant_{id}`) | dedicated namespace (`tenant_{id}`) | dedicated, isolated quota | shared (same API) |

Opting in is a single configuration switch through the control plane, not a
deployment of a divergent code path:

```
PUT /api/v1/tenants/{tenant_id}/deployment-shape
{ "dedicated": true }
```

The response echoes the effective shape and the isolation config
(`dedicated_deployment`), built by
`residency::dedicated_deployment_config`. The data-plane boundary moves; the
architecture and control plane do not.

## Notes

- `features.dedicated_deployment` on the tenant row is the source of truth;
  `residency::deployment_shape(tenant)` derives the shape.
- Postgres row-level security (`governance/rls.py`) applies to both shapes —
  dedicated deployments additionally move the physical pool.
- Region is immutable after onboarding; changing it requires a new tenant
  (data stays in the original region per the archive-pinning contract).

## Active-active plan (P8-5, L2+, deferred)

Regional primaries with replica fan-out are **designed but deferred**
(D-5 trigger; §17). The plan, recorded so L2 has no rewrite:

- **Residency as the routing key**: a tenant's region (already pinned and
  immutable) selects its regional primary — the region field is the
  shard-0 of multi-region. No new tenant-level config is needed.
- **Single-writer per region**: one primary per region, replicas fan out
  locally (P8-3); no cross-region write-path (no global sequence, no
  distributed transactions). Cross-region traffic is read-only by design —
  a tenant always writes to its own region's primary.
- **Read-your-writes stays within the region**: the window mechanism
  (P8-3) is per-region; a tenant's reads resolve against its region.
- **Disaster recovery**: region pinning + archive prefixes (P5-12) already
  bound data to a region; the regional restore drill is a P6-9 ops item,
  not a code change.
- **Trigger (D-5)**: multi-region timing is a customer demand decision —
  no code until a customer requires residency in a second region or a
  regional DR SLA.

**Today (L1)**: residency pinning is honored and tested (P5-12 `[x]`);
active-active is this documented plan.
