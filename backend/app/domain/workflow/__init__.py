"""
Workflow domain models and business logic.

Defines conversation workflows, state machines, and process flows.
"""

from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID, uuid4


class WorkflowState(Enum):
    """States in a workflow."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    WAITING_FOR_INPUT = "waiting_for_input"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    FAILED = "failed"


@dataclass
class WorkflowStep:
    """A single step in a workflow."""

    id: UUID = field(default_factory=uuid4)
    name: str = ""
    description: str = ""
    requires_human_approval: bool = False
    timeout_seconds: int = 300


@dataclass
class WorkflowDefinition:
    """Definition of a complete workflow."""

    id: UUID = field(default_factory=uuid4)
    name: str = ""
    tenant_id: UUID | None = None
    steps: list[WorkflowStep] = field(default_factory=list)
    version: int = 1

    def get_step(self, step_id: UUID) -> WorkflowStep | None:
        """Get a step by ID."""
        for step in self.steps:
            if step.id == step_id:
                return step
        return None


@dataclass
class WorkflowInstance:
    """Running instance of a workflow."""

    id: UUID = field(default_factory=uuid4)
    definition_id: UUID | None = None
    conversation_id: UUID | None = None
    current_step_id: UUID | None = None
    state: WorkflowState = WorkflowState.PENDING
    context: dict[str, str] = field(default_factory=dict)
    history: list[dict[str, str]] = field(default_factory=list)

    def advance(self, step_id: UUID) -> None:
        """Advance to the next step."""
        self.current_step_id = step_id
        self.state = WorkflowState.IN_PROGRESS
        self.history.append({"step_id": str(step_id), "action": "advance"})

    def complete(self) -> None:
        """Mark workflow as completed."""
        self.state = WorkflowState.COMPLETED
        self.history.append({"action": "complete"})

    def escalate(self) -> None:
        """Mark workflow as escalated."""
        self.state = WorkflowState.ESCALATED
        self.history.append({"action": "escalate"})
