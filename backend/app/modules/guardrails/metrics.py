from ...infrastructure.patterns import MetricsCollector


class GuardrailMetrics(MetricsCollector):
    def __init__(self, tenant_id: str = "global"):
        super().__init__(namespace=f"guardrails.{tenant_id}")
        self._evaluations_total = self.counter("evaluations.total", {"tenant": tenant_id})
        self._evaluations_blocked = self.counter("evaluations.blocked", {"tenant": tenant_id})
        self._evaluations_allowed = self.counter("evaluations.allowed", {"tenant": tenant_id})
        self._layer_latency = self.histogram("layer.latency", {"tenant": tenant_id})
        self._active_requests = self.gauge("requests.active", {"tenant": tenant_id})

    def record_evaluation(self, layer: str, allowed: bool, latency_ms: float) -> None:
        self._evaluations_total.inc()
        if allowed:
            self._evaluations_allowed.inc()
        else:
            self._evaluations_blocked.inc()
        self._layer_latency.observe(latency_ms / 1000.0)

    def mark_active_request(self, delta: int = 1) -> None:
        self._active_requests.inc(float(delta))
