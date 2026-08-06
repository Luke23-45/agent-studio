"""
Tenant configuration service.

Manages multi-tenant configuration, policy compilation, and versioning.
"""

import json
import structlog
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from backend.app.domain.policy import PolicyRule, PolicySet, PolicyType, PolicyAction
from backend.app.domain.tenant import TenantConfig

logger = structlog.get_logger(__name__)


class TenantConfigService:
    """Service for managing tenant configurations."""

    def __init__(self, config_storage_path: Path | None = None):
        self.config_storage_path = config_storage_path or Path("./tenant_configs")
        self._config_cache: dict[UUID, TenantConfig] = {}
        self._policy_cache: dict[UUID, PolicySet] = {}

    def create_tenant(
        self,
        name: str,
        slug: str,
        allowed_topics: list[str] | None = None,
        blocked_topics: list[str] | None = None,
        escalation_threshold: float = 0.7,
    ) -> TenantConfig:
        """Create a new tenant configuration."""
        config = TenantConfig(
            name=name,
            slug=slug,
            allowed_topics=allowed_topics or [],
            blocked_topics=blocked_topics or [],
            escalation_threshold=escalation_threshold,
        )

        self._config_cache[config.id] = config
        logger.info("tenant_created", tenant_id=config.id, slug=slug)
        return config

    def save_config(self, config: TenantConfig) -> None:
        """Save tenant configuration to storage."""
        if self.config_storage_path:
            self.config_storage_path.mkdir(parents=True, exist_ok=True)
            config_file = self.config_storage_path / f"{config.slug}.json"

            config_data = {
                "id": str(config.id),
                "name": config.name,
                "slug": config.slug,
                "allowed_topics": config.allowed_topics,
                "blocked_topics": config.blocked_topics,
                "escalation_threshold": config.escalation_threshold,
                "knowledge_allowlist": config.knowledge_allowlist,
                "default_provider": config.default_provider,
                "default_model": config.default_model,
                "features": config.features,
                "guardrail_config": config.guardrail_config,
                "guardrail_thresholds": config.guardrail_thresholds,
            }

            with open(config_file, "w") as f:
                json.dump(config_data, f, indent=2)

        logger.info("tenant_config_saved", tenant_id=config.id)

    def load_config(self, tenant_id: UUID) -> TenantConfig | None:
        """Load tenant configuration from storage or cache."""
        if tenant_id in self._config_cache:
            return self._config_cache[tenant_id]

        if self.config_storage_path:
            # Try to find config by ID
            for config_file in self.config_storage_path.glob("*.json"):
                try:
                    with open(config_file) as f:
                        data = json.load(f)
                    if data.get("id") == str(tenant_id):
                        config = self._config_from_data(data)
                        self._config_cache[tenant_id] = config
                        return config
                except Exception as e:
                    logger.error("config_load_error", file=str(config_file), error=str(e))

        return None

    def load_config_by_slug(self, slug: str) -> TenantConfig | None:
        """Load tenant configuration by slug."""
        for cached in self._config_cache.values():
            if cached.slug == slug:
                return cached

        if self.config_storage_path:
            config_file = self.config_storage_path / f"{slug}.json"
            if config_file.exists():
                try:
                    with open(config_file) as f:
                        data = json.load(f)
                    config = self._config_from_data(data)
                    self._config_cache[config.id] = config
                    return config
                except Exception as e:
                    logger.error("config_load_error", file=str(config_file), error=str(e))
        return None

    def invalidate_cache(self, tenant_id: UUID) -> None:
        """Drop cached config/policy for a tenant (called on publish/rollback)."""
        self._config_cache.pop(tenant_id, None)
        self._policy_cache.pop(tenant_id, None)
        logger.info("tenant_config_cache_invalidated", tenant_id=tenant_id)

    @staticmethod
    def _config_from_data(data: dict[str, Any]) -> TenantConfig:
        """Rehydrate a TenantConfig from stored JSON data."""
        return tenant_config_from_data(data)

    def create_policy_set(
        self,
        tenant_id: UUID,
        name: str,
        rules: list[dict[str, Any]] | None = None,
    ) -> PolicySet:
        """Create a policy set for a tenant."""
        policy_rules = []
        if rules:
            for rule_data in rules:
                rule = PolicyRule(
                    name=rule_data.get("name", ""),
                    policy_type=PolicyType(rule_data.get("policy_type", "topic_filter")),
                    action=PolicyAction(rule_data.get("action", "allow")),
                    conditions=rule_data.get("conditions", {}),
                    priority=rule_data.get("priority", 0),
                )
                policy_rules.append(rule)

        policy_set = PolicySet(
            tenant_id=tenant_id,
            name=name,
            rules=policy_rules,
        )

        self._policy_cache[tenant_id] = policy_set
        logger.info("policy_set_created", tenant_id=tenant_id, name=name)
        return policy_set

    def get_policy_set(self, tenant_id: UUID) -> PolicySet | None:
        """Get policy set for a tenant."""
        if tenant_id in self._policy_cache:
            return self._policy_cache[tenant_id]

        # Could load from storage if implemented
        return None

    def update_tenant_features(
        self,
        tenant_id: UUID,
        features: dict[str, bool],
    ) -> TenantConfig | None:
        """Update feature flags for a tenant."""
        config = self._config_cache.get(tenant_id)
        if not config:
            config = self.load_config(tenant_id)

        if config:
            config.features.update(features)
            self.save_config(config)
            logger.info("tenant_features_updated", tenant_id=tenant_id)
            return config

        return None

    def delete_tenant(self, tenant_id: UUID) -> bool:
        """Delete a tenant configuration."""
        config = self._config_cache.get(tenant_id)
        if not config:
            config = self.load_config(tenant_id)

        if config and self.config_storage_path:
            config_file = self.config_storage_path / f"{config.slug}.json"
            if config_file.exists():
                config_file.unlink()

        self._config_cache.pop(tenant_id, None)
        self._policy_cache.pop(tenant_id, None)
        logger.info("tenant_deleted", tenant_id=tenant_id)
        return True


