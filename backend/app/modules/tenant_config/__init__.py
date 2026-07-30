"""
Tenant configuration module.

Manages multi-tenant setup, policy compilation, and versioning.
"""

from .service import (
    TenantConfigService,
    create_tenant_config_service,
    get_tenant_config_service,
)

__all__ = [
    "TenantConfigService",
    "create_tenant_config_service",
    "get_tenant_config_service",
]