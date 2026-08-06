import structlog
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TypeVar
from contextlib import contextmanager

logger = structlog.get_logger(__name__)

T = TypeVar("T")


@dataclass
class CounterMetric:
    name: str
    value: int = 0
    labels: Dict[str, str] = field(default_factory=dict)

    def inc(self, amount: int = 1) -> None:
        self.value += amount

    def snapshot(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "type": "counter", "labels": self.labels}


@dataclass
class GaugeMetric:
    name: str
    value: float = 0.0
    labels: Dict[str, str] = field(default_factory=dict)

    def set(self, value: float) -> None:
        self.value = value

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount

    def snapshot(self) -> Dict[str, Any]:
        return {"name": self.name, "value": self.value, "type": "gauge", "labels": self.labels}


@dataclass
class HistogramMetric:
    name: str
    buckets: List[float] = field(default_factory=lambda: [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0])
    values: List[float] = field(default_factory=list)
    labels: Dict[str, str] = field(default_factory=dict)

    def observe(self, value: float) -> None:
        self.values.append(value)

    def snapshot(self) -> Dict[str, Any]:
        if not self.values:
            return {"name": self.name, "count": 0, "type": "histogram", "labels": self.labels}
        sorted_vals = sorted(self.values)
        n = len(sorted_vals)
        return {
            "name": self.name,
            "count": n,
            "sum": sum(self.values),
            "min": sorted_vals[0],
            "max": sorted_vals[-1],
            "avg": sum(self.values) / n,
            "p50": sorted_vals[int(n * 0.50)],
            "p95": sorted_vals[int(n * 0.95)],
            "p99": sorted_vals[int(n * 0.99)],
            "buckets": {str(b): sum(1 for v in self.values if v <= b) for b in self.buckets},
            "type": "histogram",
            "labels": self.labels,
        }


class MetricsCollector:
    def __init__(self, namespace: str = ""):
        self.namespace = namespace
        self._counters: Dict[str, CounterMetric] = {}
        self._gauges: Dict[str, GaugeMetric] = {}
        self._histograms: Dict[str, HistogramMetric] = {}

    def counter(self, name: str, labels: Optional[Dict[str, str]] = None) -> CounterMetric:
        key = f"{self.namespace}.{name}" if self.namespace else name
        if key not in self._counters:
            self._counters[key] = CounterMetric(name=key, labels=labels or {})
        return self._counters[key]

    def gauge(self, name: str, labels: Optional[Dict[str, str]] = None) -> GaugeMetric:
        key = f"{self.namespace}.{name}" if self.namespace else name
        if key not in self._gauges:
            self._gauges[key] = GaugeMetric(name=key, labels=labels or {})
        return self._gauges[key]

    def histogram(self, name: str, labels: Optional[Dict[str, str]] = None) -> HistogramMetric:
        key = f"{self.namespace}.{name}" if self.namespace else name
        if key not in self._histograms:
            self._histograms[key] = HistogramMetric(name=key, labels=labels or {})
        return self._histograms[key]

    def record_latency(self, name: str, labels: Optional[Dict[str, str]] = None) -> Callable[[], None]:
        hist = self.histogram(name, labels)
        start = time.monotonic()
        def _record():
            elapsed = time.monotonic() - start
            hist.observe(elapsed)
        return _record

    def snapshot_all(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, metric in self._counters.items():
            result[key] = metric.snapshot()
        for key, metric in self._histograms.items():
            result[key] = metric.snapshot()
        for key, metric in self._gauges.items():
            result[key] = metric.snapshot()
        return result
