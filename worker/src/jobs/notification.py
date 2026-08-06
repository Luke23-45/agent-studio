"""
Notification Job.

Sends notifications for:
- Escalation alerts (human handoff required)
- Red-team vulnerability reports
- Ingestion completion/failure
- Scheduled report delivery
- System health warnings

Supports multiple channels: email, Slack, webhook, SMS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)

class NotificationChannel(str, Enum):
    EMAIL = "email"
    SLACK = "slack"
    WEBHOOK = "webhook"
    SMS = "sms"

class NotificationPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"

@dataclass
class NotificationRecipient:
    """Recipient configuration."""
    channel: NotificationChannel
    address: str  # email, slack channel, webhook URL, phone number
    name: Optional[str] = None

@dataclass
class NotificationMessage:
    """Notification message content."""
    subject: str
    body: str
    priority: NotificationPriority = NotificationPriority.NORMAL
    metadata: Dict[str, Any] = field(default_factory=dict)
    attachments: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class NotificationResult:
    """Result of sending a single notification."""
    recipient: NotificationRecipient
    success: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    sent_at: datetime = field(default_factory=datetime.utcnow)

@dataclass
class NotificationReport:
    """Aggregated report from a notification job."""
    job_id: str
    total_sent: int
    successful: int
    failed: int
    results: List[NotificationResult] = field(default_factory=list)
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

class NotificationJob:
    """
    Sends batch notifications to configured recipients.
    
    Usage:
        job = NotificationJob()
        job.add_recipient(NotificationChannel.EMAIL, "admin@example.com")
        await job.send(NotificationMessage(subject="Alert", body="..."))
    """
    
    def __init__(
        self,
        smtp_host: Optional[str] = None,
        slack_webhook: Optional[str] = None,
        default_sender: str = "noreply@neryva.ai",
    ):
        self.smtp_host = smtp_host
        self.slack_webhook = slack_webhook
        self.default_sender = default_sender
        self.job_id = f"notify-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
        
        self.recipients: List[NotificationRecipient] = []
    
    def add_recipient(
        self,
        channel: NotificationChannel,
        address: str,
        name: Optional[str] = None,
    ):
        """Add a recipient to the notification list."""
        self.recipients.append(NotificationRecipient(
            channel=channel,
            address=address,
            name=name,
        ))
    
    async def send(self, message: NotificationMessage) -> NotificationReport:
        """Send notification to all configured recipients."""
        logger.info(f"Starting notification job {self.job_id}")
        
        report = NotificationReport(
            job_id=self.job_id,
            total_sent=len(self.recipients),
            successful=0,
            failed=0,
        )
        
        try:
            for recipient in self.recipients:
                try:
                    result = await self._send_to_recipient(recipient, message)
                    report.results.append(result)
                    
                    if result.success:
                        report.successful += 1
                    else:
                        report.failed += 1
                        
                except Exception as e:
                    logger.error(f"Failed to notify {recipient.address}: {e}")
                    report.results.append(NotificationResult(
                        recipient=recipient,
                        success=False,
                        error=str(e),
                    ))
                    report.failed += 1
            
            report.completed_at = datetime.utcnow()
            
            logger.info(
                f"Notification completed: {report.successful}/{report.total_sent} successful"
            )
            
        except Exception as e:
            logger.error(f"Notification job failed: {e}")
            raise
        
        return report
    
    async def _send_to_recipient(
        self,
        recipient: NotificationRecipient,
        message: NotificationMessage,
    ) -> NotificationResult:
        """Send notification to a single recipient via their preferred channel."""
        if recipient.channel == NotificationChannel.EMAIL:
            return await self._send_email(recipient, message)
        elif recipient.channel == NotificationChannel.SLACK:
            return await self._send_slack(recipient, message)
        elif recipient.channel == NotificationChannel.WEBHOOK:
            return await self._send_webhook(recipient, message)
        elif recipient.channel == NotificationChannel.SMS:
            return await self._send_sms(recipient, message)
        else:
            return NotificationResult(
                recipient=recipient,
                success=False,
                error=f"Unknown channel: {recipient.channel}",
            )
    
    async def _send_email(
        self,
        recipient: NotificationRecipient,
        message: NotificationMessage,
    ) -> NotificationResult:
        """Send email notification."""
        # Placeholder - would use aiosmtplib or similar
        logger.info(f"Would send email to {recipient.address}: {message.subject}")
        
        if not self.smtp_host:
            return NotificationResult(
                recipient=recipient,
                success=False,
                error="SMTP host not configured",
            )
        
        # In production:
        # import aiosmtplib
        # from email.mime.text import MIMEText
        # ... send email ...
        
        return NotificationResult(
            recipient=recipient,
            success=True,
            message_id=f"email-{datetime.utcnow().timestamp()}",
        )
    
    async def _send_slack(
        self,
        recipient: NotificationRecipient,
        message: NotificationMessage,
    ) -> NotificationResult:
        """Send Slack notification."""
        # Placeholder - would use aiohttp to post to Slack webhook
        logger.info(f"Would send Slack message to {recipient.address}")
        
        webhook_url = self.slack_webhook or recipient.address
        
        # In production:
        # import aiohttp
        # payload = {
        #     "channel": recipient.address,
        #     "username": "Neryva Alerts",
        #     "text": message.body,
        #     "blocks": [...]
        # }
        # async with aiohttp.ClientSession() as session:
        #     await session.post(webhook_url, json=payload)
        
        return NotificationResult(
            recipient=recipient,
            success=True,
            message_id=f"slack-{datetime.utcnow().timestamp()}",
        )
    
    async def _send_webhook(
        self,
        recipient: NotificationRecipient,
        message: NotificationMessage,
    ) -> NotificationResult:
        """Send webhook notification."""
        # Placeholder - would POST to custom webhook URL
        logger.info(f"Would send webhook to {recipient.address}")
        
        # In production:
        # import aiohttp
        # payload = {
        #     "subject": message.subject,
        #     "body": message.body,
        #     "priority": message.priority.value,
        #     **message.metadata
        # }
        # async with aiohttp.ClientSession() as session:
        #     await session.post(recipient.address, json=payload)
        
        return NotificationResult(
            recipient=recipient,
            success=True,
            message_id=f"webhook-{datetime.utcnow().timestamp()}",
        )
    
    async def _send_sms(
        self,
        recipient: NotificationRecipient,
        message: NotificationMessage,
    ) -> NotificationResult:
        """Send SMS notification."""
        # Placeholder - would use Twilio, Vonage, or similar
        logger.info(f"Would send SMS to {recipient.address}")
        
        # In production:
        # from twilio.rest import Client
        # client = Client(account_sid, auth_token)
        # client.messages.create(body=message.body, to=recipient.address, from_=...)
        
        return NotificationResult(
            recipient=recipient,
            success=True,
            message_id=f"sms-{datetime.utcnow().timestamp()}",
        )


# Convenience functions for common notification scenarios

async def send_escalation_alert(
    tenant_id: str,
    handoff_id: str,
    recipients: List[str],
    reason: str,
):
    """Send escalation alert to support team."""
    job = NotificationJob()
    for email in recipients:
        job.add_recipient(NotificationChannel.EMAIL, email)
    
    message = NotificationMessage(
        subject=f"[CRITICAL] Escalation Required - Tenant {tenant_id}",
        body=f"""
