import asyncio
import hashlib
import inspect
import structlog
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from uuid import UUID

from ...domain.safety import SafetyCheckResult, SafetyViolation, ViolationSeverity, SafetyCategory
from ...domain.tenant import TenantConfig
from ...infrastructure.patterns import (
    CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpenError,
    RateLimiter, RateLimitConfig, RateLimitExceeded,
    HealthCheckable, HealthComponent, HealthStatus, HealthReport,
    ManagedService,
)
from .types import GuardrailLayer, GuardrailDecision
from .models import GuardrailConfig, GuardrailEvaluationResult
from .config import GuardrailsModuleConfig, build_config_from_tenant
from .errors import GuardrailError, GuardrailConfigurationError, GuardrailTimeoutError
from .metrics import GuardrailMetrics
from .interfaces import GuardrailEngine
from .regex_fastpath import RegexFastpathEngine
from .classifier import ClassifierLayer
from .nemo_rails import NeMoGuardrailsEngine, NeMoRailConfig
from .jailbreak import JailbreakDetector
from .guardrails_ai import GuardrailsAIEngine
from .pii_engine import PIIEnhancementEngine
from .spotlighting import SpotlightingEngine

logger = structlog.get_logger(__name__)


class GuardrailsOrchestrator(ManagedService):
    def __init__(self, config: GuardrailsModuleConfig):
        super().__init__("guardrails_orchestrator")
        self.config = config
        self.config.validate()
        self._engines: Dict[GuardrailLayer, GuardrailEngine] = {}
        self._circuit_breakers: Dict[str, CircuitBreaker] = {}
        self._rate_limiter = RateLimiter(RateLimitConfig(
            max_requests=config.rate_limit_max_requests,
            window_seconds=config.rate_limit_window_seconds,
        ))
        self._metrics = GuardrailMetrics()
        self._spotlight = SpotlightingEngine() if config.enable_spotlighting else None
        self._layer_order: List[GuardrailLayer] = []
        # Optional sink for audit evidence records (matrix item 4.14).
        # Wired per-request by callers; may be sync or async. Records are
        # plain dicts. Awaited so evidence is not lost on process exit.
        self.evidence_callback: Optional[Callable[[Dict[str, Any]], Union[None, Any]]] = None

    async def _do_initialize(self) -> None:
        engines: List[Tuple[GuardrailLayer, Optional[GuardrailEngine]]] = [
            (GuardrailLayer.REGEX_FASTPATH, RegexFastpathEngine() if self.config.enable_regex_fastpath else None),
            (GuardrailLayer.CLASSIFIER, ClassifierLayer(threshold=self.config.classifier_threshold) if self.config.enable_classifier else None),
            (GuardrailLayer.JAILBREAK_SCAN, JailbreakDetector(heuristic_threshold=self.config.jailbreak_threshold) if self.config.enable_jailbreak_scan else None),
            (GuardrailLayer.GUARDRAILS_AI, GuardrailsAIEngine() if self.config.enable_guardrails_ai else None),
            (GuardrailLayer.PII_REDACTION, PIIEnhancementEngine(score_threshold=self.config.pii_threshold) if self.config.enable_pii_redaction else None),
        ]
        if self.config.enable_nemo_rails and self.config.nemo_config_path:
            nemo_config = NeMoRailConfig(config_path=self.config.nemo_config_path)
            engines.append((GuardrailLayer.NEMO_RAILS, NeMoGuardrailsEngine(nemo_config)))

        init_tasks = []
        for layer, engine in engines:
            if engine is not None:
                self._engines[layer] = engine
                init_tasks.append(engine.initialize())
                cb_config = CircuitBreakerConfig(
                    name=layer.name,
                    failure_threshold=self.config.circuit_breaker_failure_threshold,
                    recovery_timeout=self.config.circuit_breaker_recovery_timeout,
                )
                self._circuit_breakers[layer.name] = CircuitBreaker(cb_config)

        if init_tasks:
            results = await asyncio.gather(*init_tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    layer_name = list(self._engines.keys())[i].name
                    logger.error("engine_init_failed", layer=layer_name, error=str(result))
                    if not self.config.fail_open_on_error:
                        raise

        self._layer_order = [
            GuardrailLayer.REGEX_FASTPATH,
            GuardrailLayer.CLASSIFIER,
            GuardrailLayer.JAILBREAK_SCAN,
            GuardrailLayer.NEMO_RAILS,
        ]
        logger.info("guardrails_orchestrator_initialized", engines=list(self._engines.keys()))

    async def _do_close(self) -> None:
        for engine in self._engines.values():
            await engine.close()
        self._engines.clear()

    async def evaluate_input(
        self,
        user_input: str,
        tenant_config: TenantConfig,
        conversation_history: Optional[List[Dict[str, str]]] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> GuardrailEvaluationResult:
        if not user_input:
            return GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW)

        try:
            await self._rate_limiter.acquire_or_raise(str(tenant_config.id))
        except RateLimitExceeded as e:
            logger.warning("rate_limit_exceeded", tenant=tenant_config.id)
            result = GuardrailEvaluationResult(
                allowed=False,
                decision=GuardrailDecision.BLOCK,
                violations=[SafetyViolation(
                    category=SafetyCategory.HARMFUL_CONTENT,
                    severity=ViolationSeverity.HIGH,
                    description=f"Rate limit exceeded: {e}",
                )],
            )
            self._emit_evidence("input", user_input, result, tenant_config, conversation_id, session_id)
            return result

        self._metrics.mark_active_request(1)
        try:
            result = await self._run_input_pipeline(user_input, tenant_config)
            await self._emit_evidence("input", user_input, result, tenant_config, conversation_id, session_id)
            return result
        finally:
            self._metrics.mark_active_request(-1)
    async def _run_input_pipeline(self, user_input: str, tenant_config: TenantConfig) -> GuardrailEvaluationResult:
        start_time = asyncio.get_event_loop().time()
        result = GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW)
        violations: List[SafetyViolation] = []
        layer_results: Dict[GuardrailLayer, SafetyCheckResult] = {}

        if self.config.parallel_layer_execution:
            tasks = []
            for layer in self._layer_order:
                if layer in self._engines:
                    tasks.append(self._evaluate_layer_safe(layer, user_input, tenant_config))
            if tasks:
                completed = await asyncio.gather(*tasks, return_exceptions=True)
                for outcome in completed:
                    if isinstance(outcome, GuardrailEvaluationResult):
                        violations.extend(outcome.violations)
                        layer_results.update(outcome.layer_results)
                    elif isinstance(outcome, Exception):
                        if not self.config.fail_open_on_error:
                            raise
                        logger.error("layer_failed", error=str(outcome))
        else:
            for layer in self._layer_order:
                if layer in self._engines:
                    outcome = await self._evaluate_layer_safe(layer, user_input, tenant_config)
                    if isinstance(outcome, GuardrailEvaluationResult):
                        violations.extend(outcome.violations)
                        layer_results.update(outcome.layer_results)
                        if not outcome.allowed and outcome.decision == GuardrailDecision.BLOCK:
                            break
                    elif isinstance(outcome, Exception):
                        if not self.config.fail_open_on_error:
                            raise

        elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        result.allowed = len(violations) == 0
        result.violations = violations
        result.layer_results = layer_results
        result.processing_time_ms = elapsed_ms

        if not result.allowed:
            has_high = any(v.severity in (ViolationSeverity.HIGH, ViolationSeverity.CRITICAL) for v in violations)
            result.decision = GuardrailDecision.BLOCK if has_high else GuardrailDecision.REDIRECT

        result.metadata["layers_evaluated"] = [l.name for l in layer_results.keys()]
        result.metadata["total_violations"] = len(violations)
        self._metrics.record_evaluation("input", result.allowed, elapsed_ms)
        return result

    async def _evaluate_layer_safe(
        self, layer: GuardrailLayer, user_input: str, tenant_config: TenantConfig
    ) -> GuardrailEvaluationResult | Exception:
        engine = self._engines.get(layer)
        if not engine:
            return GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW)

        cb = self._circuit_breakers.get(layer.name)
        if cb and cb.state.name == "OPEN":
            logger.warning("circuit_breaker_open", layer=layer.name)
            if self.config.fail_open_on_error:
                return GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW)
            else:
                return GuardrailEvaluationResult(
                    allowed=False, decision=GuardrailDecision.BLOCK,
                    violations=[SafetyViolation(
                        category=SafetyCategory.HARMFUL_CONTENT, severity=ViolationSeverity.HIGH,
                        description=f"Guardrail layer {layer.name} unavailable",
                    )],
                )

        try:
            async def _evaluate() -> SafetyCheckResult:
                return await engine.evaluate(user_input, tenant_config)

            if cb:
                check = await cb.call(_evaluate)
            else:
                check = await asyncio.wait_for(
                    _evaluate(), timeout=self.config.layer_timeout_seconds
                )

            if check.passed:
                return GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW, layer_results={layer: check})
            else:
                return GuardrailEvaluationResult(
                    allowed=False,
                    decision=GuardrailDecision.BLOCK,
                    layer_results={layer: check},
                    violations=check.violations,
                )
        except asyncio.TimeoutError:
            msg = f"Layer {layer.name} timed out"
            logger.error(msg, timeout=self.config.layer_timeout_seconds)
            if self.config.fail_open_on_error:
                return GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW)
            return GuardrailEvaluationResult(
                allowed=False, decision=GuardrailDecision.BLOCK,
                violations=[SafetyViolation(category=SafetyCategory.HARMFUL_CONTENT, severity=ViolationSeverity.HIGH, description=msg)],
            )
        except CircuitBreakerOpenError as e:
            logger.error("circuit_breaker_intercepted", layer=layer.name, error=str(e))
            if self.config.fail_open_on_error:
                return GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW)
            return GuardrailEvaluationResult(
                allowed=False, decision=GuardrailDecision.BLOCK,
                violations=[SafetyViolation(category=SafetyCategory.HARMFUL_CONTENT, severity=ViolationSeverity.HIGH, description=str(e))],
            )
        except Exception as e:
            logger.error("layer_evaluation_error", layer=layer.name, error=str(e))
            if self.config.fail_open_on_error:
                return GuardrailEvaluationResult(allowed=True, decision=GuardrailDecision.ALLOW)
            raise

    async def evaluate_output(
        self,
        model_output: str,
        schema_name: Optional[str] = None,
        tenant_config: Optional[TenantConfig] = None,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> GuardrailEvaluationResult:
        start_time = asyncio.get_event_loop().time()
        violations: List[SafetyViolation] = []
        layer_results: Dict[GuardrailLayer, SafetyCheckResult] = {}
        decision = GuardrailDecision.ALLOW

        gaai = self._engines.get(GuardrailLayer.GUARDRAILS_AI)
        if schema_name and isinstance(gaai, GuardrailsAIEngine):
            result = await gaai.validate_output(model_output, schema_name)
            layer_results[GuardrailLayer.GUARDRAILS_AI] = result
            if not result.passed:
                violations.extend(result.violations)
                decision = GuardrailDecision.REASK

        pii = self._engines.get(GuardrailLayer.PII_REDACTION)
        redacted_output = model_output
        if isinstance(pii, PIIEnhancementEngine):
            redacted_output, detections = await pii.redact_pii(model_output)
            if detections:
                layer_results[GuardrailLayer.PII_REDACTION] = SafetyCheckResult(
                    passed=True, violations=[], confidence=0.95,
                    metadata={"pii_detected": len(detections)},
                )

        elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        result = GuardrailEvaluationResult(
            allowed=decision in (GuardrailDecision.ALLOW, GuardrailDecision.REDACT),
            decision=decision,
            layer_results=layer_results,
            violations=violations,
            redacted_text=redacted_output if redacted_output != model_output else None,
            confidence_score=1.0 if not violations else 0.5,
            processing_time_ms=elapsed_ms,
            metadata={"layers_evaluated": [l.name for l in layer_results.keys()], "total_violations": len(violations)},
        )
        await self._emit_evidence("output", model_output, result, tenant_config, conversation_id, session_id)
        return result

    async def _emit_evidence(
        self,
        direction: str,
        content: str,
        result: GuardrailEvaluationResult,
        tenant_config: Optional[TenantConfig],
        conversation_id: Optional[str],
        session_id: Optional[str],
    ) -> None:
        """Persist an audit evidence packet via the configured callback (4.14)."""
        if self.evidence_callback is None:
            return
        if tenant_config is None:
            return
        record = {
            "tenant_id": str(tenant_config.id),
            "conversation_id": conversation_id,
            "session_id": session_id,
            "direction": direction,
            "decision": result.decision.name,
            "allowed": result.allowed,
            "input_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "violations": [v.to_dict() for v in result.violations],
            "layers_evaluated": result.metadata.get("layers_evaluated", []),
            "processing_time_ms": result.processing_time_ms,
            "metadata": {
                "confidence_score": result.confidence_score,
                "redacted": result.redacted_text is not None,
            },
        }
        try:
            outcome = self.evidence_callback(record)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("evidence_emit_failed", direction=direction, error=str(e))

    def apply_spotlighting_to_context(self, retrieved_chunks: List[Tuple[str, Dict[str, Any]]], user_query: str) -> str:
        if not self._spotlight:
            return "\n\n".join(chunk for chunk, _ in retrieved_chunks) + f"\n\nQuery: {user_query}"
        return self._spotlight.create_spotlight_template(user_query, retrieved_chunks)

    async def validate_input(
        self,
        user_input: str,
        tenant_config: TenantConfig,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = await self.evaluate_input(
            user_input,
            tenant_config,
            conversation_id=conversation_id,
            session_id=session_id,
        )
        return {
            "is_valid": result.allowed,
            "blocked": result.decision == GuardrailDecision.BLOCK,
            "violations": [v.to_dict() for v in result.violations],
        }

    async def _do_health_check(self) -> HealthComponent:
        components = []
        for layer, engine in self._engines.items():
            try:
                hc = await engine.health_check()
                if isinstance(hc, dict):
                    components.append(HealthComponent(
                        name=layer.name, status=HealthStatus.HEALTHY, metadata=hc
                    ))
                elif isinstance(hc, HealthComponent):
                    components.append(hc)
            except Exception as e:
                components.append(HealthComponent(
                    name=layer.name, status=HealthStatus.UNHEALTHY, message=str(e)
                ))

        for name, cb in self._circuit_breakers.items():
            components.append(HealthComponent(
                name=f"circuit_breaker.{name}",
                status=HealthStatus.HEALTHY if cb.state.name == "CLOSED" else HealthStatus.DEGRADED,
                metadata=cb.get_state_summary(),
            ))

        overall = HealthStatus.HEALTHY
        for c in components:
            if c.status == HealthStatus.UNHEALTHY:
                overall = HealthStatus.UNHEALTHY
            elif c.status == HealthStatus.DEGRADED and overall == HealthStatus.HEALTHY:
                overall = HealthStatus.DEGRADED

        return HealthComponent(
            name=self.name,
            status=overall,
            dependencies=components,
            metadata={"layers_loaded": len(self._engines), "active_requests": self._metrics._active_requests.value},
        )

    def get_metrics_snapshot(self) -> Dict[str, Any]:
        return self._metrics.snapshot_all()

    def get_circuit_breaker_states(self) -> Dict[str, str]:
        return {name: cb.state.name for name, cb in self._circuit_breakers.items()}


def create_guardrails_orchestrator(tenant_config: TenantConfig, nemo_config_path: Optional[str] = None) -> GuardrailsOrchestrator:
    config = build_config_from_tenant(tenant_config, nemo_config_path)
    return GuardrailsOrchestrator(config)


def create_guardrails_service() -> GuardrailsOrchestrator:
    return GuardrailsOrchestrator(GuardrailsModuleConfig())


# Per-tenant registry with lazy async initialization.
# Engines (classifier models, Presidio, etc.) are expensive to construct, so
# they are created once per tenant and reused across requests.
_guardrails_registry: Dict[str, GuardrailsOrchestrator] = {}
_guardrails_locks: Dict[str, asyncio.Lock] = {}


async def get_guardrails_service(
    tenant_config: TenantConfig,
    nemo_config_path: Optional[str] = None,
) -> GuardrailsOrchestrator:
    """Get (or lazily create and initialize) the guardrails orchestrator for a tenant."""
    key = str(tenant_config.id)
    existing = _guardrails_registry.get(key)
    if existing is not None:
        return existing

    lock = _guardrails_locks.setdefault(key, asyncio.Lock())
    async with lock:
        existing = _guardrails_registry.get(key)
        if existing is not None:
            return existing
        service = create_guardrails_orchestrator(tenant_config, nemo_config_path)
        await service.initialize()
        _guardrails_registry[key] = service
        logger.info("guardrails_service_ready", tenant_id=key, engines=list(service._engines.keys()))
        return service


def invalidate_guardrails_service(tenant_id: UUID) -> None:
    """Drop a tenant's orchestrator so it is rebuilt on next request."""
    key = str(tenant_id)
    service = _guardrails_registry.pop(key, None)
    _guardrails_locks.pop(key, None)
    if service is not None:
        logger.info("guardrails_service_invalidated", tenant_id=key)
