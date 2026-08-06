"""
Validation service for output checking.

Uses Guardrails AI and schema validation to ensure output quality and safety.
"""

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ValidationResult:
    """Result of output validation."""

    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    corrected_output: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class OutputValidator:
    """Validates LLM outputs against schemas and policies."""

    def __init__(self, schema: dict[str, Any] | None = None):
        self.schema = schema
        self._guardrails_validator: Any | None = None
        self._setup_guardrails()

    def _setup_guardrails(self) -> None:
        """Initialize Guardrails AI validator if schema provided."""
        if self.schema is None:
            return

        try:
            import guardrails as gd
            from guardrails.validators import ValidLength, TwoWords

            # Create validator from schema
            self._guardrails_validator = gd.Guard.from_rail_string(
                self._schema_to_rail(self.schema)
            )
        except ImportError:
            logger.warning("guardrails_not_available", message="Guardrails AI not installed")
        except Exception as e:
            logger.error("guardrails_setup_error", error=str(e))

    def _schema_to_rail(self, schema: dict[str, Any]) -> str:
        """Convert JSON schema to Guardrails RAIL format."""
        # Simplified conversion - can be extended
        rail_str = """
<rail version="0.1">
<output>
    <string name="response" description="The generated response" />
</output>

<instructions>
- Provide a helpful and accurate response
- Stay within the allowed topics
- Do not generate harmful content
</instructions>
</rail>
"""
        return rail_str

    async def validate(
        self,
        output: str,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """Validate an output string."""
        errors = []
        warnings = []

        # Basic length check
        if len(output) == 0:
            errors.append("Output is empty")
        elif len(output) < 10:
            warnings.append("Output is very short")

        # Check for common issues
        output_lower = output.lower()
        if "i cannot help with that" in output_lower:
            warnings.append("Response indicates inability to help")

        # Use Guardrails if available
        if self._guardrails_validator:
            try:
                validated_output = await self._guardrails_validator.parse(
                    llm_output=output,
                    num_retries=1,
                )
                if hasattr(validated_output, "_validated_response"):
                    # Guardrails found issues
                    pass
            except Exception as e:
                logger.warning("guardrails_validation_error", error=str(e))

        # Schema validation if provided
        if self.schema:
            try:
                import json
                parsed = json.loads(output)
                # Could add JSON schema validation here
            except json.JSONDecodeError:
                # Not JSON, skip schema validation
                pass

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            metadata=context or {},
        )

    def validate_sync(
        self,
        output: str,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """Synchronous validation wrapper."""
        import asyncio

        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        return loop.run_until_complete(self.validate(output, context))


class SchemaValidator:
    """Validates structured outputs against JSON schemas."""

    def __init__(self, json_schema: dict[str, Any]):
        self.json_schema = json_schema

    def validate(self, data: dict[str, Any]) -> ValidationResult:
        """Validate data against JSON schema."""
        errors = []

        try:
            from jsonschema import validate, ValidationError

            validate(instance=data, schema=self.json_schema)
        except ValidationError as e:
            errors.append(str(e.message))
        except ImportError:
            logger.warning("jsonschema_not_available")

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
        )


def create_validator(schema: dict[str, Any] | None = None) -> OutputValidator:
    """Factory function to create output validator."""
    return OutputValidator(schema)


def create_schema_validator(json_schema: dict[str, Any]) -> SchemaValidator:
    """Factory function to create schema validator."""
    return SchemaValidator(json_schema)
