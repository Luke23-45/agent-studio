"""Notification delivery module.

Real transports, no mocks:
- slack / teams / webhook: HTTP POST via httpx
- email: SMTP via stdlib smtplib (STARTTLS)
- sms: provider webhook (SMS_WEBHOOK_URL)

Anything not configured fails loudly with a descriptive error so the
queue layer can retry / dead-letter instead of silently dropping
notifications.
"""

from .models import NotificationChannel, NotificationRequest, NotificationResult
from .service import NotificationService, create_notification_service, get_notification_service

__all__ = [
    "NotificationChannel",
    "NotificationRequest",
    "NotificationResult",
    "NotificationService",
    "create_notification_service",
    "get_notification_service",
]
