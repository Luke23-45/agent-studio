from typing import Any, Dict, List, Optional, Protocol, Tuple

from ...domain.safety import SafetyCheckResult
from ...domain.tenant import TenantConfig
from .types import GuardrailLayer


class GuardrailEngine(Protocol):
    layer: GuardrailLayer

    async def initialize(self) -> None:
        ...

    async def evaluate(self, text: str, tenant_config: TenantConfig, context: Optional[Dict[str, Any]] = None) -> SafetyCheckResult:
        ...

    async def health_check(self) -> Dict[str, Any]:
        ...

    async def close(self) -> None:
        ...


class SpotlightEngine(Protocol):
    async def apply_spotlighting(self, retrieved_content: str, source_metadata: Optional[Dict[str, Any]] = None) -> str:
        ...

    def create_spotlight_template(self, user_query: str, retrieved_chunks: List[Tuple[str, Dict[str, Any]]]) -> str:
        ...
