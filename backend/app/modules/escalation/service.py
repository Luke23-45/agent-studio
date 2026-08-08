import structlog
from typing import Any

from backend.app.domain.policy import PolicyAction
from backend.app.domain.tenant import TenantConfig

from .models import HandoffRequest, HandoffResponse

logger = structlog.get_logger(__name__)


class EscalationService:
    def __init__(
        self,
        tenant_config: TenantConfig,
        ticketing_webhook_url: str | None = None,
        helpdesk_integration: str | None = None,
        escalation_repository: Any | None = None,
        notification_channel: str | None = None,
        notification_target: str | None = None,
        ticketing_config: Any | None = None,
        ticketing_transport: Any | None = None,
    ):
        self.tenant_config = tenant_config
        self.ticketing_webhook_url = ticketing_webhook_url
        self.helpdesk_integration = helpdesk_integration or "generic"
        self.escalation_repository = escalation_repository
        self.notification_channel = notification_channel
        self.notification_target = notification_target
        self._ticketing_client: Any | None = None
        # P9-3: concrete helpdesk adapters (zendesk/jira/servicenow) with
        # injectable transport for tests.
        self._ticketing_config = ticketing_config
        self._ticketing_transport = ticketing_transport

    def _adapter_config(self) -> Any:
        if self._ticketing_config is not None:
            return self._ticketing_config
        from backend.app.adapters.ticketing import TicketingConfig

        config = TicketingConfig.from_settings()
        if self.helpdesk_integration and self.helpdesk_integration != "generic":
            config.adapter = self.helpdesk_integration
        return config

    def _default_notification_target(self) -> str | None:
        from backend.app.settings.env import get_settings

        settings = get_settings()
        if self.notification_channel is None:
            self.notification_channel = settings.NOTIFICATION_CHANNEL
        if self.notification_target is None:
            self.notification_target = settings.NOTIFICATION_TARGET
        return self.notification_target

    async def _enqueue_operator_notification(self, handoff: HandoffRequest) -> None:
        """Best-effort operator alert via the job queue (retries + DLQ).

        Enqueued only when a target is configured; uninitialized queues
        (unit-test contexts) are skipped silently.
        """
        try:
            from backend.app.infrastructure.queue.manager import Job, get_queue_manager

            if not self._default_notification_target():
                logger.info("escalation_notification_skipped", reason="no target configured")
                return
            manager = get_queue_manager()
            job = Job(
                type="notification.send",
                payload={
                    "channel": self.notification_channel,
                    "to": self.notification_target,
                    "tenant_id": str(self.tenant_config.id),
                    "session_id": handoff.session_id,
                    "subject": f"Neryva escalation {str(handoff.id)[:8]}",
                    "body": (
                        f"Escalation {handoff.id} for tenant {self.tenant_config.slug}: "
                        f"confidence={handoff.confidence:.2f} reason={handoff.reason}"
                    ),
                    "metadata": {
                        "handoff_id": str(handoff.id),
                        "category": "escalation",
                    },
                },
            )
            accepted = await manager.enqueue(
                job, idempotency_key=f"escalation:{handoff.id}"
            )
            logger.info(
                "escalation_notification_enqueued",
                handoff_id=handoff.id,
                channel=self.notification_channel,
                accepted=accepted,
            )
        except RuntimeError:
            logger.info("escalation_notification_skipped", reason="queue not initialized")
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("escalation_notification_failed", handoff_id=handoff.id, error=str(e))

    def should_escalate(
        self,
        confidence: float,
        policy_action: PolicyAction,
        explicit_request: bool = False,
    ) -> bool:
        if explicit_request:
            return True
        if policy_action == PolicyAction.ESCALATE:
            return True
        if confidence < self.tenant_config.escalation_threshold:
            return True
        return False

    def detect_explicit_handoff_request(self, message: str) -> bool:
        handoff_phrases = [
            "speak to a human",
            "talk to a person",
            "human agent",
            "real person",
            "customer service",
            "support agent",
            "escalate this",
            "supervisor",
            "manager",
        ]
        message_lower = message.lower()
        return any(phrase in message_lower for phrase in handoff_phrases)

    async def create_handoff(
        self,
        session_id: str,
        user_message: str,
        confidence: float,
        reason: str,
        policy_action: PolicyAction,
        conversation_history: list[dict[str, str]],
        model_response: str | None = None,
        attempted_resolutions: list[str] | None = None,
        conversation_id: str | None = None,
    ) -> HandoffResponse:
        handoff = HandoffRequest(
            tenant_id=self.tenant_config.id,
            session_id=session_id,
            user_message=user_message,
            model_response=model_response,
            confidence=confidence,
            reason=reason,
            policy_action=policy_action,
            conversation_history=conversation_history,
            attempted_resolutions=attempted_resolutions or [],
            recommended_next_step=self._generate_recommendation(confidence, reason, model_response),
        )

        logger.info(
            "handoff_created",
            handoff_id=handoff.id,
            tenant_id=self.tenant_config.id,
            confidence=confidence,
            reason=reason,
        )

        await self._persist_handoff(handoff, conversation_id=conversation_id)
        await self._enqueue_operator_notification(handoff)

        try:
            response = await self._send_to_ticketing_system(handoff)
            if response.ticket_id and response.ticket_id != str(handoff.id):
                await self._record_external_ref(handoff, response.ticket_id)
            return response
        except Exception as e:
            logger.error("handoff_ticketing_error", error=str(e))
            return HandoffResponse(
                success=False,
                message=f"Handoff recorded but ticketing system unavailable: {str(e)}",
            )

    async def _record_external_ref(self, handoff: HandoffRequest, ticket_id: str) -> None:
        """Persist the external ticket id back onto the escalation row."""
        if not self.escalation_repository:
            return
        try:
            await self.escalation_repository.update_status(
                str(handoff.id), "pending", external_ref=ticket_id
            )
        except Exception as e:
            logger.warning("handoff_external_ref_failed", handoff_id=handoff.id, error=str(e))

    async def _persist_handoff(
        self, handoff: HandoffRequest, conversation_id: str | None = None
    ) -> None:
        """Persist the escalation record (matrix item 8.1). Best-effort only."""
        if not self.escalation_repository:
            return
        try:
            await self.escalation_repository.add({
                "id": str(handoff.id),
                "tenant_id": str(self.tenant_config.id),
                "conversation_id": conversation_id,
                "session_id": handoff.session_id,
                "category": "general",
                "severity": "high" if handoff.confidence < 0.3 else "medium",
                "status": handoff.status,
                "reason": handoff.reason,
                "summary": handoff.user_message[:500],
                "details": {
                    "user_message": handoff.user_message,
                    "model_response": handoff.model_response,
                    "confidence": handoff.confidence,
                    "policy_action": handoff.policy_action.name if handoff.policy_action else None,
                    "recommended_next_step": handoff.recommended_next_step,
                    "attempted_resolutions": handoff.attempted_resolutions,
                },
                "channel": self.helpdesk_integration,
                "created_at": handoff.created_at,
            })
            logger.info("handoff_persisted", handoff_id=handoff.id)
        except Exception as e:
            logger.warning("handoff_persist_failed", handoff_id=handoff.id, error=str(e))

    def _generate_recommendation(
        self, confidence: float, reason: str, model_response: str | None
    ) -> str:
        if confidence < 0.3:
            return "Low confidence response - please review conversation context and provide accurate assistance."
        elif "policy" in reason.lower():
            return "Policy violation detected - review against tenant guidelines before responding."
        elif "blocked" in reason.lower():
            return "Content blocked by guardrails - verify if block was appropriate and respond accordingly."
        return "Standard escalation - continue conversation from where bot left off."

    async def _send_to_ticketing_system(self, handoff: HandoffRequest) -> HandoffResponse:
        # P9-3: concrete helpdesk adapters dispatch here (zendesk/jira/
        # servicenow); generic keeps the legacy webhook transport.
        from backend.app.adapters.ticketing import get_ticketing_adapter

        config = self._adapter_config()
        if config.adapter != "generic":
            adapter = get_ticketing_adapter(config, transport=self._ticketing_transport)
            if adapter is not None:
                payload = handoff.to_ticket_payload()
                result = await adapter.create_ticket(payload)
                if hasattr(adapter, "close"):
                    await adapter.close()
                if result.ok:
                    return HandoffResponse(
                        success=True,
                        ticket_id=result.ticket_id or str(handoff.id),
                        assignment_url=result.url,
                        message=f"Ticket created: {result.ticket_id}",
                        estimated_wait_time_minutes=15,
                    )
                raise Exception(
                    f"Ticketing API error ({config.adapter}): {result.error}"
                )
            logger.warning(
                "helpdesk_adapter_unconfigured",
                adapter=config.adapter,
                handoff_id=handoff.id,
            )
            return HandoffResponse(
                success=True,
                message="Handoff recorded locally. No external ticketing system configured.",
                estimated_wait_time_minutes=None,
            )

        if not self.ticketing_webhook_url:
            logger.warning("no_ticketing_integration", handoff_id=handoff.id)
            return HandoffResponse(
                success=True,
                message="Handoff recorded locally. No external ticketing system configured.",
                estimated_wait_time_minutes=None,
            )

        import aiohttp

        payload = handoff.to_ticket_payload()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.ticketing_webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status in (200, 201):
                        result = await response.json()
                        ticket_id = result.get("ticket_id", str(handoff.id))
                        return HandoffResponse(
                            success=True,
                            ticket_id=ticket_id,
                            message=f"Ticket created: {ticket_id}",
                            estimated_wait_time_minutes=15,
                        )
                    error_text = await response.text()
                    raise Exception(f"Ticketing API error: {error_text}")
        except Exception as e:
            logger.error("ticketing_webhook_error", error=str(e))
            raise

    def format_handoff_message(self, handoff: HandoffRequest) -> str:
        return (
            "I'm connecting you with a human specialist who can better assist you. "
            "Please hold on for a moment. "
            f"(Reference ID: {handoff.id})"
        )