# Singleton instance
_tenant_config_service: TenantConfigService | None = None


def get_tenant_config_service(
    storage_path: Path | None = None,
) -> TenantConfigService:
    """Get or create tenant config service instance."""
    global _tenant_config_service
    if _tenant_config_service is None:
        _tenant_config_service = TenantConfigService(storage_path)
    return _tenant_config_service


def configure_tenant_config_service(storage_path: Path | None = None) -> TenantConfigService:
    """(Re)configure the tenant config service singleton, e.g. from app lifespan."""
    global _tenant_config_service
    _tenant_config_service = TenantConfigService(storage_path)
    return _tenant_config_service


def create_tenant_config_service(storage_path: Path | None = None) -> TenantConfigService:
    """Factory function to create tenant config service."""
    return TenantConfigService(storage_path)


def tenant_config_from_data(data: dict[str, Any]) -> TenantConfig:
    """Rehydrate a TenantConfig from a stored mapping (DB row or JSON file).

    Defaults are merged so that tenants created before new guardrail keys
    existed still evaluate with sane, explicit configuration.
    """
    default_guardrails = {
        "regex_fastpath": True,
        "classifier": True,
        "nemo_rails": False,
        "jailbreak_detection": True,
        "output_validation": True,
        "pii_detection": True,
        "spotlighting": True,
    }
    default_budgets = {
        "max_redact_iterations": 2.0,
        "max_graph_steps": 50.0,
        "max_duration_s": 60.0,
    }
    default_tool_clearing = {
        "keep_recent_turns": 2.0,
    }
    return TenantConfig(
        id=UUID(data["id"]),
        name=data["name"],
        slug=data["slug"],
        allowed_topics=data.get("allowed_topics", []),
        blocked_topics=data.get("blocked_topics", []),
        escalation_threshold=data.get("escalation_threshold", 0.7),
        knowledge_allowlist=data.get("knowledge_allowlist", []),
        default_provider=data.get("default_provider", "openai"),
        default_model=data.get("default_model", "gpt-4"),
        features=data.get("features", {}),
        guardrail_config={**default_guardrails, **data.get("guardrail_config", {})},
        guardrail_thresholds={
            "classifier": 0.5,
            "jailbreak": 0.7,
            "pii": 0.5,
            **data.get("guardrail_thresholds", {}),
        },
        budgets={**default_budgets, **data.get("budgets", {})},
        tool_clearing={**default_tool_clearing, **data.get("tool_clearing", {})},
    )


def policy_set_from_db(row: dict[str, Any]) -> PolicySet:
    """Rehydrate a domain PolicySet from a PolicyRepository row (rules included).

    The DB is the source of truth for tenant policies; the request path must
    never evaluate against an empty in-memory set after a restart.
    """
    rules: list[PolicyRule] = []
    for rule_data in row.get("rules") or []:
        rules.append(
            PolicyRule(
                name=rule_data.get("name", ""),
                policy_type=PolicyType(rule_data.get("policy_type", "topic_filter")),
                action=PolicyAction(rule_data.get("action", "allow")),
                conditions=rule_data.get("conditions", {}),
                priority=rule_data.get("priority", 0),
            )
        )
    return PolicySet(
        id=UUID(row["id"]),
        tenant_id=UUID(row["tenant_id"]),
        name=row.get("name", "default"),
        rules=rules,
        version=row.get("version", 1),
    )
