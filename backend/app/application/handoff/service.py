import structlog
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable
from uuid import UUID

from ...infrastructure.patterns import ManagedService, HealthComponent, HealthStatus
from ...infrastructure.patterns.retry import AsyncRetry, RetryConfig, ExponentialBackoff
from .models import HandoffRequest, HandoffResponse

logger = structlog.get_logger(__name__)


class HandoffError(Exception):
    def __init__(self, message: str, conversation_id: str = ""):
        self.conversation_id = conversation_id
        super().__init__(f"Handoff error [{conversation_id}]: {message}")


class TicketingUnavailableError(HandoffError):
    pass


@dataclass
class HandoffServiceConfig:
    ticketing_webhook_url: Optional[str] = None
    escalation_email: Optional[str] = None
    ticketing_timeout: float = 10.0
    max_retries: int = 3
    retry_min_delay: float = 1.0
    handoff_expiry_minutes: int = 60
    max_concurrent_handoffs: int = 100
    status_webhook_url: Optional[str] = None


class HandoffService(ManagedService):
    def __init__(self, config: Optional[HandoffServiceConfig] = None):
        super().__init__("handoff_service")
        self.config = config or HandoffServiceConfig()
        self._session_cache: Dict[str, HandoffRequest] = {}
        self._retry = AsyncRetry(RetryConfig(
            max_attempts=self.config.max_retries,
            backoff=ExponentialBackoff(min_delay=self.config.retry_min_delay),
        ))
        self._active_handoffs: int = 0

    async def _do_initialize(self) -> None:
        logger.info("handoff_service_initialized",
                    webhook=bool(self.config.ticketing_webhook_url),
                    expiry_minutes=self.config.handoff_expiry_minutes)

    async def _do_close(self) -> None:
        self._session_cache.clear()

    async def create_handoff(
        self,
        tenant_id: UUID,
        conversation_id: str,
        user_message: str,
        model_response: Optional[str],
        confidence: float,
        reason: str,
        context: Optional[Dict[str, Any]] = None,
        attempted_resolution: Optional[str] = None,
        recommended_next_step: Optional[str] = None,
    ) -> HandoffResponse:
        if self._active_handoffs >= self.config.max_concurrent_handoffs:
            raise HandoffError("Max concurrent handoffs reached", conversation_id)

        logger.info("creating_handoff",
                    tenant_id=tenant_id, conversation_id=conversation_id,
                    reason=reason, confidence=confidence)

        handoff_request = HandoffRequest(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            user_message=user_message,
            model_response=model_response,
            confidence=confidence,
            reason=reason,
            context=context or {},
            attempted_resolution=attempted_resolution,
            recommended_next_step=recommended_next_step,
        )

        self._session_cache[conversation_id] = handoff_request
        self._active_handoffs += 1

        error = ""
        try:
            ticket_response = await self._send_to_ticketing(handoff_request)
            if ticket_response.success:
                logger.info("handoff_ticket_created",
                           ticket_id=ticket_response.ticket_id,
                           conversation_id=conversation_id)
                return ticket_response
        except Exception as e:
            error = str(e)
            logger.error("ticketing_error", error=error, conversation_id=conversation_id)

        return HandoffResponse(
            success=True,
            ticket_id=None,
            assigned_to=None,
            message="Handoff recorded. Ticketing system unavailable.",
            metadata={"fallback": True, "error": error},
        )

    async def _send_to_ticketing(self, request: HandoffRequest) -> HandoffResponse:
        if not self.config.ticketing_webhook_url:
            logger.warning("no_ticketing_webhook_configured")
            return HandoffResponse(
                success=False, ticket_id=None, assigned_to=None,
                message="Ticketing webhook not configured",
            )

        payload = {
            "event": "handoff.created",
            "tenant_id": str(request.tenant_id),
            "conversation_id": request.conversation_id,
            "user_message": request.user_message,
            "model_response": request.model_response,
            "confidence": request.confidence,
            "reason": request.reason,
            "context": request.context,
            "attempted_resolution": request.attempted_resolution,
            "recommended_next_step": request.recommended_next_step,
            "created_at": request.created_at.isoformat(),
            "handoff_id": request.id,
        }

        async def _post() -> HandoffResponse:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.config.ticketing_webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=self.config.ticketing_timeout),
                ) as response:
                    if response.status in (200, 201):
                        result = await response.json()
                        ticket_id = result.get("ticket_id", request.id[:16])
                        assigned_to = result.get("assigned_to", "support-queue")
                        return HandoffResponse(
                            success=True,
                            ticket_id=ticket_id,
                            assigned_to=assigned_to,
                            message=f"Ticket created: {ticket_id}",
                            metadata={"status_code": response.status},
                        )
                    error_text = await response.text()
                    raise TicketingUnavailableError(
                        f"Ticketing API returned {response.status}: {error_text}",
                        request.conversation_id,
                    )

        try:
            return await self._retry.execute(_post)
        except Exception as e:
            raise TicketingUnavailableError(str(e), request.conversation_id) from e

    async def update_handoff_status(
        self,
        conversation_id: str,
        status: str,
        assigned_to: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> bool:
        request = self._session_cache.get(conversation_id)
        if not request:
            return False
        request.status = status
        if assigned_to:
            request.assigned_to = assigned_to
        if resolution:
            request.resolution = resolution

        if self.config.status_webhook_url:
            try:
                await self._notify_status_change(request)
            except Exception as e:
                logger.error("status_notification_failed", error=str(e), conversation_id=conversation_id)

        logger.info("handoff_status_updated",
                   conversation_id=conversation_id, status=status)
        return True

    async def _notify_status_change(self, request: HandoffRequest) -> None:
        import aiohttp
        payload = {
            "event": "handoff.status_changed",
            "handoff_id": request.id,
            "conversation_id": request.conversation_id,
            "status": request.status,
            "assigned_to": request.assigned_to,
            "resolution": getattr(request, 'resolution', None),
            "tenant_id": str(request.tenant_id),
        }
        async with aiohttp.ClientSession() as session:
            await session.post(
                self.config.status_webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=5),
            )

    def get_handoff_status(self, conversation_id: str) -> Optional[HandoffRequest]:
        self._expire_stale_handoffs()
        return self._session_cache.get(conversation_id)

    def clear_handoff(self, conversation_id: str) -> None:
        self._session_cache.pop(conversation_id, None)
        self._active_handoffs = max(0, self._active_handoffs - 1)
        logger.info("handoff_cleared", conversation_id=conversation_id)

    def _expire_stale_handoffs(self) -> None:
        now = time.time()
        expiry_seconds = self.config.handoff_expiry_minutes * 60
        expired = [
            cid for cid, req in self._session_cache.items()
            if (now - req.created_at.timestamp()) > expiry_seconds
        ]
        for cid in expired:
            self.clear_handoff(cid)
            logger.info("handoff_expired", conversation_id=cid)

    def get_active_handoff_count(self) -> int:
        return len(self._session_cache)

    async def _do_health_check(self) -> HealthComponent:
        return HealthComponent(
            name=self.name,
            status=HealthStatus.HEALTHY,
            metadata={
                "active_handoffs": self.get_active_handoff_count(),
                "max_concurrent": self.config.max_concurrent_handoffs,
                "ticketing_configured": bool(self.config.ticketing_webhook_url),
                "webhook_configured": bool(self.config.status_webhook_url),
            },
        )


def create_handoff_service(
    ticketing_webhook_url: Optional[str] = None,
    escalation_email: Optional[str] = None,
) -> HandoffService:
    config = HandoffServiceConfig(
        ticketing_webhook_url=ticketing_webhook_url,
        escalation_email=escalation_email,
    )
    return HandoffService(config)
