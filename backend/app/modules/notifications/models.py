"""Notification data models."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import uuid4


class NotificationChannel(str, Enum):
    """Supported delivery channels."""

    EMAIL = "email"
    SLACK = "slack"
    TEAMS = "teams"
    WEBHOOK = "webhook"
    SMS = "sms"


@dataclass
class NotificationRequest:
    """A single notification delivery request."""

    id: str = field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    session_id: str = ""
    channel: NotificationChannel = NotificationChannel.WEBHOOK
    to: str = ""  # email address, webhook URL, or phone number
    subject: str = ""
    body: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: dict = field(default_factory=dict)

    def to_slack_payload(self) -> dict:
        return {
            "text": self.body,
            "attachments": [
                {
                    "color": "warning",
                    "title": self.subject or "Neryva notification",
                    "text": self.body,
                    "fields": [
                        {"title": "Tenant", "value": self.tenant_id, "short": True},
                        {"title": "Session", "value": self.session_id, "short": True},
                    ],
                    "footer": "Neryva Agent Studio",
                    "ts": int(self.created_at.timestamp()),
                }
            ],
        }

    def to_teams_payload(self) -> dict:
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": self.subject or "Neryva notification",
            "title": self.subject or "Neryva notification",
            "text": self.body,
            "sections": [
                {
                    "facts": [
                        {"name": "Tenant", "value": self.tenant_id},
                        {"name": "Session", "value": self.session_id},
                    ]
                }
            ],
        }

    def to_webhook_payload(self) -> dict:
        return {
            "event": "notification",
            "id": self.id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "channel": self.channel.value,
            "subject": self.subject,
            "body": self.body,
            "created_at": self.created_at.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class NotificationResult:
    """Outcome of a delivery attempt."""

    success: bool
    channel: NotificationChannel
    message: str = ""
    error: str = ""
