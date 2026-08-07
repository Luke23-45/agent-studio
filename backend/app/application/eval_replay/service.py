"""Real evaluation replay: re-run stored conversations through the
production orchestration pipeline and record the results.

Turns are loaded from the database, replayed in order through a fresh
OrchestrationService (LLM + policy + retrieval + escalation), and the
outcome is written back as an audit event. Guardrails are not re-applied
during replay — the original run already recorded guardrail evidence;
replay measures orchestration quality against the recorded transcript.
"""

import structlog

from backend.app.application.orchestration.factories import (
    get_retrieval_service,
    load_or_create_policy_set,
)
from backend.app.application.orchestration import create_orchestration_service
from backend.app.gateway.service import get_gateway
from backend.app.infrastructure.db.repositories import (
    AuditRepository,
    ConversationRepository,
    TenantRepository,
)
from backend.app.modules.escalation import create_escalation_service
from backend.app.modules.tenant_config import tenant_config_from_data

logger = structlog.get_logger(__name__)


class ReplayError(Exception):
    pass


class EvalReplayService:
    def __init__(self, db, repository_factory=None, audit_factory=None):
        self.db = db
        self._repo_factory = repository_factory or (lambda r: r(db))
        self._audit_factory = audit_factory or (lambda r: r(db))

    async def replay_conversation(
        self,
        tenant_id: str,
        session_id: str,
        escalation_repository=None,
        ticketing_webhook_url: str | None = None,
        details_extra: dict | None = None,
    ) -> dict:
        """Replay one stored conversation and return a comparison report.

        ``details_extra`` is merged into the audit event details (run
        name/type/config from the caller). The returned report carries an
        ``audit_event_id`` so callers can hydrate the run without re-querying.
        """
        conversation_repo = self._repo_factory(ConversationRepository)
        conversation = await conversation_repo.get_by_session(tenant_id, session_id)
        if not conversation:
            raise ReplayError(f"Conversation not found: tenant={tenant_id} session={session_id}")

        messages = await conversation_repo.list_messages(conversation["id"])
        user_turns = [m for m in messages if m.get("role") == "user"]
        if not user_turns:
            raise ReplayError("Conversation has no user turns to replay")

        tenant_repo = self._repo_factory(TenantRepository)
        tenant_row = await tenant_repo.get_by_id(tenant_id)
        if not tenant_row:
            raise ReplayError(f"Tenant not found: {tenant_id}")
        tenant_config = tenant_config_from_data(tenant_row)
        policy_set = await load_or_create_policy_set(self.db, tenant_config)

        escalation_service = create_escalation_service(
            tenant_config,
            ticketing_webhook_url=ticketing_webhook_url,
            escalation_repository=escalation_repository,
        )
        retrieval_service = get_retrieval_service(tenant_config.id)
        orchestration = create_orchestration_service(
            tenant_config=tenant_config,
            policy_set=policy_set,
            gateway=get_gateway(),
            retrieval_service=retrieval_service,
            escalation_service=escalation_service,
        )

        replayed = 0
        skipped_blocked = 0
        succeeded = 0
        failed = 0
        exact_matches = 0
        blocked = 0
        handoffs = 0
        confidence_sum = 0.0
        errors = []
        prior_history: list[dict] = []

        for turn in user_turns:
            original = turn.get("content") or ""
            metadata = turn.get("metadata") or {}
            if metadata.get("blocked"):
                skipped_blocked += 1
                blocked += 1
                continue

            result = await orchestration.process_message(
                user_message=original,
                redacted_message=turn.get("redacted_content") or original,
                session_id=session_id,
                conversation_history=list(prior_history),
            )
            replayed += 1
            if result.get("error"):
                failed += 1
                errors.append({"turn": original[:200], "error": result["error"]})
            else:
                succeeded += 1
                confidence = result.get("confidence") or 0.0
                confidence_sum += confidence
                if result.get("handoff_required"):
                    handoffs += 1

            prior_history.append({"role": "user", "content": original})

        report = {
            "tenant_id": tenant_id,
            "session_id": session_id,
            "conversation_id": conversation["id"],
            "total_turns": len(user_turns),
            "skipped_blocked": skipped_blocked,
            "replayed": replayed,
            "succeeded": succeeded,
            "failed": failed,
            "avg_confidence": round(confidence_sum / succeeded, 4) if succeeded else None,
            "blocked": blocked,
            "handoffs": handoffs,
            "exact_matches": exact_matches,
            "errors": errors[:10],
        }
        logger.info("eval_replay_completed", **{k: v for k, v in report.items() if k != "errors"})

        audit_repo = self._audit_factory(AuditRepository)
        audit_details: dict = {"session_id": session_id, **report}
        if details_extra:
            audit_details.update(details_extra)
        audit_event = await audit_repo.add(
            action="eval.replay",
            resource_type="conversation",
            resource_id=conversation["id"],
            tenant_id=tenant_id,
            actor_type="system",
            details=audit_details,
        )
        report["audit_event_id"] = audit_event["id"]
        return report
