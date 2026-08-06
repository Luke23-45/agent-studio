from dataclasses import dataclass, field
from typing import Any


@dataclass
class EscalationMetrics:
    total_escalations: int = 0
    escalations_by_reason: dict[str, int] = field(default_factory=dict)
    average_confidence_at_escalation: float = 0.0
    average_resolution_time_minutes: float = 0.0
    tickets_created: int = 0
    tickets_resolved: int = 0


class EscalationMetricsCollector:
    def __init__(self):
        self.metrics = EscalationMetrics()
        self._escalation_confidences: list[float] = []

    def record_escalation(self, reason: str, confidence: float) -> None:
        self.metrics.total_escalations += 1
        reason_key = reason.split("-")[0].strip() if "-" in reason else reason
        self.metrics.escalations_by_reason[reason_key] = (
            self.metrics.escalations_by_reason.get(reason_key, 0) + 1
        )
        self._escalation_confidences.append(confidence)
        if self._escalation_confidences:
            self.metrics.average_confidence_at_escalation = (
                sum(self._escalation_confidences) / len(self._escalation_confidences)
            )

    def record_ticket_created(self) -> None:
        self.metrics.tickets_created += 1

    def record_ticket_resolved(self, resolution_time_minutes: float) -> None:
        self.metrics.tickets_resolved += 1
        total = self.metrics.tickets_resolved
        old_avg = self.metrics.average_resolution_time_minutes
        self.metrics.average_resolution_time_minutes = (
            (old_avg * (total - 1) + resolution_time_minutes) / total
        )

    def get_summary(self) -> dict[str, Any]:
        return {
            "total_escalations": self.metrics.total_escalations,
            "escalations_by_reason": self.metrics.escalations_by_reason,
            "average_confidence_at_escalation": round(self.metrics.average_confidence_at_escalation, 3),
            "average_resolution_time_minutes": round(self.metrics.average_resolution_time_minutes, 2),
            "tickets_created": self.metrics.tickets_created,
            "tickets_resolved": self.metrics.tickets_resolved,
            "resolution_rate": (
                self.metrics.tickets_resolved / self.metrics.tickets_created
                if self.metrics.tickets_created > 0
                else 0.0
            ),
        }


_metrics_collector: EscalationMetricsCollector | None = None


def get_metrics_collector() -> EscalationMetricsCollector:
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = EscalationMetricsCollector()
    return _metrics_collector