_escalation_service: EscalationService | None = None


def get_escalation_service(
    tenant_config: TenantConfig,
    ticketing_webhook_url: str | None = None,
    helpdesk_integration: str | None = None,
    escalation_repository: Any | None = None,
    notification_channel: str | None = None,
    notification_target: str | None = None,
    ticketing_config: Any | None = None,
    ticketing_transport: Any | None = None,
) -> EscalationService:
    global _escalation_service
    if _escalation_service is None:
        _escalation_service = EscalationService(
            tenant_config=tenant_config,
            ticketing_webhook_url=ticketing_webhook_url,
            helpdesk_integration=helpdesk_integration,
            escalation_repository=escalation_repository,
            notification_channel=notification_channel,
            notification_target=notification_target,
            ticketing_config=ticketing_config,
            ticketing_transport=ticketing_transport,
        )
    return _escalation_service


def create_escalation_service(
    tenant_config: TenantConfig,
    ticketing_webhook_url: str | None = None,
    helpdesk_integration: str | None = None,
    escalation_repository: Any | None = None,
    notification_channel: str | None = None,
    notification_target: str | None = None,
    ticketing_config: Any | None = None,
    ticketing_transport: Any | None = None,
) -> EscalationService:
    return EscalationService(
        tenant_config=tenant_config,
        ticketing_webhook_url=ticketing_webhook_url,
        helpdesk_integration=helpdesk_integration,
        escalation_repository=escalation_repository,
        notification_channel=notification_channel,
        notification_target=notification_target,
        ticketing_config=ticketing_config,
        ticketing_transport=ticketing_transport,
    )
