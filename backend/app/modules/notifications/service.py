"""Notification delivery service with real transports.

Delivery is performed over real HTTP (httpx) or SMTP (stdlib smtplib).
Missing configuration raises so callers (queue retries / dead-lettering)
can react; notifications are never silently dropped or faked.
"""

import smtplib
from email.message import EmailMessage

import structlog
from httpx import AsyncClient, Timeout

from backend.app.settings.env import get_settings

from .models import NotificationChannel, NotificationRequest, NotificationResult

logger = structlog.get_logger(__name__)


class NotificationService:
    def __init__(self, smtp_config: dict | None = None, sms_webhook_url: str | None = None):
        settings = get_settings()
        self.smtp_config = smtp_config or {
            "host": getattr(settings, "SMTP_HOST", None),
            "port": getattr(settings, "SMTP_PORT", 587),
            "user": getattr(settings, "SMTP_USER", None),
            "password": getattr(settings, "SMTP_PASSWORD", None),
            "from": getattr(settings, "SMTP_FROM", None),
            "starttls": getattr(settings, "SMTP_STARTTLS", True),
        }
        self.sms_webhook_url = sms_webhook_url or getattr(settings, "SMS_WEBHOOK_URL", None)
        self._client: AsyncClient | None = None

    async def send(self, request: NotificationRequest) -> NotificationResult:
        try:
            if request.channel == NotificationChannel.EMAIL:
                await self._send_email(request)
            elif request.channel == NotificationChannel.SLACK:
                await self._send_http_json(request.to, request.to_slack_payload())
            elif request.channel == NotificationChannel.TEAMS:
                await self._send_http_json(request.to, request.to_teams_payload())
            elif request.channel == NotificationChannel.SMS:
                await self._send_sms(request)
            elif request.channel == NotificationChannel.WEBHOOK:
                await self._send_http_json(request.to, request.to_webhook_payload())
            else:
                raise ValueError(f"Unsupported notification channel: {request.channel}")
            logger.info("notification_sent", channel=request.channel.value, id=request.id)
            return NotificationResult(success=True, channel=request.channel, message="Delivered")
        except Exception as e:
            logger.error("notification_failed", channel=request.channel.value, id=request.id, error=str(e))
            return NotificationResult(success=False, channel=request.channel, message=str(e), error=str(e))

    async def _client_get(self) -> AsyncClient:
        if self._client is None:
            self._client = AsyncClient(timeout=Timeout(15.0))
        return self._client

    async def _send_http_json(self, url: str, payload: dict) -> None:
        if not url:
            raise ValueError(f"No webhook URL for channel; cannot deliver notification")
        client = await self._client_get()
        response = await client.post(url, json=payload, headers={"Content-Type": "application/json"})
        response.raise_for_status()

    async def _send_email(self, request: NotificationRequest) -> None:
        if not request.to:
            raise ValueError("Email notification missing recipient address")
        cfg = self.smtp_config
        if not cfg.get("host") or not cfg.get("from"):
            raise ValueError(
                "SMTP not configured (set SMTP_HOST and SMTP_FROM). "
                "Email delivery cannot be simulated."
            )
        message = EmailMessage()
        message["From"] = cfg["from"]
        message["To"] = request.to
        message["Subject"] = request.subject or "Neryva notification"
        message.set_content(request.body)

        if cfg.get("port") == 465:
            with smtplib.SMTP_SSL(cfg["host"], int(cfg["port"])) as server:
                if cfg.get("user"):
                    server.login(cfg["user"], cfg["password"])
                server.send_message(message)
        else:
            with smtplib.SMTP(cfg["host"], int(cfg["port"])) as server:
                if cfg.get("starttls"):
                    server.starttls()
                if cfg.get("user"):
                    server.login(cfg["user"], cfg["password"])
                server.send_message(message)

    async def _send_sms(self, request: NotificationRequest) -> None:
        if not self.sms_webhook_url:
            raise ValueError(
                "SMS delivery requires a provider webhook (set SMS_WEBHOOK_URL). "
                "SMS delivery cannot be simulated."
            )
        payload = request.to_webhook_payload()
        payload["to"] = request.to
        await self._send_http_json(self.sms_webhook_url, payload)


_notification_service: NotificationService | None = None


def get_notification_service(
    smtp_config: dict | None = None, sms_webhook_url: str | None = None
) -> NotificationService:
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService(
            smtp_config=smtp_config, sms_webhook_url=sms_webhook_url
        )
    return _notification_service


def create_notification_service(
    smtp_config: dict | None = None, sms_webhook_url: str | None = None
) -> NotificationService:
    return NotificationService(smtp_config=smtp_config, sms_webhook_url=sms_webhook_url)
