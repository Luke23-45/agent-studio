"""
P5-6 — Postgres Row-Level Security (Arch §6.3.2).

Defense-in-depth beneath scoped repositories. Enables RLS on tenant tables
and installs one policy per table. The policy is *permissive* when the
``app.tenant_id`` GUC is unset (matching today's behavior — repositories
are the primary scoping layer) and *restrictive* when it is set, so a
session pinned to one tenant cannot read other tenants' rows even with a
buggy query (Arch §6.3.2 acceptance). Super-admin ops either set the GUC or
work through the ``neryva_superadmin`` role (bypass, unprivileged app role
cannot).

SQLite (the default dev/test driver) has no RLS; enabling is a no-op there,
so the model layer and unit tests stay driver-agnostic. The migration that
applies these statements guards on the Postgres dialect only.
"""

from __future__ import annotations

from typing import Iterable, Tuple

# Tables whose rows are tenant-scoped and must carry RLS in Postgres.
TENANT_SCOPED_TABLES: Tuple[str, ...] = (
    "tenants",
    "conversations",
    "messages",
    "message_parts",
    "threads",
    "thread_events",
    "guardrail_evidence",
    "audit_events",
    "api_keys",
    "escalations",
    "policy_sets",
    "policy_rules",
    "webhook_subscriptions",
    "webhook_events",
    "webhook_deliveries",
    "event_outbox",
    "model_catalog",
    "memories",
    "surfaces",
    "end_users",
    "session_tokens",
    "tenant_config_versions",
    "tool_registry",
    "tenant_provider_keys",
    "spend_events",
    "quota_state",
    "cache_invalidation_log",
)

# GUC name: per-session current tenant, set before any query.
TENANT_GUC = "app.tenant_id"
APP_ROLE = "neryva_app"
SUPERADMIN_ROLE = "neryva_superadmin"


def rls_enable_statement(table: str) -> str:
    return f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY;'


def tenant_scope_expr(table: str) -> str:
    """SQL predicate scoping ``table`` to the session tenant.

    Most tables carry a ``tenant_id`` column. Two do not and need a mapped
    scope: ``tenants`` (the row *is* the tenant — scope by ``id``) and
    ``policy_rules`` (no ``tenant_id`` — reached through ``policy_sets``).
    """
    if table == "tenants":
        return f"id = current_setting('{TENANT_GUC}', true)"
    if table == "policy_rules":
        return (
            f"policy_set_id IN (SELECT id FROM policy_sets "
            f"WHERE tenant_id = current_setting('{TENANT_GUC}', true))"
        )
    return f"tenant_id = current_setting('{TENANT_GUC}', true)"


def rls_policy_statements(table: str) -> list[str]:
    """Create/refresh the tenant-isolation policy on one table.

    GUC ``app.tenant_id`` set → the policy only returns that tenant's rows
    (defense-in-depth: a cross-tenant query is blocked even if the query
    itself misses its tenant filter). GUC unset (no tenant pinned in this
    session — e.g. boot or platform ops) → policy is permissive, matching
    today's behavior: repositories are the primary scoping layer and already
    filter. Sessions that must be strictly tenant-bound set the GUC via
    ``set_tenant_guc_sql`` before querying.
    """
    scope = tenant_scope_expr(table)
    return [
        f'DROP POLICY IF EXISTS "tenant_isolation_{table}" ON "{table}";',
        (
            f'CREATE POLICY "tenant_isolation_{table}" ON "{table}" '
            f"USING ("
            f"current_setting('{TENANT_GUC}', true) IS NULL "
            f"OR {scope}) "
            f"WITH CHECK ("
            f"current_setting('{TENANT_GUC}', true) IS NULL "
            f"OR {scope});"
        ),
    ]


def rls_ddl(include: Iterable[str] | None = None) -> list[str]:
    """Full DDL for enabling RLS on the tenant-scoped tables."""
    tables = tuple(include or TENANT_SCOPED_TABLES)
    statements: list[str] = []
    for table in tables:
        statements.append(rls_enable_statement(table))
        statements.extend(rls_policy_statements(table))
    return statements


def rls_enforce_statement(table: str) -> str:
    """Force RLS even for the table owner (app) — per-tenant enforcement."""
    return f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY;'


def set_tenant_guc_sql(tenant_id: str) -> str:
    """SQL to pin the session to one tenant before any query runs."""
    return f"SET LOCAL {TENANT_GUC} = '{tenant_id}';"