Human handoff required for tenant {tenant_id}.

Handoff ID: {handoff_id}
Reason: {reason}
Time: {datetime.utcnow().isoformat()}

Please review in the admin dashboard immediately.
        """,
        priority=NotificationPriority.CRITICAL,
    )
    
    return await job.send(message)


async def send_redteam_report(
    tenant_id: str,
    report_path: str,
    critical_count: int,
    high_count: int,
    recipients: List[str],
):
    """Send red-team vulnerability report."""
    job = NotificationJob()
    for email in recipients:
        job.add_recipient(NotificationChannel.EMAIL, email)
    
    severity = "CRITICAL" if critical_count > 0 else "HIGH" if high_count > 0 else "MEDIUM"
    priority = NotificationPriority.CRITICAL if critical_count > 0 else NotificationPriority.HIGH
    
    message = NotificationMessage(
        subject=f"[{severity}] Red-Team Report - Tenant {tenant_id}",
        body=f"""
Red-team assessment completed for tenant {tenant_id}.

Findings:
- Critical: {critical_count}
- High: {high_count}

Report: {report_path}

Please review and remediate vulnerabilities promptly.
        """,
        priority=priority,
        metadata={
            "tenant_id": tenant_id,
            "critical_count": critical_count,
            "high_count": high_count,
        },
    )
    
    return await job.send(message)
