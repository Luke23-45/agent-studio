import asyncio
import json as json_lib
import structlog
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...domain.safety import SafetyCheckResult, SafetyViolation, ViolationSeverity, SafetyCategory
from ...infrastructure.patterns import ManagedService
from .types import GuardrailLayer
from .errors import GuardrailEngineError

logger = structlog.get_logger(__name__)


@dataclass
class OutputSchema:
    name: str
    json_schema: Dict[str, Any]
    validators: List[str] = field(default_factory=list)
    strict_mode: bool = True

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("OutputSchema name is required")
        if not self.json_schema:
            raise ValueError("OutputSchema json_schema is required")


class GuardrailsAIEngine(ManagedService):
    layer = GuardrailLayer.GUARDRAILS_AI

    def __init__(self, validation_timeout: float = 10.0):
        super().__init__("guardrails_ai")
        self.validation_timeout = validation_timeout
        self._schemas: Dict[str, OutputSchema] = {}
        self._gd_available = False

    def register_schema(self, schema: OutputSchema) -> None:
        if schema.name in self._schemas:
            logger.warning("schema_overwritten", name=schema.name)
        self._schemas[schema.name] = schema
        logger.info("schema_registered", name=schema.name)

    def unregister_schema(self, name: str) -> bool:
        return self._schemas.pop(name, None) is not None

    async def _do_initialize(self) -> None:
        try:
            import guardrails as gd
            self._gd_available = True
            logger.info("guardrails_ai_available")
        except ImportError:
            self._gd_available = False
            logger.warning("guardrails_ai_not_available, using fallback json validation")

    async def _do_close(self) -> None:
        self._schemas.clear()

    async def validate_output(self, output: str, schema_name: str) -> SafetyCheckResult:
        start_time = asyncio.get_event_loop().time()
        if schema_name not in self._schemas:
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.SCHEMA_VALIDATION,
                    severity=ViolationSeverity.HIGH,
                    description=f"Unknown schema: {schema_name}",
                )],
                confidence=0.0,
            )

        schema = self._schemas[schema_name]
        if not output or not output.strip():
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.SCHEMA_VALIDATION,
                    severity=ViolationSeverity.HIGH,
                    description="Empty output",
                )],
                confidence=0.0,
            )

        if not self._gd_available:
            result = await asyncio.get_event_loop().run_in_executor(
                None, self._basic_json_validation, output, schema
            )
            elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            result.processing_time_ms = elapsed_ms
            return result

        try:
            import guardrails as gd
            rail_string = self._build_rail_string(schema)
            loop = asyncio.get_event_loop()
            validator = await loop.run_in_executor(None, lambda: gd.Guard.from_rail_string(rail_string))
            validated = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: validator.validate(llm_response=output)),
                timeout=self.validation_timeout,
            )
            if validated is None or validated[0] is None:
                return SafetyCheckResult(
                    passed=False,
                    violations=[SafetyViolation(
                        category=SafetyCategory.SCHEMA_VALIDATION,
                        severity=ViolationSeverity.HIGH,
                        description="Output failed schema validation",
                        metadata={"schema": schema_name},
                    )],
                    confidence=0.0,
                )
            return SafetyCheckResult(passed=True, violations=[], confidence=0.95)
        except asyncio.TimeoutError:
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.SCHEMA_VALIDATION,
                    severity=ViolationSeverity.MEDIUM,
                    description=f"Validation timed out after {self.validation_timeout}s",
                )],
                confidence=0.0,
            )
        except Exception as e:
            logger.error("guardrails_ai_error", error=str(e))
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.SCHEMA_VALIDATION,
                    severity=ViolationSeverity.MEDIUM,
                    description=str(e),
                )],
                confidence=0.0,
            )

    def _basic_json_validation(self, output: str, schema: OutputSchema) -> SafetyCheckResult:
        try:
            from jsonschema import validate, ValidationError
            parsed = json_lib.loads(output)
            validate(instance=parsed, schema=schema.json_schema)
            return SafetyCheckResult(passed=True, violations=[], confidence=0.9)
        except json_lib.JSONDecodeError as e:
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.SCHEMA_VALIDATION,
                    severity=ViolationSeverity.HIGH,
                    description=f"Invalid JSON: {e}",
                )],
                confidence=0.0,
            )
        except ValidationError as e:
            return SafetyCheckResult(
                passed=False,
                violations=[SafetyViolation(
                    category=SafetyCategory.SCHEMA_VALIDATION,
                    severity=ViolationSeverity.HIGH,
                    description=e.message,
                )],
                confidence=0.0,
            )

    def _build_rail_string(self, schema: OutputSchema) -> str:
        """Serialize the registered JSON schema into a real RAIL spec.

        The previous implementation returned a vacuous `<object name=.../>`
        that ignored every property, so validation never checked anything.
        This generates one `<property>` element per JSON schema property,
        including required flags, types, nested objects, and arrays.
        """
        body = self._rail_object_body(schema.json_schema)
        return (
            '<rail version="0.1">\n'
            "<output>\n"
            f'<object name="{self._rail_element_name(schema.name)}">\n'
            f"{body}"
            "</object>\n"
            "</output>\n"
            "</rail>"
        )

    @staticmethod
    def _rail_element_name(name: str) -> str:
        """Sanitize a schema name into a valid XML element name."""
        return "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)

    @classmethod
    def _rail_object_body(cls, json_schema: Dict[str, Any]) -> str:
        properties = json_schema.get("properties") or {}
        required = set(json_schema.get("required") or [])
        lines: List[str] = []
        for prop_name, prop_schema in properties.items():
            lines.append(cls._rail_property(prop_name, prop_schema, prop_name in required))
        return "".join(lines)

    @classmethod
    def _rail_property(cls, name: str, prop_schema: Dict[str, Any], is_required: bool) -> str:
        el_name = cls._rail_element_name(name)
        prop_type = prop_schema.get("type")
        required_attr = ' required="true"' if is_required else ""
        indent = "    "

        if prop_type == "object":
            nested = cls._rail_object_body(prop_schema)
            return (
                f'{indent}<object name="{el_name}"{required_attr}>\n'
                f"{nested}"
                f"{indent}</object>\n"
            )
        if prop_type == "array":
            items = prop_schema.get("items") or {}
            item_type = items.get("type")
            if item_type == "object":
                nested = cls._rail_object_body(items)
                return (
                    f'{indent}<list name="{el_name}"{required_attr}>\n'
                    f"{indent}    <object>\n"
                    f"{nested}"
                    f"{indent}    </object>\n"
                    f"{indent}</list>\n"
                )
            if item_type in ("string", "integer", "number", "boolean"):
                rail_item = cls._rail_type(item_type)
                return (
                    f'{indent}<list name="{el_name}"{required_attr}>\n'
                    f'{indent}    <element type="{rail_item}"/>\n'
                    f"{indent}</list>\n"
                )
            return f'{indent}<list name="{el_name}"{required_attr}/>\n'
        if prop_type in ("string", "integer", "number", "boolean"):
            rail_type = cls._rail_type(prop_type)
            return f'{indent}<property name="{el_name}" type="{rail_type}"{required_attr}/>\n'
        # Unknown/null types: skip property rather than emit invalid XML
        return ""

    @staticmethod
    def _rail_type(json_type: str) -> str:
        mapping = {
            "string": "str",
            "integer": "int",
            "number": "float",
            "boolean": "bool",
        }
        return mapping.get(json_type, "str")

    async def _do_health_check(self) -> Dict[str, Any]:
        return {
            "guardrails_ai_available": self._gd_available,
            "schemas_registered": len(self._schemas),
            "schema_names": list(self._schemas.keys()),
        }
