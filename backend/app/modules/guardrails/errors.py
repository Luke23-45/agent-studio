class GuardrailError(Exception):
    pass


class GuardrailConfigurationError(GuardrailError):
    def __init__(self, field: str, message: str):
        self.field = field
        super().__init__(f"Configuration error on '{field}': {message}")


class GuardrailTimeoutError(GuardrailError):
    def __init__(self, layer: str, timeout: float):
        self.layer = layer
        self.timeout = timeout
        super().__init__(f"Guardrail layer '{layer}' timed out after {timeout}s")


class GuardrailEngineError(GuardrailError):
    def __init__(self, layer: str, message: str, cause: Exception | None = None):
        self.layer = layer
        self.cause = cause
        detail = f" ({cause})" if cause else ""
        super().__init__(f"Guardrail engine '{layer}' failed: {message}{detail}")


class GuardrailResourceExhausted(GuardrailError):
    def __init__(self, layer: str, resource: str, limit: str):
        self.layer = layer
        self.resource = resource
        super().__init__(f"Guardrail layer '{layer}' resource exhausted: {resource} limit ({limit})")
