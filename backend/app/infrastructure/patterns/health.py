from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Protocol
from datetime import datetime


class HealthStatus(Enum):
    HEALTHY = auto()
    DEGRADED = auto()
    UNHEALTHY = auto()


@dataclass
class HealthComponent:
    name: str
    status: HealthStatus
    message: str = ""
    last_checked: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)
    dependencies: List["HealthComponent"] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.name,
            "message": self.message,
            "last_checked": self.last_checked.isoformat(),
            "metadata": self.metadata,
            "dependencies": [d.to_dict() for d in self.dependencies],
        }


class HealthCheckable(Protocol):
    async def health_check(self) -> HealthComponent:
        ...


@dataclass
class HealthReport:
    status: HealthStatus
    components: Dict[str, HealthComponent]
    summary: str = ""
    generated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.name,
            "summary": self.summary,
            "generated_at": self.generated_at.isoformat(),
            "components": {name: c.to_dict() for name, c in self.components.items()},
        }
