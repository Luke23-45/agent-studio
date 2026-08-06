import asyncio
import structlog
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...domain.safety import SafetyCheckResult, SafetyViolation, ViolationSeverity, SafetyCategory
from ...domain.tenant import TenantConfig
from ...infrastructure.patterns import ManagedService
from .types import GuardrailLayer
from .errors import GuardrailEngineError

logger = structlog.get_logger(__name__)


@dataclass
class ClassifierPrediction:
    label: str
    confidence: float
    all_scores: Dict[str, float] = field(default_factory=dict)
    model_name: str = ""
    latency_ms: float = 0.0

    def is_relevant(self, threshold: float = 0.5) -> bool:
        return self.confidence >= threshold


class ClassifierLayer(ManagedService):
    layer = GuardrailLayer.CLASSIFIER

    def __init__(
        self,
        jina_model_path: Optional[str] = None,
        roberta_model_path: Optional[str] = None,
        device: str = "cpu",
        embedding_timeout: float = 10.0,
        threshold: float = 0.5,
    ):
        super().__init__("classifier_layer")
        self.jina_model_path = jina_model_path or "jinaai/jina-embeddings-v2-small-en"
        self.roberta_model_path = roberta_model_path or "sentence-transformers/stsb-roberta-base"
        self.device = device
        self.embedding_timeout = embedding_timeout
        self.threshold = threshold
        self._jina_model = None
        self._roberta_model = None

    async def _do_initialize(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            loop = asyncio.get_event_loop()
            self._jina_model = await loop.run_in_executor(
                None, lambda: SentenceTransformer(self.jina_model_path, device=self.device)
            )
            self._roberta_model = await loop.run_in_executor(
                None, lambda: SentenceTransformer(self.roberta_model_path, device=self.device)
            )
            logger.info("classifier_models_loaded", jina=self.jina_model_path, roberta=self.roberta_model_path)
        except ImportError as e:
            # Degrade instead of raising: the layer stays registered and
            # fails CLOSED at evaluation time (see evaluate()). A boot-time
            # raise here would turn every request into a 500.
            logger.warning(
                "classifier_unavailable",
                reason="import_error",
                error=str(e),
                message="Topic classifier degraded: evaluation will fail closed.",
            )
            self._jina_model = None
            self._roberta_model = None
        except Exception as e:
            logger.warning("classifier_load_error", error=str(e), jina=self.jina_model_path)
            self._jina_model = None
            self._roberta_model = None

    async def _do_close(self) -> None:
        self._jina_model = None
        self._roberta_model = None

    async def classify_topic_relevance(
        self, user_input: str, allowed_topics: List[str], tenant_config: TenantConfig
    ) -> ClassifierPrediction:
        if not allowed_topics:
            return ClassifierPrediction(label="unknown", confidence=0.5, model_name="passthrough")
        if not self._jina_model:
            return ClassifierPrediction(label="unknown", confidence=0.5, model_name="unavailable")

        start_time = asyncio.get_event_loop().time()
        try:
            loop = asyncio.get_event_loop()
            input_embedding = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._jina_model.encode([user_input], convert_to_numpy=True, normalize_embeddings=True)[0],
                ),
                timeout=self.embedding_timeout,
            )
            topic_embeddings = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._jina_model.encode(allowed_topics, convert_to_numpy=True, normalize_embeddings=True),
                ),
                timeout=self.embedding_timeout,
            )

            from sklearn.metrics.pairwise import cosine_similarity
            similarities = cosine_similarity([input_embedding], topic_embeddings)[0]
            best_idx = similarities.argmax()
            best_score = float(similarities[best_idx])
            elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000

            return ClassifierPrediction(
                label=allowed_topics[best_idx],
                confidence=best_score,
                all_scores={topic: float(score) for topic, score in zip(allowed_topics, similarities)},
                model_name="jina-embeddings-v2-small-en",
                latency_ms=elapsed_ms,
            )
        except asyncio.TimeoutError:
            logger.error("classifier_timeout", timeout=self.embedding_timeout)
            return ClassifierPrediction(label="timeout", confidence=0.0, model_name="jina-embeddings-v2-small-en")
        except Exception as e:
            logger.error("classifier_error", error=str(e))
            return ClassifierPrediction(label="error", confidence=0.0, model_name="jina-embeddings-v2-small-en")

    async def evaluate(
        self,
        text: str,
        tenant_config: TenantConfig,
        context: Optional[Dict[str, Any]] = None,
    ) -> SafetyCheckResult:
        """Evaluate topic relevance of user input against the tenant's allowed topics."""
        start_time = asyncio.get_event_loop().time()
        if not text:
            return SafetyCheckResult(passed=True, violations=[], confidence=1.0, processing_time_ms=0.0)

        allowed_topics = tenant_config.allowed_topics
        if not allowed_topics:
            return SafetyCheckResult(
                passed=True, violations=[], confidence=1.0,
                processing_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                metadata={"reason": "no_topic_restrictions"},
            )

        threshold = self.threshold
        if context and "classifier_threshold" in context:
            threshold = float(context["classifier_threshold"])

        prediction = await self.classify_topic_relevance(text, allowed_topics, tenant_config)

        if prediction.label in ("unavailable", "timeout", "error", "unknown"):
            # Fail-closed: when the classifier cannot run, a message whose
            # topic is restricted must not slip through. Per-tenant opt-out
            # is available by disabling the "classifier" guardrail.
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.OFF_TOPIC,
                    severity=ViolationSeverity.HIGH,
                    description=(
                        "Topic classifier unavailable; message cannot be "
                        "verified against allowed topics."
                    ),
                    metadata={"classifier_status": prediction.label, "model": prediction.model_name},
                )],
                confidence=0.0,
                processing_time_ms=(asyncio.get_event_loop().time() - start_time) * 1000,
                metadata={"classifier_status": prediction.label, "model": prediction.model_name},
            )

        elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
        if prediction.confidence < threshold:
            violation = SafetyViolation(
                category=SafetyCategory.OFF_TOPIC,
                severity=ViolationSeverity.MEDIUM,
                description=(
                    f"Message not relevant to allowed topics ({', '.join(allowed_topics)}). "
                    f"Best match '{prediction.label}' at confidence {prediction.confidence:.2f}."
                ),
                metadata={
                    "best_topic": prediction.label,
                    "confidence": prediction.confidence,
                    "threshold": threshold,
                    "all_scores": prediction.all_scores,
                },
            )
            return SafetyCheckResult(
                passed=False, violations=[violation], confidence=prediction.confidence,
                processing_time_ms=elapsed_ms,
                metadata={"layer": self.layer.name, "model": prediction.model_name},
            )

        return SafetyCheckResult(
            passed=True, violations=[], confidence=prediction.confidence,
            processing_time_ms=elapsed_ms,
            metadata={"layer": self.layer.name, "best_topic": prediction.label, "model": prediction.model_name},
        )

    async def _do_health_check(self) -> Dict[str, Any]:
        return {
            "models_loaded": self._jina_model is not None and self._roberta_model is not None,
            "jina_model": self.jina_model_path,
            "roberta_model": self.roberta_model_path,
        }
