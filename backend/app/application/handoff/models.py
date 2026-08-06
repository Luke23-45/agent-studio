from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4


@dataclass
class HandoffRequest:
    tenant_id: UUID
    conversation_id: str
    user_message: str
    model_response: str | None
    confidence: float
    reason: str
    context: dict[str, Any] = field(default_factory=dict)
    attempted_resolution: str | None = None
    recommended_next_step: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class HandoffResponse:
    success: bool
    ticket_id: str | None
    assigned_to: str | None
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
