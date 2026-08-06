"""
Tenant configuration module.

Manages multi-tenant setup, policy compilation, and versioning.
"""

from .service import (
    TenantConfigService,
    configure_tenant_config_service,
    create_tenant_config_service,
    get_tenant_config_service,
    policy_set_from_db,
    tenant_config_from_data,
)

__all__ = [
    "TenantConfigService",
    "configure_tenant_config_service",
    "create_tenant_config_service",
    "get_tenant_config_service",
    "policy_set_from_db",
    "tenant_config_from_data",
]