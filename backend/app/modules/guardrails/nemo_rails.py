import asyncio
import structlog
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ...domain.safety import SafetyCheckResult, SafetyViolation, ViolationSeverity, SafetyCategory
from ...domain.tenant import TenantConfig
from ...infrastructure.patterns import ManagedService
from .types import GuardrailLayer
from .errors import GuardrailEngineError

logger = structlog.get_logger(__name__)


@dataclass
class NeMoRailConfig:
    config_path: str
    colang_version: str = "2.x"
    enable_dialog_rails: bool = True
    enable_topic_rails: bool = True
    enable_safety_rails: bool = True

    def __post_init__(self) -> None:
        if not self.config_path:
            raise ValueError("NeMoRailConfig.config_path is required")


class NeMoGuardrailsEngine(ManagedService):
    layer = GuardrailLayer.NEMO_RAILS

    def __init__(self, config: NeMoRailConfig, generation_timeout: float = 30.0):
        super().__init__("nemo_guardrails")
        self.config = config
        self.generation_timeout = generation_timeout
        self._rails_instance = None

    async def _do_initialize(self) -> None:
        try:
            from nemoguardrails import RailsConfig, LLMRails
            loop = asyncio.get_event_loop()
            rails_config = await loop.run_in_executor(None, lambda: RailsConfig.from_path(self.config.config_path))
            self._rails_instance = LLMRails(config=rails_config)
            logger.info("nemo_rails_initialized", config_path=self.config.config_path)
        except ImportError as e:
            logger.warning("nemo_rails_import_error", error=str(e))
            self._rails_instance = None
        except Exception as e:
            raise GuardrailEngineError(self.layer.name, f"Failed to initialize NeMo Guardrails: {e}", cause=e) from e

    async def _do_close(self) -> None:
        self._rails_instance = None

    async def evaluate(
        self,
        text: str,
        tenant_config: TenantConfig,
        context: Optional[Dict[str, Any]] = None,
    ) -> SafetyCheckResult:
        """Evaluate input through NeMo rails.

        Fail-closed: if the rails instance was not initialized this engine
        reports a violation instead of silently passing (a "pass-through"
        here would claim enforcement that does not exist).
        """
        if not self._rails_instance:
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.HARMFUL_CONTENT,
                    severity=ViolationSeverity.HIGH,
                    description=(
                        "NeMo Guardrails enabled but not initialized "
                        f"(config: {self.config.config_path})"
                    ),
                    metadata={"layer": self.layer.name, "reason": "not_initialized"},
                )],
                confidence=0.0,
                metadata={"layer": self.layer.name, "rails_applied": False, "reason": "not_initialized"},
            )

        try:
            messages = [
                {"type": "user", "content": text},
            ]
            valid, errors = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self._rails_instance.validate(messages=messages, context=context or {}),
                ),
                timeout=self.generation_timeout,
            )
            if valid:
                return SafetyCheckResult(
                    passed=True, violations=[], confidence=1.0,
                    metadata={"layer": self.layer.name, "rails_applied": True},
                )
            violations = [
                SafetyViolation(
                    category=SafetyCategory.HARMFUL_CONTENT,
                    severity=ViolationSeverity.HIGH,
                    description=str(error),
                    metadata={"layer": self.layer.name, "rails_applied": True},
                )
                for error in (errors or [])
            ]
            return SafetyCheckResult(
                passed=False, violations=violations, confidence=0.0,
                metadata={"layer": self.layer.name, "rails_applied": True},
            )
        except asyncio.TimeoutError:
            logger.error("nemo_rails_timeout", timeout=self.generation_timeout)
            raise GuardrailEngineError(
                self.layer.name,
                f"NeMo Guardrails timed out after {self.generation_timeout}s",
            ) from None
        except GuardrailEngineError:
            raise
        except Exception as e:
            logger.error("nemo_rails_error", error=str(e))
            raise GuardrailEngineError(self.layer.name, f"NeMo Guardrails error: {e}", cause=e) from e

    async def generate_response(
        self,
        user_message: str,
        conversation_history: List[Dict[str, str]],
        context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        if not self._rails_instance:
            return user_message, {"rails_applied": False}

        try:
            messages = [{"type": msg.get("role", "user"), "content": msg.get("content", "")} for msg in conversation_history]
            messages.append({"type": "user", "content": user_message})

            result = await asyncio.wait_for(
                self._rails_instance.generate_async(messages=messages, context=context or {}),
                timeout=self.generation_timeout,
            )

            if isinstance(result, dict):
                response_text = result.get("content", "")
                metadata = result.get("metadata", {})
            else:
                response_text = str(result)
                metadata = {}

            metadata["rails_applied"] = True
            metadata["colang_version"] = self.config.colang_version
            return response_text, metadata
        except asyncio.TimeoutError:
            logger.error("nemo_rails_timeout", timeout=self.generation_timeout)
            return user_message, {"rails_applied": False, "error": "timeout"}
        except Exception as e:
            logger.error("nemo_rails_error", error=str(e))
            return user_message, {"rails_applied": False, "error": str(e)}

    async def _do_health_check(self) -> Dict[str, Any]:
        return {
            "initialized": self._rails_instance is not None,
            "config_path": self.config.config_path,
            "dialog_rails": self.config.enable_dialog_rails,
            "topic_rails": self.config.enable_topic_rails,
            "safety_rails": self.config.enable_safety_rails,
        }
