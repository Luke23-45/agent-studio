"""
API routes for the Neryva Agent Studio backend.

Provides REST endpoints for conversations, tenants, policies, API keys,
audit events, and escalations, with RBAC + tenant scoping (matrix 1.1/1.2/1.11).

Persistence contract (matrix 0.1/3.1): the database is the source of truth
for tenants and policies at request time. JSON files are only a write-side
cache for the tenant config service and are never consulted on the hot path.
"""

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.app.adapters.dlp import get_pii_service
from backend.app.adapters.tracing import get_langfuse_adapter
from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    generate_api_key,
    get_principal,
    get_rate_limiter,
    require_permission,
)
from backend.app.api.dependencies.session import (
    ConversationPrincipal,
    get_conversation_principal,
)
from backend.app.application.compaction import (
    CompactionService,
    LLMSummaryGenerator,
)
from backend.app.application.orchestration import create_orchestration_service
from backend.app.application.retrieval import create_retrieval_service
from backend.app.application.validation.streaming import StreamingModerationWindow
from backend.app.context import ContextTurn, TokenEstimator
from backend.app.domain.policy import PolicySet
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.admission import (
    AdmissionLimitExceeded,
    get_admission_gate,
)
from backend.app.gateway.service import GatewayBackedAdapter, get_gateway
from backend.app.gateway.types import (
    GatewayChainExhausted,
    GatewayConfigurationError,
    GatewayError,
    GatewayQuotaExceeded,
)
from backend.app.governance.budgets import (
    budget_rejection_message,
    build_gateway_budget_cfg,
)
from backend.app.governance.compiled import ConfigValidationError, compile_surface_config
from backend.app.governance.evidence import build_tool_gate_packet
from backend.app.governance.isolation import (
    resolve_tenant_context,
    trace_tags,
)
from backend.app.governance.promotion import promotion_gate, validate_canary_percent
from backend.app.governance.toolgate import build_authorizer
from backend.app.infrastructure.db import (
    ApiKeyRepository,
    AuditRepository,
    ConversationRepository,
    EndUserRepository,
    EscalationRepository,
    EvidenceRepository,
    PolicyRepository,
    SurfaceRepository,
    TenantConfigVersionRepository,
    TenantRepository,
    ThreadRepository,
    get_database_manager,
)
from backend.app.infrastructure.stream import get_stream_buffer
from backend.app.modules.escalation import create_escalation_service
from backend.app.modules.guardrails import get_guardrails_service
from backend.app.modules.rag import create_rag_service
from backend.app.modules.tenant_config import (
    get_tenant_config_service,
    policy_set_from_db,
    tenant_config_from_data,
)
from backend.app.modules.webhooks import (
    EVENT_CONVERSATION_COMPLETED,
    EVENT_CONVERSATION_CREATED,
    EVENT_ESCALATION_RAISED,
    EVENT_EVAL_FAILED,
    EVENT_GUARDRAIL_BLOCKED,
)
from backend.app.session.coordinator import CoordinatorBusy, get_thread_coordinator
from backend.app.session.hot_tier import get_thread_tail_cache
from backend.app.session.limits import get_end_user_limits
from backend.app.settings.env import settings
from backend.app.settings.feature_flags import feature_flags

logger = structlog.get_logger(__name__)

router = APIRouter()


# Request/Response models
class ConversationRequest(BaseModel):
    """Request model for conversation endpoint."""

    tenant_slug: str
    message: str
    session_id: str | None = None


class ConversationResponse(BaseModel):
    """Response model for conversation endpoint."""

    response: str
    confidence: float
    handoff_required: bool
    session_id: str
    citations: list[dict] = Field(default_factory=list)
    faithfulness: dict | None = None
    thread_id: str | None = None


class PrincipalResponse(BaseModel):
    """The authenticated principal for the current API key.

    Enables the admin console to render role-aware navigation and tenant
    scoping without exposing any secrets.
    """

    key_id: str
    name: str
    role: str
    tenant_id: str | None
    scopes: list[str]
    auth_enabled: bool


class ProviderKeyRequest(BaseModel):
    """Set/rotate a tenant provider credential (Arch 6.3.9, P0-8)."""

    api_key: str = Field(min_length=1, max_length=4096)
    key_source: str = Field(default="tenant-owned")


class ProviderKeyResponse(BaseModel):
    """Provider key metadata; the secret never leaves the backend."""

    tenant_id: str
    provider: str
    key_source: str
    key_version: int
    updated_at: datetime


class ConfigVersionRequest(BaseModel):
    """New immutable tenant config version (Arch 12, P0-11)."""

    config: dict


class ConfigVersionResponse(BaseModel):
    """Tenant config version metadata (never the full config payload)."""

    tenant_id: str
    version: int
    status: str
    validation_status: str = ""
    eval_status: str = ""
    canary_percent: int | None = None
    published_at: datetime | None
    promoted_by: str | None


class ConfigEvalRequest(BaseModel):
    """Eval-suite result on a draft (P5-2 pipeline; P6-6 slot)."""

    suite: str = Field(default="config", min_length=1, max_length=64)
    passed: bool
    details: dict[str, Any] | None = None


async def _load_effective_tenant_config(
    db: Any, tenant_row: dict[str, Any], request_key: str | None = None
) -> TenantConfig:
    """Runtime reads the published config version (Arch 12, P0-11).

    The tenant row is the base; a published config version (if any)
    overrides it, so publish/rollback change runtime behavior without
    touching the tenant row. No published version -> the row is
    authoritative. Shared with the background worker (P2-5); ``request_key``
    (end-user/session) resolves a canary rollout (P5-2).
    """
    from backend.app.application.compaction.refresh import (
        load_effective_tenant_config as _shared_load,
    )

    return await _shared_load(db, tenant_row, request_key)


def _assert_conversation_tenant(
    principal: ConversationPrincipal, tenant_config: TenantConfig
) -> None:
    """Session tokens are scoped to exactly one tenant; API keys RBAC-checked."""
    if principal.tenant_id and principal.tenant_id != str(tenant_config.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="principal is scoped to a different tenant",
        )


async def _bind_thread(
    db: Any,
    tenant_config: TenantConfig,
    conversation_id: str,
    principal: ConversationPrincipal,
    surface_id: str | None = None,
) -> dict[str, Any]:
    """Bind the durable thread to the conversation (Arch 7.1, P1-10)."""
    return await ThreadRepository(db).get_or_create_for_conversation(
        str(tenant_config.id),
        conversation_id,
        surface_id=surface_id or principal.surface_id,
        end_user_id=principal.end_user_id,
    )


async def _resolve_request_surface(
    db: Any, tenant_config: TenantConfig, principal: ConversationPrincipal
) -> dict[str, Any] | None:
    """Resolve + validate the request surface (Arch 6.1, P1-9).

    Session-token requests resolve the token's surface (or the tenant's
    default when the token carries none) and DENY when no active surface
    exists (P0-4 default-deny). API-key requests are unconstrained.
    """
    from backend.app.api.routes.surfaces import resolve_surface

    if not principal.is_session:
        return None
    surface = await resolve_surface(str(tenant_config.id), principal.surface_id)
    if surface is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active surface configured for this request",
        )
    return surface


async def _build_governance_wiring(
    db: Any,
    tenant_config: TenantConfig,
    surface: dict[str, Any] | None,
    session_id: str,
    conversation_id: str,
    evidence_repo: EvidenceRepository,
) -> tuple[Callable[[dict[str, Any]], Any], dict[str, Any]]:
    """P5-1/P5-3/P5-7: compile the surface config into the runtime hooks.

    Returns ``(tool_authorizer, gateway_cfg)`` for the orchestration:
    - the deterministic P5-3 tool gate (denials audited as evidence), and
    - the P5-7 budget-hierarchy ``quota_*_usd`` cfg built from the compiled
      surface budgets (P5-1), so the gateway's quota layer enforces
      platform > tenant > surface > end-user without route/loop changes.
    """
    compiled = compile_surface_config(
        tenant_config, surface=surface, version=getattr(tenant_config, "version", 1)
    )
    gateway_cfg = build_gateway_budget_cfg(compiled.budgets)

    async def _audit_denial(record: dict[str, Any]) -> None:
        packet = build_tool_gate_packet(
            tenant_id=str(tenant_config.id),
            session_id=session_id,
            tool_name=record.get("tool") or "",
            reason=record.get("reason"),
            conversation_id=conversation_id,
            surface_id=record.get("surface_id"),
        )
        try:
            await evidence_repo.add(packet.to_dict())
        except Exception as e:
            logger.warning("tool_denial_evidence_failed", error=str(e))

    gate = await build_authorizer(
        tenant_config=tenant_config,
        db=db,
        surface=surface,
        audit_service=_audit_denial,
    )

    async def _tool_authorizer(call: dict[str, Any]) -> Any:
        decision = await gate.authorize_async(call)
        return decision.allowed

    return _tool_authorizer, gateway_cfg


async def _refresh_hot_tail(
    db: Any,
    tenant_id: str,
    thread_id: str,
    summary: dict[str, Any] | None = None,
) -> None:
    """Push the durable tail into the Redis hot tier; never fatal.

    P5-4: the hot tier is a model-context fast path, so rows are stored
    redacted-only (raw ``content`` never leaves the access-controlled
    durable store).
    """
    try:
        rows = await ThreadRepository(db).read_tail(tenant_id, thread_id, limit=10)
        redacted_rows = [
            {**row, "content": row.get("redacted_content") or ""}
            for row in rows
        ]
        await get_thread_tail_cache().cache_tail(
            tenant_id, thread_id, redacted_rows, summary=summary
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("hot_tail_refresh_failed", thread_id=thread_id, error=str(e))


async def _enqueue_tool_clear(
    tenant_config: TenantConfig, thread_id: str
) -> bool:
    """Schedule the background tool-result reclaim (Arch 8.3, P2-7).

    Only for tenants with the feature enabled. Runs on turn completion
    (Arch 9.1 step 8) so reclaim is incremental, never on the request path;
    failures are logged and never break the turn (the render-time
    placeholder still protects context). Idempotency key keeps one pending
    job per thread.
    """
    if not tenant_config.features.get("clear_tool_results", False):
        return False
    from backend.app.application.clearing import JOB_TOOL_RESULT_CLEAR
    from backend.app.infrastructure.queue.manager import Job, get_queue_manager

    try:
        manager = get_queue_manager()
        await manager.enqueue(
            Job(
                type=JOB_TOOL_RESULT_CLEAR,
                payload={
                    "tenant_id": str(tenant_config.id),
                    "thread_id": thread_id,
                },
            ),
            idempotency_key=f"tool-clear:{thread_id}",
        )
        return True
    except Exception as e:  # pragma: no cover - queue outage path
        logger.warning("tool_clear_enqueue_failed", thread_id=thread_id, error=str(e))
        return False


def _build_memory_retriever(
    db: Any, tenant_config: TenantConfig, end_user_id: str | None
) -> Callable[[str], Awaitable[list[str]]]:
    """Build the top-k memory-facts hook (Arch 8.4, P2-8).

    Read scope is the end-user's own facts when the session has one
    (per-tenant, per-end-user store), otherwise tenant-global facts. The
    tenant controls the read bound via ``memory.max_facts``; retrieval
    never raises (the orchestrator swallows failures).
    """
    from backend.app.application.memory import memory_config, retrieve_facts

    limit = memory_config(tenant_config).max_facts

    async def _retriever(query: str) -> list[str]:
        return await retrieve_facts(
            db,
            str(tenant_config.id),
            end_user_id=end_user_id,
            query=query,
            limit=limit,
        )

    return _retriever


def _build_tool_callback(
    threads: ThreadRepository,
    tenant_id: str,
    thread_id: str,
    conversation_id: str,
    parent_message_id: str,
    request_id: str,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Build the orchestration tool-part persistence hook (P2-9).

    Each tool part (``tool_use`` then ``tool_result``, per executed call)
    is appended as its own assistant message in the durable thread log —
    first-class parts per turn (ties P5-3). Tool results are PII-redacted
    before storage when Presidio is enabled (the raw output never reaches
    the store unredacted); the loop never depends on the outcome (the
    orchestrator swallows callback failures).
    """

    async def _persist(part: dict[str, Any]) -> None:
        content = part.get("content") or {}
        redacted_content = content
        if feature_flags.ENABLE_PRESIDIO:
            payload_text = content.get("content")
            if isinstance(payload_text, str) and payload_text:
                try:
                    pii_result = get_pii_service().process_message(payload_text)
                    redacted_content = {
                        **content,
                        "content": pii_result.redacted_text,
                    }
                except RuntimeError as e:
                    logger.warning(
                        "tool_part_redaction_unavailable",
                        thread_id=thread_id,
                        error=str(e),
                    )
        await threads.append_message(
            tenant_id,
            thread_id,
            role="assistant",
            content="",
            redacted_content="",
            conversation_id=conversation_id,
            parent_message_id=parent_message_id,
            request_id=f"{request_id}:tool:{uuid4().hex[:8]}",
            extra_parts=[
                {
                    "part_type": part["part_type"],
                    "content": content,
                    "redacted_content": redacted_content,
                }
            ],
        )

    return _persist


async def _enqueue_memory_extract(
    tenant_config: TenantConfig, thread_id: str
) -> bool:
    """Schedule background memory extraction (Arch 8.4, P2-8).

    Only for tenants with the feature enabled. Runs on turn completion
    (Arch 9.1 step 8); failures are logged and never break the turn.
    Idempotency key keeps one pending job per thread; the extraction
    watermark makes re-runs free.
    """
    if not tenant_config.features.get("memory", False):
        return False
    from backend.app.application.memory import JOB_MEMORY_EXTRACT
    from backend.app.infrastructure.queue.manager import Job, get_queue_manager

    try:
        manager = get_queue_manager()
        await manager.enqueue(
            Job(
                type=JOB_MEMORY_EXTRACT,
                payload={
                    "tenant_id": str(tenant_config.id),
                    "thread_id": thread_id,
                },
            ),
            idempotency_key=f"memory-extract:{thread_id}",
        )
        return True
    except Exception as e:  # pragma: no cover - queue outage path
        logger.warning("memory_extract_enqueue_failed", thread_id=thread_id, error=str(e))
        return False


@router.get("/auth/me", response_model=PrincipalResponse)
async def auth_me(principal: ApiKeyPrincipal = Depends(get_principal)):
    """Return the principal resolved from the caller's API key."""
    return PrincipalResponse(
        key_id=principal.key_id,
        name=principal.name,
        role=principal.role,
        tenant_id=str(principal.tenant_id) if principal.tenant_id else None,
        scopes=principal.scopes,
        auth_enabled=settings.AUTH_ENABLED,
    )


class TenantCreateRequest(BaseModel):
    """Request model for creating a tenant."""

    name: str = Field(..., min_length=1)
    slug: str = Field(..., min_length=1)
    allowed_topics: list[str] = Field(default_factory=list)
    blocked_topics: list[str] = Field(default_factory=list)
    escalation_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    # P5-12: region pinned at onboarding (None = platform default).
    region: str | None = None
    # P5-9: per-tenant retention window (days); None = default posture.
    retention_days: int | None = Field(default=None, ge=1)


class TenantResponse(BaseModel):
    """Response model for tenant."""

    id: str
    name: str
    slug: str
    allowed_topics: list[str]
    blocked_topics: list[str]
    escalation_threshold: float
    region: str | None = None
    retention_days: int | None = None


class ApiKeyCreateRequest(BaseModel):
    """Request model for creating an API key."""

    name: str = Field(..., min_length=1)
    role: str = Field(default="operator")
    tenant_id: str | None = None
    expires_at: datetime | None = None


class ApiKeyResponse(BaseModel):
    """Response model for a created API key. `key` is only shown once."""

    id: str
    name: str
    role: str
    tenant_id: str | None
    prefix: str
    key: str
    expires_at: datetime | None
    created_at: datetime


class ApiKeyListItem(BaseModel):
    """API key list item (never exposes the raw key)."""

    id: str
    name: str
    role: str
    tenant_id: str | None
    prefix: str
    revoked: bool
    expires_at: datetime | None
    last_used_at: datetime | None
    usage_count: int
    created_at: datetime


class AuditEventResponse(BaseModel):
    """Response model for audit events."""

    id: str
    tenant_id: str | None
    actor_type: str
    actor_id: str | None
    action: str
    resource_type: str
    resource_id: str | None
    details: dict
    created_at: datetime


class EscalationResponse(BaseModel):
    """Response model for escalation records."""

    id: str
    tenant_id: str
    conversation_id: str | None
    session_id: str | None
    category: str
    severity: str
    status: str
    reason: str
    summary: str
    details: dict
    channel: str
    external_ref: str | None
    created_at: datetime


@router.post("/conversations", response_model=ConversationResponse)
async def process_conversation(
    request: ConversationRequest,
    request_ctx: Request,
    principal: ConversationPrincipal = Depends(get_conversation_principal),
):
    """Process a conversation message.

    Idempotent when the client sends an ``Idempotency-Key`` header: the
    first response is stored and replayed verbatim for repeat requests
    (matrix 1.7). The user turn is also written to the durable thread
    with the key as its request_id, so a lost response can never
    double-write (P1-2 dedup).
    """
    idempotency_key = request_ctx.headers.get("Idempotency-Key")
    if idempotency_key:
        from backend.app.infrastructure.patterns.idempotency import get_idempotency_guard

        cached = await get_idempotency_guard().get_response(
            idempotency_key, scope=f"conv:{request.tenant_slug}"
        )
        if cached is not None:
            logger.info(
                "conversation_idempotent_replay",
                tenant_slug=request.tenant_slug,
                idempotency_key=idempotency_key,
            )
            return JSONResponse(status_code=cached["status_code"], content=cached["body"])

    logger.info("conversation_request", tenant_slug=request.tenant_slug)
    session_id = request.session_id or str(uuid4())

    # Load tenant from the database (source of truth at request time)
    db = get_database_manager()
    tenant_repo = TenantRepository(db)
    tenant_row = await tenant_repo.get_by_slug(request.tenant_slug)

    if not tenant_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {request.tenant_slug}",
        )

    tenant_config = await _load_effective_tenant_config(
        db, tenant_row, request_key=principal.end_user_id or session_id
    )
    _assert_conversation_tenant(principal, tenant_config)

    # Langfuse tracing for the request pipeline (flag + credentials gated)
    trace = None
    adapter = None
    if feature_flags.ENABLE_LANGFUSE_TRACING:
        adapter = get_langfuse_adapter()
        if adapter.is_enabled:
            ctx = resolve_tenant_context(
                tenant_id=str(tenant_config.id),
                end_user_id=principal.end_user_id,
            )
            trace = adapter.trace(
                name="conversation.process",
                session_id=session_id,
                metadata={
                    "tenant_id": str(tenant_config.id),
                    "tenant_slug": request.tenant_slug,
                },
                tags=list(trace_tags(ctx).values()),
            )

    # Distributed rate limiting: per-tenant and per-model windows in
    # addition to the per-key window already enforced in get_principal.
    await get_rate_limiter().acquire_multi_or_raise(
        [
            f"tenant:{tenant_config.id}",
            f"model:{tenant_config.default_provider}:{tenant_config.default_model}",
        ]
    )

    # Load the tenant's published policy set from the database. An empty set
    # is persisted on first use so policy state survives restarts and is
    # never an in-memory-only artifact.
    policy_set = await _load_or_create_policy_set(db, tenant_config)

    conversation_repo = ConversationRepository(db)
    conversation = await conversation_repo.get_or_create(str(tenant_config.id), session_id)
    if conversation.get("created"):
        await _publish_webhook_event(
            EVENT_CONVERSATION_CREATED,
            tenant_config,
            {"session_id": session_id, "conversation_id": conversation["id"]},
            session_id,
        )

    if conversation["status"] == "paused":
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "Conversation is paused (human-in-the-loop hold); "
                "resume it before sending new messages."
            ),
        )

    # Durable thread (Arch 7.1): the append-only log is the source of truth
    # for history; the conversation row remains the v1 projection.
    threads = ThreadRepository(db)
    surface = await _resolve_request_surface(db, tenant_config, principal)
    thread = await _bind_thread(
        db, tenant_config, conversation["id"], principal,
        surface_id=surface["id"] if surface else None,
    )
    request_id = idempotency_key or f"req-{uuid4()}"

    # PII redaction (fail-closed: refuse to process when Presidio is enabled
    # but unavailable)
    if feature_flags.ENABLE_PRESIDIO:
        try:
            pii_service = get_pii_service()
        except RuntimeError as e:
            logger.error("pii_service_unavailable", error=str(e))
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(e),
            )
        pii_result = pii_service.process_message(request.message)
        redacted_message = pii_result.redacted_text
    else:
        pii_result = None
        redacted_message = request.message

    user_turn = await threads.append_message(
        str(tenant_config.id),
        thread["id"],
        role="user",
        content=request.message,
        redacted_content=redacted_message,
        conversation_id=conversation["id"],
        request_id=request_id,
        end_user_id=principal.end_user_id,
    )
    await conversation_repo.add_message(
        conversation["id"], "user", request.message,
        redacted_content=redacted_message,
    )

    # Guardrails check (tenant-scoped, lazily initialized and cached)
    guardrails_service = await get_guardrails_service(tenant_config)
    evidence_repo = EvidenceRepository(db)
    guardrails_service.evidence_callback = (
        lambda record: evidence_repo.add(record)
    )
    guardrails_span = (
        adapter.create_span(
            trace,
            "guardrails.input_validation",
            input={"message": redacted_message},
        )
        if trace
        else None
    )
    input_validation = await guardrails_service.validate_input(
        redacted_message,
        tenant_config,
        conversation_id=conversation["id"],
        session_id=session_id,
    )
    if guardrails_span:
        guardrails_span.end(
            output={
                "valid": input_validation["is_valid"],
                "blocked": input_validation["blocked"],
                "violations": input_validation["violations"],
            }
        )

    if not input_validation["is_valid"] and input_validation["blocked"]:
        logger.info(
            "conversation_blocked_by_guardrails",
            tenant_slug=request.tenant_slug,
            violations=input_validation["violations"],
        )
        await conversation_repo.add_message(
            conversation["id"], "assistant",
            "I cannot help with that request.",
            metadata={"blocked": True, "violations": input_validation["violations"]},
        )
        await threads.append_message(
            str(tenant_config.id),
            thread["id"],
            role="assistant",
            content="I cannot help with that request.",
            redacted_content="I cannot help with that request.",
            conversation_id=conversation["id"],
            parent_message_id=user_turn["id"],
            metadata={"blocked": True, "violations": input_validation["violations"]},
        )
        await _refresh_hot_tail(db, str(tenant_config.id), thread["id"])
        if trace:
            adapter.update(
                trace,
                output={"response": "I cannot help with that request.", "blocked": True},
            )
            adapter.flush()
        blocked_response = ConversationResponse(
            response="I cannot help with that request.",
            confidence=1.0,
            handoff_required=False,
            session_id=session_id,
            thread_id=thread["id"],
        )
        if idempotency_key:
            await _store_idempotent_response(
                idempotency_key, f"conv:{request.tenant_slug}", blocked_response
            )
        await _publish_webhook_event(
            EVENT_GUARDRAIL_BLOCKED,
            tenant_config,
            {"session_id": session_id, "violations": input_validation["violations"]},
            session_id,
        )
        return blocked_response

    # Model catalog enforcement: allowlist, fallback, cost ceiling (5.2)
    provider, model_override = await _resolve_catalog_model(
        tenant_config, request.message
    )
    if surface and surface.get("model_pin"):
        pin = surface["model_pin"]
        if ":" in pin:
            pin_provider, pin_model = pin.split(":", 1)
            provider, model_override = pin_provider.strip(), pin_model.strip()
        else:
            model_override = pin.strip()

    # P3-9: the gateway resolves provider keys internally; the route never
    # sees or handles a key.
    gateway = get_gateway()

    escalation_repository = EscalationRepository(db)
    escalation_service = create_escalation_service(
        tenant_config,
        ticketing_webhook_url=(
            settings.TICKETING_WEBHOOK_URL if feature_flags.ENABLE_HUMAN_HANDOFF else None
        ),
        escalation_repository=escalation_repository,
    )

    retrieval_service = _get_retrieval_service(tenant_config.id)
    history_turns = await _load_history_turns(
        threads, thread["id"], str(tenant_config.id), exclude_latest=True
    )
    context_summary, context_summary_position, context_summary_layers = (
        await _load_thread_summary(threads, thread["id"], str(tenant_config.id))
    )

    # P4-9: the non-streaming path runs full-output validation inside the
    # orchestration's bounded verify loop (same gate the streaming path's
    # rolling window uses), then returns; a rejected response re-asks
    # ``max_verify_retries`` times and escalates (Arch 9.2).
    if feature_flags.ENABLE_PRESIDIO:
        async def _output_validator(text: str):
            return await guardrails_service.evaluate_output(
                text,
                tenant_config=tenant_config,
                conversation_id=conversation["id"],
                session_id=session_id,
            )

    else:
        async def _output_validator(text: str):
            return None

    tool_authorizer, gateway_cfg = await _build_governance_wiring(
        db,
        tenant_config,
        surface,
        session_id=session_id,
        conversation_id=conversation["id"],
        evidence_repo=evidence_repo,
    )
    orchestration = create_orchestration_service(
        tenant_config=tenant_config,
        policy_set=policy_set,
        gateway=gateway,
        retrieval_service=retrieval_service,
        escalation_service=escalation_service,
        model_override=model_override,
        memory_retriever=_build_memory_retriever(
            db, tenant_config, principal.end_user_id
        ),
        surface_id=surface["id"] if surface else None,
        end_user_id=principal.end_user_id,
        tool_authorizer=tool_authorizer,
        gateway_cfg=gateway_cfg,
        output_validator=_output_validator,
    )
    orchestration.compaction_callback = _build_compaction_callback(
        db,
        threads,
        tenant_config,
        provider,
        model_override or tenant_config.default_model,
        thread,
    )
    orchestration.evidence_callback = lambda record: evidence_repo.add(record)
    orchestration.tool_callback = _build_tool_callback(
        threads,
        str(tenant_config.id),
        thread["id"],
        conversation["id"],
        user_turn["id"],
        request_id,
    )
    end_user_limits = get_end_user_limits()

    async def _usage(record: dict[str, Any]) -> None:
        # P3-5: spend_events is written exclusively by the gateway cost
        # ledger; this callback only tracks the end-user spend cap.
        if principal.end_user_id:
            await end_user_limits.record_spend(
                str(tenant_config.id), principal.end_user_id,
                float(record.get("input_tokens", 0) + record.get("output_tokens", 0)),
            )

    orchestration.usage_callback = _usage

    orchestration_span = (
        adapter.create_span(
            trace,
            "orchestration",
            input={
                "message": redacted_message,
                "history_turns": len(history_turns),
            },
            metadata=(
                {"surface_id": surface["id"]}
                if surface
                else None
            ),
        )
        if trace
        else None
    )

    # Session coordinator (Arch 7.2, P1-3): at most one in-flight
    # generation per thread; concurrent messages queue in sequence order.
    try:
        thread_lease = await get_thread_coordinator().acquire(
            str(tenant_config.id), thread["id"]
        )
    except CoordinatorBusy:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "15"},
            detail="Another message is being processed for this thread. Try again shortly.",
        ) from None

    # Admission control (Arch 10): bound concurrent in-flight generations
    # per tenant and platform-wide; excess requests get 429 + Retry-After.
    admission_gate = get_admission_gate()
    try:
        admission_handle = await admission_gate.admit(tenant_config.id)
    except AdmissionLimitExceeded as e:
        await thread_lease.release()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": str(int(e.retry_after))},
            detail="Concurrent generation limit exceeded. Try again shortly.",
        )

    # Process message
    try:
        result = await orchestration.process_message(
            user_message=request.message,
            redacted_message=redacted_message,
            session_id=session_id,
            conversation_history=history_turns,
            context={"conversation_id": conversation["id"]},
            context_summary=context_summary,
            context_summary_position=context_summary_position,
            context_summary_layers=context_summary_layers,
        )
        if orchestration_span:
            orchestration_span.end(
                output={
                    "response": result.get("model_response"),
                    "confidence": result.get("confidence"),
                    "policy_action": str(result.get("policy_action")),
                    "error": result.get("error"),
                }
            )

        if result.get("error"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result["error"],
            )

        # P4-9: output validation already ran inside the orchestration's
        # bounded verify loop. ``model_response`` is the released text
        # (PII-redacted when the validator applied redaction); the raw
        # model output is preserved in ``result["context"]["output_raw"]``
        # for the durable log. A final rejection (retries exhausted)
        # escalates and returns a withholding notice instead of the
        # rejected bytes.
        response_raw = (
            (result.get("context") or {}).get("output_raw")
            or result.get("model_response") or ""
        )
        response_text = result.get("model_response") or ""
        handoff_required = bool(result.get("handoff_required", False))
        output_validation = (result.get("context") or {}).get("output_validation") or {}
        output_blocked = bool(output_validation.get("checked")) and not bool(
            output_validation.get("allowed", True)
        )
        if output_blocked and handoff_required:
            # The response failed every bounded re-ask; never release the
            # rejected content — escalate instead (Arch 9.3).
            response_text = "[content withheld by compliance]"
            response_raw = response_text
            logger.warning(
                "conversation_output_blocked_escalated",
                tenant_slug=request.tenant_slug,
                decision=output_validation.get("decision"),
            )
        await conversation_repo.add_message(
            conversation["id"], "assistant", response_raw,
            redacted_content=response_text,
            metadata={
                "confidence": result.get("confidence", 0.0),
                "handoff_required": handoff_required,
            },
        )
        await threads.append_message(
            str(tenant_config.id),
            thread["id"],
            role="assistant",
            content=response_raw,
            redacted_content=response_text,
            conversation_id=conversation["id"],
            parent_message_id=user_turn["id"],
            metadata={
                "confidence": result.get("confidence", 0.0),
                "handoff_required": handoff_required,
            },
        )
        await _refresh_hot_tail(db, str(tenant_config.id), thread["id"])
        await _enqueue_tool_clear(tenant_config, thread["id"])
        await _enqueue_memory_extract(tenant_config, thread["id"])
        if handoff_required:
            await conversation_repo.mark_escalated(conversation["id"])
            await _publish_webhook_event(
                EVENT_ESCALATION_RAISED,
                tenant_config,
                {"session_id": session_id, "conversation_id": conversation["id"]},
                session_id,
            )

        if trace:
            adapter.update(
                trace,
                output={
                    "response": response_text,
                    "confidence": result.get("confidence", 0.0),
                    "handoff_required": handoff_required,
                },
            )
            adapter.flush()

        response = ConversationResponse(
            response=response_text,
            confidence=result.get("confidence", 0.0),
            handoff_required=handoff_required,
            session_id=session_id,
            citations=result.get("citations") or [],
            faithfulness=result.get("context", {}).get("faithfulness"),
            thread_id=thread["id"],
        )
        if idempotency_key:
            await _store_idempotent_response(
                idempotency_key, f"conv:{request.tenant_slug}", response
            )
        await _publish_webhook_event(
            EVENT_CONVERSATION_COMPLETED,
            tenant_config,
            {
                "session_id": session_id,
                "handoff_required": handoff_required,
                "confidence": result.get("confidence", 0.0),
            },
            session_id,
        )
        return response
    except HTTPException:
        if trace:
            adapter.flush()
        raise
    except GatewayError as e:
        if trace:
            adapter.update(trace, output={"error": str(e)})
            adapter.flush()
        await _publish_webhook_event(
            EVENT_EVAL_FAILED,
            tenant_config,
            {"session_id": session_id, "conversation_id": conversation["id"], "error": str(e)},
            session_id,
        )
        if isinstance(e, GatewayQuotaExceeded):
            # P5-7: surface-level rejection carries a human-readable budget
            # line (level + limit + projection) the widget renders distinctly.
            raise HTTPException(
                status_code=_gateway_http_status(e),
                detail=budget_rejection_message(e.level, e.limit_usd, e.projected_usd),
            ) from e
        raise HTTPException(
            status_code=_gateway_http_status(e),
            detail=str(e),
        ) from e
    except Exception as e:
        logger.error("conversation_error", error=str(e))
        if trace:
            adapter.update(trace, output={"error": str(e)})
            adapter.flush()
        await _publish_webhook_event(
            EVENT_EVAL_FAILED,
            tenant_config,
            {"session_id": session_id, "conversation_id": conversation["id"], "error": str(e)},
            session_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    finally:
        await admission_handle.release()
        await thread_lease.release()
        await thread_lease.release()


def _sse(event: str, data: dict, event_id: int | None = None) -> str:
    """Format a Server-Sent Events frame. ``event_id`` emits ``id:`` for
    ``Last-Event-ID`` reconnect replay (Arch 9.1, Phase 4)."""
    import json as _json

    header = f"id: {event_id}\n" if event_id is not None else ""
    return f"{header}event: {event}\ndata: {_json.dumps(data)}\n\n"


def _gateway_http_status(exc: GatewayError) -> int:
    """Map a gateway failure kind to an HTTP status (Arch 10, P3-9).

    - quota_exceeded -> 402 Payment Required (monthly USD budget levels)
    - gateway_configuration -> 503 (no usable deployment / missing key)
    - chain_exhausted -> 502 (every fallback target failed)
    - everything else -> 500
    """
    if isinstance(exc, GatewayQuotaExceeded):
        return status.HTTP_402_PAYMENT_REQUIRED
    if isinstance(exc, GatewayConfigurationError):
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if isinstance(exc, GatewayChainExhausted):
        return status.HTTP_502_BAD_GATEWAY
    return status.HTTP_500_INTERNAL_SERVER_ERROR


async def _store_idempotent_response(
    idempotency_key: str, scope: str, response: ConversationResponse
) -> None:
    """Memoize a write response under the client's Idempotency-Key."""
    from backend.app.infrastructure.patterns.idempotency import get_idempotency_guard

    await get_idempotency_guard().store_response(
        idempotency_key, 200, response.model_dump(), scope=scope
    )


async def _publish_webhook_event(
    event_type: str,
    tenant_config: TenantConfig,
    data: dict,
    session_id: str,
) -> None:
    """Publish a webhook event through the transactional outbox.

    Records an outbox row (idempotent by event id) and attempts an
    immediate drain; when the drain succeeds the webhook delivery jobs are
    enqueued as before. Failures must never break the request — the row
    stays pending and the worker retries it a later drain, so no event is
    lost on crash (EU-AI-Act Art. 12).
    """
    try:
        from uuid import uuid4 as _uuid4

        from backend.app.infrastructure.db import get_database_manager
        from backend.app.modules.webhooks.outbox import EventOutboxRepository
        from backend.app.modules.webhooks.relay import OutboxRelay, enqueue_outbox_relay

        db = get_database_manager()
        outbox = EventOutboxRepository(db)
        event_id = str(_uuid4())
        await outbox.record(
            event_id=event_id,
            tenant_id=str(tenant_config.id),
            event_type=event_type,
            payload=data,
            session_id=session_id,
        )
        try:
            await enqueue_outbox_relay()
        except Exception as _e:  # queue not ready: inline drain still works
            pass
        try:
            await OutboxRelay(db=db).drain(limit=50)
        except Exception:  # pragma: no cover - defensive
            pass
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("webhook_publish_failed", event_type=event_type, error=str(e))


def _jsonable(data: Any) -> Any:
    """JSON-roundtrip with ``default=str`` so datetimes serialize safely."""
    import json as _json

    return _json.loads(_json.dumps(data, default=str))


async def _resolve_catalog_model(
    tenant_config: TenantConfig, user_message: str
) -> tuple[str, str | None]:
    """Enforce the tenant's model catalog (allowlist, fallback, cost ceiling).

    Returns ``(provider, model_override)``. ``model_override`` is None when
    the tenant's default model is used. Empty catalog = unconstrained.
    """
    from backend.app.modules.model_catalog import (
        CostCeilingExceeded,
        ModelCatalogService,
        ModelNotAllowed,
    )

    service = ModelCatalogService()
    try:
        provider, model = await service.resolve_model(
            str(tenant_config.id),
            tenant_config.default_provider,
            tenant_config.default_model,
        )
    except ModelNotAllowed as e:
        logger.warning("model_catalog_denied", error=str(e))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(e))

    try:
        tokens = ModelCatalogService.estimate_tokens(user_message)
        await service.check_cost_ceiling(str(tenant_config.id), provider, model, tokens)
    except CostCeilingExceeded as e:
        logger.warning("model_cost_ceiling_exceeded", error=str(e))
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(e))

    model_override = model if model != tenant_config.default_model else None
    return provider, model_override


@router.post("/conversations/stream")
async def stream_conversation(
    request: ConversationRequest,
    request_ctx: Request,
    principal: ConversationPrincipal = Depends(get_conversation_principal),
):
    """Stream a conversation response over Server-Sent Events.

    Emits ``session``, ``delta`` (token-level), ``guardrails`` and ``result``
    (or ``error``) events. The response is generated via the LLM's
    ``stream_chat``; guardrails and policy checks run exactly as in the
    non-streaming path.
    """
    from fastapi.responses import StreamingResponse

    logger.info("conversation_stream_request", tenant_slug=request.tenant_slug)
    session_id = request.session_id or str(uuid4())
    idempotency_key = request_ctx.headers.get("Idempotency-Key")

    db = get_database_manager()
    tenant_repo = TenantRepository(db)
    tenant_row = await tenant_repo.get_by_slug(request.tenant_slug)
    if not tenant_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {request.tenant_slug}",
        )

    tenant_config = await _load_effective_tenant_config(
        db, tenant_row, request_key=principal.end_user_id or session_id
    )
    _assert_conversation_tenant(principal, tenant_config)

    await get_rate_limiter().acquire_multi_or_raise(
        [
            f"tenant:{tenant_config.id}",
            f"model:{tenant_config.default_provider}:{tenant_config.default_model}",
        ]
    )

    policy_set = await _load_or_create_policy_set(db, tenant_config)

    conversation_repo = ConversationRepository(db)
    conversation = await conversation_repo.get_or_create(str(tenant_config.id), session_id)
    if conversation.get("created"):
        await _publish_webhook_event(
            EVENT_CONVERSATION_CREATED,
            tenant_config,
            {"session_id": session_id, "conversation_id": conversation["id"]},
            session_id,
        )

    if conversation["status"] == "paused":
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "Conversation is paused (human-in-the-loop hold); "
                "resume it before sending new messages."
            ),
        )

    # Durable thread (Arch 7.1): append-only source of truth.
    threads = ThreadRepository(db)
    surface = await _resolve_request_surface(db, tenant_config, principal)
    thread = await _bind_thread(
        db, tenant_config, conversation["id"], principal,
        surface_id=surface["id"] if surface else None,
    )
    request_id = idempotency_key or f"req-{uuid4()}"
    stream_id = request_id

    # Phase 4 reconnect replay (Arch 9.1 step 9 + P4-5): a client that dropped
    # mid-stream and reconnects with ``Last-Event-ID`` replays the already-
    # buffered (validated) chunks plus the terminal event from the server-
    # side stream buffer — no duplicate user message, no re-generation.
    # Idempotency (P4-5): a retry that reuses ``Idempotency-Key`` after a
    # 5xx/interruption never duplicates — if the same request already produced
    # a terminal result, that buffer is replayed instead of calling the model.
    last_event_id = 0
    raw_last = request_ctx.headers.get("Last-Event-ID")
    if raw_last:
        try:
            last_event_id = int(raw_last)
        except (TypeError, ValueError):
            last_event_id = 0
    stream_buffer = get_stream_buffer()
    want_replay = last_event_id > 0
    idempotency_hit = False
    if idempotency_key and not want_replay:
        idempotency_hit = await stream_buffer.get_terminal(
            str(tenant_config.id), str(thread["id"]), stream_id
        ) is not None
        want_replay = idempotency_hit
    if want_replay:
        records = await stream_buffer.replay(
            str(tenant_config.id), str(thread["id"]), stream_id, after_event_id=last_event_id
        )

        async def _replay_generator():
            for record in records:
                seq = record["id"]
                if record["type"] == "delta":
                    yield _sse("delta", {"content": record["data"]["content"]}, event_id=seq)
                elif record["type"] == "result":
                    yield _sse("result", record["data"], event_id=seq)
                elif record["type"] == "error":
                    yield _sse("error", record["data"], event_id=seq)

        return StreamingResponse(
            _replay_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if feature_flags.ENABLE_PRESIDIO:
        try:
            pii_service = get_pii_service()
        except RuntimeError as e:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
        pii_result = pii_service.process_message(request.message)
        redacted_message = pii_result.redacted_text
    else:
        redacted_message = request.message

    user_turn = await threads.append_message(
        str(tenant_config.id),
        thread["id"],
        role="user",
        content=request.message,
        redacted_content=redacted_message,
        conversation_id=conversation["id"],
        request_id=request_id,
        end_user_id=principal.end_user_id,
    )
    await conversation_repo.add_message(
        conversation["id"], "user", request.message, redacted_content=redacted_message
    )

    guardrails_service = await get_guardrails_service(tenant_config)
    evidence_repo = EvidenceRepository(db)
    guardrails_service.evidence_callback = lambda record: evidence_repo.add(record)
    input_validation = await guardrails_service.validate_input(
        redacted_message,
        tenant_config,
        conversation_id=conversation["id"],
        session_id=session_id,
    )

    escalation_repository = EscalationRepository(db)
    escalation_service = create_escalation_service(
        tenant_config,
        ticketing_webhook_url=(
            settings.TICKETING_WEBHOOK_URL if feature_flags.ENABLE_HUMAN_HANDOFF else None
        ),
        escalation_repository=escalation_repository,
    )
    retrieval_service = _get_retrieval_service(tenant_config.id)
    history_turns = await _load_history_turns(
        threads, thread["id"], str(tenant_config.id), exclude_latest=True
    )
    context_summary, context_summary_position, context_summary_layers = (
        await _load_thread_summary(threads, thread["id"], str(tenant_config.id))
    )

    provider, model_override = await _resolve_catalog_model(tenant_config, request.message)
    if surface and surface.get("model_pin"):
        pin = surface["model_pin"]
        if ":" in pin:
            pin_provider, pin_model = pin.split(":", 1)
            provider, model_override = pin_provider.strip(), pin_model.strip()
        else:
            model_override = pin.strip()
    gateway = get_gateway()
    tool_authorizer, gateway_cfg = await _build_governance_wiring(
        db,
        tenant_config,
        surface,
        session_id=session_id,
        conversation_id=conversation["id"],
        evidence_repo=evidence_repo,
    )
    orchestration = create_orchestration_service(
        tenant_config=tenant_config,
        policy_set=policy_set,
        gateway=gateway,
        retrieval_service=retrieval_service,
        escalation_service=escalation_service,
        model_override=model_override,
        memory_retriever=_build_memory_retriever(
            db, tenant_config, principal.end_user_id
        ),
        surface_id=surface["id"] if surface else None,
        end_user_id=principal.end_user_id,
        tool_authorizer=tool_authorizer,
        gateway_cfg=gateway_cfg,
    )
    orchestration.compaction_callback = _build_compaction_callback(
        db,
        threads,
        tenant_config,
        provider,
        model_override or tenant_config.default_model,
        thread,
    )
    orchestration.evidence_callback = lambda record: evidence_repo.add(record)
    orchestration.tool_callback = _build_tool_callback(
        threads,
        str(tenant_config.id),
        thread["id"],
        conversation["id"],
        user_turn["id"],
        request_id,
    )
    end_user_limits = get_end_user_limits()

    async def _usage(record: dict[str, Any]) -> None:
        # P3-5: spend_events is written exclusively by the gateway cost
        # ledger; this callback only tracks the end-user spend cap.
        if principal.end_user_id:
            await end_user_limits.record_spend(
                str(tenant_config.id), principal.end_user_id,
                float(record.get("input_tokens", 0) + record.get("output_tokens", 0)),
            )

    orchestration.usage_callback = _usage

    stream_buffer = get_stream_buffer()

    # Phase 4: rolling-window output moderation (Arch 9.1 step 7). The window
    # is staged per request; deltas are held and validated before release.
    # The window size is a per-tenant latency/safety knob (P4-3, D-3):
    # ``tenant_config.stream_moderation_window_chars`` overrides the platform
    # default when set, so strict PII/policy tenants can hold larger windows
    # before release without a platform-wide change.
    moderation_window = (
        tenant_config.stream_moderation_window_chars
        or settings.STREAM_MODERATION_WINDOW_CHARS
    )
    if feature_flags.ENABLE_PRESIDIO:

        async def _stream_validator(text: str):
            return await guardrails_service.evaluate_output(
                text,
                tenant_config=tenant_config,
                conversation_id=conversation["id"],
                session_id=session_id,
            )

        moderation = StreamingModerationWindow(
            _stream_validator,
            window_size=moderation_window,
        )
    else:
        moderation = StreamingModerationWindow(None)

    async def event_generator():
        yield _sse("session", {"session_id": session_id, "thread_id": thread["id"]})
        yield _sse(
            "guardrails",
            {
                "valid": input_validation["is_valid"],
                "blocked": input_validation["blocked"],
                "violations": input_validation["violations"],
            },
        )
        if context_summary_position is not None:
            # P4-1: the client should know history was compacted before this
            # turn; the durable block replaces every turn <= position.
            yield _sse(
                "compaction",
                {
                    "position": context_summary_position,
                    "layer_count": len(context_summary_layers)
                    if context_summary_layers
                    else 0,
                },
            )

        if not input_validation["is_valid"] and input_validation["blocked"]:
            logger.info(
                "conversation_stream_blocked",
                tenant_slug=request.tenant_slug,
                violations=input_validation["violations"],
            )
            await conversation_repo.add_message(
                conversation["id"], "assistant",
                "I cannot help with that request.",
                metadata={"blocked": True, "violations": input_validation["violations"]},
            )
            await threads.append_message(
                str(tenant_config.id),
                thread["id"],
                role="assistant",
                content="I cannot help with that request.",
                redacted_content="I cannot help with that request.",
                conversation_id=conversation["id"],
                parent_message_id=user_turn["id"],
                metadata={"blocked": True, "violations": input_validation["violations"]},
            )
            await _refresh_hot_tail(db, str(tenant_config.id), thread["id"])
            yield _sse(
                "result",
                {
                    "response": "I cannot help with that request.",
                    "confidence": 1.0,
                    "handoff_required": False,
                    "thread_id": thread["id"],
                },
            )
            return

        # Session coordinator (Arch 7.2): per-thread serialization; the
        # queue waits in sequence order and reports an SSE error on timeout.
        try:
            thread_lease = await get_thread_coordinator().acquire(
                str(tenant_config.id), thread["id"]
            )
        except CoordinatorBusy:
            yield _sse(
                "error",
                {
                    "error": (
                        "Another message is being processed for this thread. "
                        "Retry in 15s."
                    )
                },
            )
            return

        # Admission control (Arch 10): bound concurrent in-flight
        # generations per tenant; SSE error + retry hint on excess.
        try:
            admission_handle = await get_admission_gate().admit(tenant_config.id)
        except AdmissionLimitExceeded as e:
            await thread_lease.release()
            yield _sse(
                "error",
                {
                    "error": (
                        "Concurrent generation limit exceeded. "
                        f"Retry in {int(e.retry_after)}s."
                    )
                },
            )
            return

        result: dict[str, Any] | None = None
        try:
            # Heartbeats (P4-1): the producer+consumer loop funnels upstream
            # events through an asyncio.Queue while a timer injects `heartbeat`
            # frames when the model is quiet — CDN/proxy idle timeouts stay
            # satisfied without delaying real deltas. Cancellation of the
            # consumer cancels the producer, which propagates into the
            # gateway stream (P4-4: no orphaned in-flight generation).
            from asyncio import Queue as _Queue

            stream_q: _Queue = asyncio.Queue()

            async def _produce() -> None:
                try:
                    async for event in orchestration.stream_message(
                        user_message=request.message,
                        redacted_message=redacted_message,
                        session_id=session_id,
                        conversation_history=history_turns,
                        context={"conversation_id": conversation["id"]},
                        context_summary=context_summary,
                        context_summary_position=context_summary_position,
                        context_summary_layers=context_summary_layers,
                    ):
                        await stream_q.put(("event", event))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # surface upstream failure in-band
                    await stream_q.put(("exception", exc))
                finally:
                    await stream_q.put(("done", None))

            async def _heartbeat_loop() -> None:
                try:
                    while True:
                        await asyncio.sleep(settings.STREAM_HEARTBEAT_SECONDS)
                        await stream_q.put(("heartbeat", None))
                except asyncio.CancelledError:
                    pass

            producer = asyncio.create_task(_produce())
            heartbeat_task = asyncio.create_task(_heartbeat_loop())
            last_released_event_id: int | None = None
            try:
                while True:
                    kind, payload = await stream_q.get()
                    if kind == "heartbeat":
                        yield _sse("heartbeat", {})
                        continue
                    if kind == "exception":
                        raise payload
                    if kind == "done":
                        break
                    event = payload
                    if event["type"] == "delta":
                        release = await moderation.push(event["content"])
                        if release is None:
                            continue  # held until a full window accumulates
                        if release.truncated:
                            # Output moderation flagged blocked content mid-
                            # stream: emit a redaction + retraction and
                            # truncate the stream (never release the tail to
                            # the client; the client drops deltas released
                            # after last_released_event_id).
                            violations = [
                                getattr(v, "to_dict", lambda: v)()
                                for v in release.violations
                            ]
                            await stream_buffer.mark_terminal(
                                str(tenant_config.id), str(thread["id"]), stream_id, "error",
                                {
                                    "error": "output_moderation_redaction",
                                    "reason": "content_moderation",
                                    "violations": violations,
                                },
                            )
                            yield _sse("redaction", {"violations": violations})
                            yield _sse(
                                "retraction",
                                {
                                    "violations": violations,
                                    "retract_from_event_id": last_released_event_id,
                                },
                            )
                            await _publish_webhook_event(
                                EVENT_GUARDRAIL_BLOCKED,
                                tenant_config,
                                {
                                    "reason": "content_moderation",
                                    "violations": violations,
                                },
                                session_id,
                            )
                            return
                        seq = await stream_buffer.append(
                            str(tenant_config.id), str(thread["id"]), stream_id, "delta",
                            {"content": release.text},
                        )
                        last_released_event_id = seq
                        yield _sse("delta", {"content": release.text}, event_id=seq)
                    elif event["type"] == "error":
                        await stream_buffer.mark_terminal(
                            str(tenant_config.id), str(thread["id"]), stream_id, "error",
                            {"error": event["error"]},
                        )
                        yield _sse("error", {"error": event["error"]})
                        break
                    else:
                        result = event
                        break
            finally:
                producer.cancel()
                heartbeat_task.cancel()

            if result is None:
                return

            # Flush whatever the window still holds before the result frame.
            tail = await moderation.finish()
            if tail is not None and tail.truncated:
                violations = [
                    getattr(v, "to_dict", lambda: v)()
                    for v in tail.violations
                ]
                await stream_buffer.mark_terminal(
                    str(tenant_config.id), str(thread["id"]), stream_id, "error",
                    {
                        "error": "output_moderation_redaction",
                        "reason": "content_moderation_tail",
                        "violations": violations,
                    },
                )
                yield _sse("redaction", {"violations": violations})
                yield _sse(
                    "retraction",
                    {
                        "violations": violations,
                        "retract_from_event_id": last_released_event_id,
                    },
                )
                await _publish_webhook_event(
                    EVENT_GUARDRAIL_BLOCKED,
                    tenant_config,
                    {
                        "reason": "content_moderation_tail",
                        "violations": violations,
                    },
                    session_id,
                )
                return
            if tail is not None and tail.text:
                seq = await stream_buffer.append(
                    str(tenant_config.id), str(thread["id"]), stream_id, "delta",
                    {"content": tail.text},
                )
                yield _sse("delta", {"content": tail.text}, event_id=seq)

            response_text = result["response"]
            response_raw = response_text
            if feature_flags.ENABLE_PRESIDIO and response_text:
                output_validation = await guardrails_service.evaluate_output(
                    response_text,
                    tenant_config=tenant_config,
                    conversation_id=conversation["id"],
                    session_id=session_id,
                )
                if output_validation.redacted_text:
                    response_text = output_validation.redacted_text

            handoff_required = bool(result.get("handoff_required", False))
            await conversation_repo.add_message(
                conversation["id"], "assistant", response_raw,
                redacted_content=response_text,
                metadata={
                    "confidence": result.get("confidence", 0.0),
                    "handoff_required": handoff_required,
                    "streamed": True,
                },
            )
            await threads.append_message(
                str(tenant_config.id),
                thread["id"],
                role="assistant",
                content=response_raw,
                redacted_content=response_text,
                conversation_id=conversation["id"],
                parent_message_id=user_turn["id"],
                metadata={
                    "confidence": result.get("confidence", 0.0),
                    "handoff_required": handoff_required,
                    "streamed": True,
                },
            )
            await _refresh_hot_tail(db, str(tenant_config.id), thread["id"])
            await _enqueue_tool_clear(tenant_config, thread["id"])
            await _enqueue_memory_extract(tenant_config, thread["id"])
            if handoff_required:
                await conversation_repo.mark_escalated(conversation["id"])
                await _publish_webhook_event(
                    EVENT_ESCALATION_RAISED,
                    tenant_config,
                    {"session_id": session_id, "conversation_id": conversation["id"]},
                    session_id,
                )

            result_payload: dict[str, Any] = {
                "response": response_text,
                "confidence": result.get("confidence", 0.0),
                "handoff_required": handoff_required,
                "citations": result.get("citations") or [],
                "faithfulness": result.get("faithfulness"),
                "thread_id": thread["id"],
            }
            # Turn completion (P4-7): fire the durable event contract exactly
            # as the non-streaming path does. Best-effort — a webhook outage
            # must never fail the SSE stream that already persisted the turn.
            await _publish_webhook_event(
                EVENT_CONVERSATION_COMPLETED,
                tenant_config,
                {
                    "session_id": session_id,
                    "streamed": True,
                    "handoff_required": handoff_required,
                    "confidence": result.get("confidence", 0.0),
                },
                session_id,
            )
            await stream_buffer.mark_terminal(
                str(tenant_config.id), str(thread["id"]), stream_id, "result", result_payload
            )
            yield _sse("result", result_payload)
        except HTTPException:
            raise
        except Exception as e:
            logger.error("conversation_stream_error", error=str(e))
            payload: dict[str, Any] = {"error": str(e)}
            if isinstance(e, GatewayError):
                payload["kind"] = e.kind
                payload["status"] = _gateway_http_status(e)
            if isinstance(e, GatewayQuotaExceeded):
                # P5-7: machine-readable budget fields for a surface-level
                # rejection, plus a widget-ready message.
                payload["error"] = budget_rejection_message(
                    e.level, e.limit_usd, e.projected_usd
                )
                payload["level"] = e.level
                payload["limit_usd"] = e.limit_usd
                payload["projected_usd"] = e.projected_usd
            await stream_buffer.mark_terminal(
                str(tenant_config.id), str(thread["id"]), stream_id, "error", payload
            )
            yield _sse("error", payload)
        finally:
            await admission_handle.release()
            await thread_lease.release()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _load_or_create_policy_set(db, tenant_config: TenantConfig) -> PolicySet:
    """Load the tenant's published policy set from the DB, creating an empty
    published set on first use so evaluation is always explicit."""
    policy_repo = PolicyRepository(db)
    row = await policy_repo.get_by_tenant(str(tenant_config.id))
    if row:
        return policy_set_from_db(row)
    await policy_repo.create_set(str(tenant_config.id), "default", rules=[])
    return PolicySet(tenant_id=tenant_config.id, name="default", rules=[])


async def _load_history_turns(
    threads: ThreadRepository,
    thread_id: str,
    tenant_id: str,
    exclude_latest: bool = True,
    limit: int = 200,
) -> list[ContextTurn]:
    """Load prior turns for session memory from the durable thread log.

    The latest message is the one currently being processed, so it is
    excluded by default. Serves the redacted column ONLY (Arch 8.1,
    P0-5): turns without redacted content are skipped (fail closed) --
    raw content never reaches the model context. The read window is a
    page size, not a model-visible slice: the SessionContextLoader
    trims to the model's token budget (P2-1).
    """
    rows = await threads.read_tail(tenant_id, thread_id, limit=limit + 1)
    if exclude_latest and rows:
        rows = rows[:-1]
    tool_seqs = await threads.list_tool_result_seqs(tenant_id, thread_id)
    return [
        ContextTurn(
            role=m["role"],
            content=m["redacted_content"] or "",
            seq=m.get("seq"),
            message_id=m.get("id"),
            has_tool_payload=m.get("seq") in tool_seqs,
        )
        for m in rows
        if m["role"] in ("user", "assistant") and m.get("redacted_content")
    ]


async def _load_thread_summary(
    threads: ThreadRepository,
    thread_id: str,
    tenant_id: str,
) -> tuple[str | None, int | None, list[dict[str, Any]] | None]:
    """Load the compaction checkpoint (Arch 8.2) for a thread.

    Returns ``(content, position, layers)``. Hot tier first (TTL-promoted
    fast path), then the durable thread row. The summary is written only by
    the compactor and is immutable once pinned (summary_version); read-only
    here. ``position`` = seq of the last summarized message: the assembler
    replaces every turn with ``seq <= position`` by the summary block.
    ``layers`` (P2-6) are the byte-stable summary layers; None when the
    block predates layering (assembler falls back to ``content``).
    """
    cached = await get_thread_tail_cache().get_tail(tenant_id, thread_id)
    if cached:
        block = cached.get("summary") or {}
        if block.get("content"):
            return (
                block["content"],
                block.get("position"),
                block.get("layers") or None,
            )
    row = await threads.get_thread(tenant_id, thread_id)
    if not row:
        return None, None, None
    block = row.get("summary_block")
    if isinstance(block, dict):
        return (
            block.get("content"),
            row.get("summary_position"),
            block.get("layers") or None,
        )
    return (block or None), row.get("summary_position"), None


def _build_compaction_callback(
    db: Any,
    threads: ThreadRepository,
    tenant_config: TenantConfig,
    provider: str,
    summarizer_model: str,
    thread: dict[str, Any],
) -> Any:
    """Build the orchestration compaction hook (Arch 8.2, P2-3/P2-5).

    Preemptive triggers defer to the background ``summary.refresh`` worker
    job (P2-5: the turn keeps moving — no user-facing wait; the worker
    performs the summarization and the next trigger is an instant swap).
    Overflow requests (one-shot recovery path) compact inline — a rare
    failure path where a synchronous checkpoint is the only option.
    Failures return None so the turn continues un-compacted — compaction
    never breaks a turn.

    P3-9: the summarizer runs through the gateway (``GatewayBackedAdapter``)
    — same routing, quota, ledger and caching path as chat; the route no
    longer resolves provider keys.
    """
    compaction = CompactionService(
        threads=threads,
        generator=LLMSummaryGenerator(
            adapter=GatewayBackedAdapter(
                get_gateway(), tenant_config, provider, summarizer_model
            ),
        ),
        estimator=TokenEstimator(),
    )
    tenant_id = str(tenant_config.id)

    async def _defer_to_worker() -> bool:
        from backend.app.application.compaction.refresh import JOB_SUMMARY_REFRESH
        from backend.app.infrastructure.queue.manager import Job, get_queue_manager

        try:
            manager = get_queue_manager()
            job = Job(
                type=JOB_SUMMARY_REFRESH,
                payload={"tenant_id": tenant_id, "thread_id": thread["id"]},
            )
            return bool(
                await manager.enqueue(
                    job, idempotency_key=f"summary-refresh:{thread['id']}"
                )
            )
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(
                "summary_refresh_enqueue_failed",
                thread_id=thread["id"],
                error=str(e),
            )
            return False

    async def _callback(info: dict[str, Any]) -> dict[str, Any] | None:
        try:
            if info.get("overflow"):
                result = await compaction.compact(tenant_id, thread["id"])
            else:
                decision = compaction.evaluate(
                    estimated_tokens=info.get("estimated_tokens") or 0,
                    budget_tokens=info.get("budget_tokens") or 0,
                    has_summary=bool(info.get("has_summary")),
                )
                if not decision.triggered:
                    return None
                if decision.action == "preemptive":
                    # P2-5: defer to the background worker; no checkpoint
                    # this turn (the generate loop continues un-compacted).
                    enqueued = await _defer_to_worker()
                    logger.info(
                        "compaction_deferred_to_worker",
                        thread_id=thread["id"],
                        ratio=round(
                            (info.get("estimated_tokens") or 0)
                            / (info.get("budget_tokens") or 1),
                            3,
                        ),
                        enqueued=enqueued,
                    )
                    return None
                result = await compaction.compact(tenant_id, thread["id"])
            if not result["compacted"]:
                return None
            await _refresh_hot_tail(
                db,
                tenant_id,
                thread["id"],
                summary={
                    "content": result["summary"],
                    "position": result["summary_position"],
                    "layers": result.get("layers"),
                },
            )
            return {
                "summary": result["summary"],
                "position": result["summary_position"],
                "layers": result.get("layers"),
            }
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("compaction_failed", thread_id=thread["id"], error=str(e))
            return None

    return _callback


# Per-tenant RAG service registry (vector store + embedding model are shared;
# RAGService itself is stateless between requests). Async single-threaded
# event loops make duplicate construction benign (last write wins).
_rag_services: dict[str, object] = {}


def _get_retrieval_service(tenant_id: UUID):
    """Get (or lazily create) the retrieval service for a tenant.

    Uses the configured vector store (pgvector for Postgres, in-memory for
    dev). Embedding failures degrade to empty retrieval (logged), never to
    request errors.
    """
    from backend.app.adapters.vectorstore import create_vector_store_from_settings

    key = str(tenant_id)
    if key in _rag_services:
        return _rag_services[key]

    vector_store = create_vector_store_from_settings()
    rag_service = create_rag_service(vector_store, tenant_id=tenant_id)
    retrieval_service = create_retrieval_service(rag_service)
    _rag_services[key] = retrieval_service
    logger.info("retrieval_service_ready", tenant_id=key)
    return retrieval_service


@router.post("/tenants", response_model=TenantResponse)
async def create_tenant(
    request: TenantCreateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Create a new tenant."""
    logger.info("tenant_create_request", slug=request.slug, actor=principal.key_id)

    from backend.app.governance.residency import resolve_region

    # P5-12: region is pinned at onboarding; unsupported regions are denied.
    try:
        region = resolve_region(request.region)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid region: {e}",
        )

    tenant_service = get_tenant_config_service()

    # Check if slug already exists (DB is source of truth, JSON file is a cache)
    db = get_database_manager()
    tenant_repo = TenantRepository(db)
    existing = await tenant_repo.get_by_slug(request.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant with slug '{request.slug}' already exists",
        )

    # Create tenant
    config = tenant_service.create_tenant(
        name=request.name,
        slug=request.slug,
        allowed_topics=request.allowed_topics,
        blocked_topics=request.blocked_topics,
        escalation_threshold=request.escalation_threshold,
    )

    # Dual-write: durable DB record + JSON cache for the config service
    await tenant_repo.create({
        "id": str(config.id),
        "slug": config.slug,
        "name": config.name,
        "allowed_topics": config.allowed_topics,
        "blocked_topics": config.blocked_topics,
        "escalation_threshold": config.escalation_threshold,
        "knowledge_allowlist": config.knowledge_allowlist,
        "default_provider": config.default_provider,
        "default_model": config.default_model,
        "features": config.features,
        "guardrail_config": config.guardrail_config,
        "guardrail_thresholds": config.guardrail_thresholds,
        "region": region,
        "retention_days": request.retention_days,
    })
    tenant_service.save_config(config)

    # Create default policy set
    tenant_service.create_policy_set(config.id, "default")
    await PolicyRepository(db).create_set(str(config.id), "default", rules=[])

    await AuditRepository(db).add(
        action="tenant.created",
        resource_type="tenant",
        resource_id=str(config.id),
        tenant_id=str(config.id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={
            "slug": config.slug,
            "name": config.name,
            "region": region,
            "retention_days": request.retention_days,
        },
    )

    return TenantResponse(
        id=str(config.id),
        name=config.name,
        slug=config.slug,
        allowed_topics=config.allowed_topics,
        blocked_topics=config.blocked_topics,
        escalation_threshold=config.escalation_threshold,
        region=region,
        retention_days=request.retention_days,
    )


@router.get("/tenants", response_model=list[TenantResponse])
async def list_tenants(
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    """List tenants. Tenant-bound keys only see their own tenant."""
    db = get_database_manager()
    tenant_repo = TenantRepository(db)

    if principal.tenant_id is not None:
        row = await tenant_repo.get_by_id(str(principal.tenant_id))
        rows = [row] if row else []
    else:
        rows = await tenant_repo.list_all()

    return [
        TenantResponse(
            id=r["id"],
            name=r["name"],
            slug=r["slug"],
            allowed_topics=r["allowed_topics"],
            blocked_topics=r["blocked_topics"],
            escalation_threshold=r["escalation_threshold"],
            region=r.get("region"),
            retention_days=r.get("retention_days"),
        )
        for r in rows
    ]


@router.get("/tenants/{tenant_id}", response_model=TenantResponse)
async def get_tenant(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    """Get a tenant by ID."""
    logger.info("tenant_get_request", tenant_id=tenant_id)

    assert_tenant_access(principal, tenant_id)

    db = get_database_manager()
    row = await TenantRepository(db).get_by_id(str(tenant_id))

    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )

    return TenantResponse(
        id=row["id"],
        name=row["name"],
        slug=row["slug"],
        allowed_topics=row["allowed_topics"],
        blocked_topics=row["blocked_topics"],
        escalation_threshold=row["escalation_threshold"],
        region=row.get("region"),
        retention_days=row.get("retention_days"),
    )


@router.put(
    "/tenants/{tenant_id}/provider-keys/{provider}",
    response_model=ProviderKeyResponse,
)
async def set_tenant_provider_key(
    tenant_id: UUID,
    provider: str,
    request: ProviderKeyRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Set or rotate a tenant's provider credential (BYOK, Arch 6.3.9).

    The key is envelope-encrypted before storage and never returned,
    logged, or exposed in traces. Rotation bumps ``key_version``.
    """
    from backend.app.infrastructure.keys.service import (
        KEY_SOURCES,
        SUPPORTED_PROVIDERS,
        ProviderKeyService,
    )

    assert_tenant_access(principal, tenant_id)

    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unsupported provider '{provider}'. Must be one of: {', '.join(SUPPORTED_PROVIDERS)}",
        )
    if request.key_source not in KEY_SOURCES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"key_source must be one of: {', '.join(KEY_SOURCES)}",
        )

    db = get_database_manager()
    tenant = await TenantRepository(db).get_by_id(str(tenant_id))
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )

    row = await ProviderKeyService(db).set_key(
        str(tenant_id), provider, request.api_key, key_source=request.key_source
    )
    await AuditRepository(db).add(
        action="provider_key.rotated",
        resource_type="provider_key",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={
            "provider": provider,
            "key_source": row["key_source"],
            "key_version": row["key_version"],
        },
    )
    logger.info(
        "tenant_provider_key_rotated",
        tenant_id=str(tenant_id),
        provider=provider,
        key_version=row["key_version"],
    )
    return ProviderKeyResponse(
        tenant_id=str(tenant_id),
        provider=provider,
        key_source=row["key_source"],
        key_version=row["key_version"],
        updated_at=row["updated_at"],
    )


class DeploymentShapeRequest(BaseModel):
    """Opt a tenant into a dedicated deployment shape (P5-12).

    Dedicated = own DB/vector/gateway behind the same shared control plane.
    It is a configuration choice, never a code fork.
    """

    dedicated: bool


@router.put("/tenants/{tenant_id}/deployment-shape")
async def set_tenant_deployment_shape(
    tenant_id: UUID,
    request: DeploymentShapeRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Set the tenant's deployment shape (shared | dedicated) and return the
    resulting isolation config (P5-12)."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.governance.residency import (
        dedicated_deployment_config,
        deployment_shape,
    )

    db = get_database_manager()
    current = await TenantRepository(db).get_by_id(str(tenant_id))
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )
    features = dict(current.get("features") or {})
    features["dedicated_deployment"] = request.dedicated
    row = await TenantRepository(db).update(str(tenant_id), {"features": features})
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )
    await AuditRepository(db).add(
        action="tenant.deployment_shape_changed",
        resource_type="tenant",
        resource_id=str(tenant_id),
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={"dedicated": request.dedicated},
    )
    return {
        "tenant_id": str(tenant_id),
        "shape": deployment_shape(row),
        "dedicated_deployment": dedicated_deployment_config(row),
    }


@router.post(
    "/tenants/{tenant_id}/config-versions",
    response_model=ConfigVersionResponse,
)
async def create_tenant_config_version(
    tenant_id: UUID,
    request: ConfigVersionRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Append a new immutable config version (draft) for a tenant.

    The config payload must carry id/name/slug matching the tenant;
    publish/rollback are separate explicit operations.
    """
    assert_tenant_access(principal, tenant_id)

    db = get_database_manager()
    tenant = await TenantRepository(db).get_by_id(str(tenant_id))
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )

    try:
        candidate = tenant_config_from_data(request.config)
    except (KeyError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid config payload: {e}",
        )
    if str(candidate.id) != str(tenant_id) or candidate.slug != tenant["slug"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Config id/slug must match the tenant",
        )

    # P5-1/P5-2 eval gate: structural (schema) + compile validation before a
    # draft may be created. A payload that fails cannot exist as a draft and
    # therefore can never be promoted.
    try:
        compile_surface_config(candidate, surface=None)
    except ConfigValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Config failed validation: {e}",
        )

    repo = TenantConfigVersionRepository(db)
    row = await repo.create_draft(
        str(tenant_id), request.config, promoted_by=principal.key_id
    )
    # P5-2 pipeline: record the passing evaluation, then the privileged
    # approval, so promoted versions have a full (validated, approved) trail.
    validated = await repo.set_validation(
        str(tenant_id), row["version"], "validated", by=principal.key_id
    )
    approved = await repo.approve(
        str(tenant_id), row["version"], approved_by=principal.key_id
    )
    await AuditRepository(db).add(
        action="config_version.created",
        resource_type="tenant_config_version",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={
            "version": row["version"],
            "status": row["status"],
            "validation_status": (validated or {}).get("validation_status"),
            "approved_by": (approved or {}).get("approved_by"),
        },
    )
    return ConfigVersionResponse(
        tenant_id=str(tenant_id),
        version=row["version"],
        status=row["status"],
        validation_status=(validated or {}).get("validation_status", ""),
        published_at=row["published_at"],
        promoted_by=row["promoted_by"],
    )


@router.post(
    "/tenants/{tenant_id}/config-versions/{version}/publish",
    response_model=ConfigVersionResponse,
)
async def publish_tenant_config_version(
    tenant_id: UUID,
    version: int,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
    canary_percent: int | None = Query(default=None, ge=0, le=100),
):
    """Publish a draft (or re-promote any version); supersedes the old one.

    Gated on the P5-2 pipeline: a version that failed validation or its eval
    suite cannot be promoted. ``canary_percent`` (0-100) turns the publish
    into a canary rollout — the previous published version stays live as the
    baseline and the runtime serves the canary to a deterministic slice of
    request traffic. Runtime behavior switches on the next request; in-memory
    caches are invalidated and an audit event is recorded.
    """
    assert_tenant_access(principal, tenant_id)

    db = get_database_manager()
    repo = TenantConfigVersionRepository(db)
    draft = await repo.get(str(tenant_id), version)
    allowed, reason = promotion_gate(
        (draft or {}).get("validation_status"),
        (draft or {}).get("eval_status"),
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Config version {version} cannot be promoted: {reason}",
        )
    if canary_percent is not None and not validate_canary_percent(canary_percent):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="canary_percent must be between 0 and 100",
        )
    row = await repo.promote(
        str(tenant_id),
        version,
        promoted_by=principal.key_id,
        canary_percent=canary_percent,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config version not found: {tenant_id} v{version}",
        )

    get_tenant_config_service().invalidate_cache(tenant_id)
    await AuditRepository(db).add(
        action="config_version.published",
        resource_type="tenant_config_version",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={
            "version": row["version"],
            "status": row["status"],
            "canary_percent": row.get("canary_percent"),
        },
    )
    logger.info(
        "tenant_config_version_published",
        tenant_id=str(tenant_id),
        version=row["version"],
        canary_percent=row.get("canary_percent"),
    )
    return ConfigVersionResponse(
        tenant_id=str(tenant_id),
        version=row["version"],
        status=row["status"],
        validation_status=(draft or {}).get("validation_status", ""),
        eval_status=(draft or {}).get("eval_status") or "",
        canary_percent=row.get("canary_percent"),
        published_at=row["published_at"],
        promoted_by=row["promoted_by"],
    )


@router.post(
    "/tenants/{tenant_id}/config-versions/{version}/evaluate",
    response_model=ConfigVersionResponse,
)
async def evaluate_tenant_config_version(
    tenant_id: UUID,
    version: int,
    request: ConfigEvalRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Record an eval-suite result on a draft (P5-2 pipeline, P6-6 slot).

    A failing suite blocks promotion at the publish gate; the result and
    per-metric details are retained on the immutable version record for the
    audit trail.
    """
    assert_tenant_access(principal, tenant_id)

    db = get_database_manager()
    repo = TenantConfigVersionRepository(db)
    draft = await repo.get(str(tenant_id), version)
    if draft is None or draft.get("status") != repo.STATUS_DRAFT:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config version not found: {tenant_id} v{version}",
        )
    eval_status = "passed" if request.passed else "failed"
    row = await repo.set_eval_result(
        str(tenant_id),
        version,
        eval_status,
        details={
            "suite": request.suite,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "metrics": request.details,
        },
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config version not found: {tenant_id} v{version}",
        )
    await AuditRepository(db).add(
        action="config_version.evaluated",
        resource_type="tenant_config_version",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={
            "version": row["version"],
            "suite": request.suite,
            "eval_status": eval_status,
        },
    )
    return ConfigVersionResponse(
        tenant_id=str(tenant_id),
        version=row["version"],
        status=row["status"],
        validation_status=row.get("validation_status") or "",
        eval_status=row.get("eval_status") or "",
        canary_percent=row.get("canary_percent"),
        published_at=row["published_at"],
        promoted_by=row["promoted_by"],
    )


@router.post(
    "/tenants/{tenant_id}/config-versions/{version}/auto-rollback",
    response_model=ConfigVersionResponse,
)
async def auto_rollback_tenant_config_version(
    tenant_id: UUID,
    version: int,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
    reason: str = Query(default="eval regression"),
):
    """Auto-rollback on regression (P5-2): re-promote the prior version.

    Marks the failing published version ``regressed`` (a failed eval gate —
    it cannot be re-promoted until re-validated) and fully re-publishes the
    previously live version. Mirrors a canary watchdog firing on error-rate
    regression.
    """
    assert_tenant_access(principal, tenant_id)

    db = get_database_manager()
    repo = TenantConfigVersionRepository(db)
    row = await repo.auto_rollback(
        str(tenant_id), version, reason=reason, rolled_back_by=principal.key_id
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Config version {tenant_id} v{version} is not a published rollout",
        )

    get_tenant_config_service().invalidate_cache(tenant_id)
    await AuditRepository(db).add(
        action="config_version.auto_rolled_back",
        resource_type="tenant_config_version",
        resource_id=row["id"],
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details={"version": row["version"], "reason": reason},
    )
    logger.info(
        "tenant_config_version_auto_rolled_back",
        tenant_id=str(tenant_id),
        version=row["version"],
        reason=reason,
    )
    return ConfigVersionResponse(
        tenant_id=str(tenant_id),
        version=row["version"],
        status=row["status"],
        validation_status=row.get("validation_status") or "",
        eval_status=row.get("eval_status") or "",
        canary_percent=row.get("canary_percent"),
        published_at=row["published_at"],
        promoted_by=row["promoted_by"],
    )


@router.post(
    "/tenants/{tenant_id}/config-versions/{version}/rollback",
    response_model=ConfigVersionResponse,
)
async def rollback_tenant_config_version(
    tenant_id: UUID,
    version: int,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Rollback: re-promote a previously published (superseded) version."""
    return await publish_tenant_config_version(tenant_id, version, principal)


@router.get("/tenants/{tenant_id}/gdpr/export")
async def export_tenant_gdpr(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    """GDPR data-subject export: portable JSON bundle of all tenant data."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.application.tenant_lifecycle import TenantLifecycleService

    try:
        bundle = await TenantLifecycleService().export_tenant_data(str(tenant_id))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    return JSONResponse(
        content=_jsonable(bundle),
        headers={
            "Content-Disposition": (
                f'attachment; filename="gdpr-export-{tenant_id}.json"'
            )
        },
    )


@router.delete("/tenants/{tenant_id}/data", status_code=status.HTTP_200_OK)
async def erase_tenant_data(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """GDPR erasure: delete all tenant data; tenant + audit trail retained."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.application.tenant_lifecycle import TenantLifecycleService

    deleted = await TenantLifecycleService().erase_tenant_data(str(tenant_id))
    return {"tenant_id": str(tenant_id), "deleted": deleted}


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_200_OK)
async def offboard_tenant(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Offboard: erase tenant data, revoke its API keys, delete the tenant."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.application.tenant_lifecycle import TenantLifecycleService

    deleted = await TenantLifecycleService().offboard_tenant(str(tenant_id))
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )
    return {"tenant_id": str(tenant_id), "status": "offboarded"}


@router.post("/tenants/{tenant_id}/onboard")
async def onboard_tenant(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Run the onboarding checklist (P5-10): default deny-by-default surface,
    isolation namespaces/prefixes, budget defaults, tenant-bound operator key.

    Idempotent: steps already provisioned are reported as such. The raw
    operator key is returned exactly once, on first creation.
    """
    assert_tenant_access(principal, tenant_id)

    from backend.app.application.tenant_lifecycle import TenantOnboardingService

    try:
        receipt = await TenantOnboardingService().run(str(tenant_id))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
    return receipt


@router.get("/tenants/{tenant_id}/onboarding")
async def get_onboarding_checklist(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    """Report the onboarding checklist state for a tenant (P5-10)."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.application.tenant_lifecycle import TenantOnboardingService

    try:
        return await TenantOnboardingService().checklist(str(tenant_id))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@router.get("/tenants/{tenant_id}/metrics")
async def get_tenant_metrics(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    """Tenant-scoped metrics (P5-10): conversations, messages, end users,
    escalations, governance evidence, webhook deliveries."""
    assert_tenant_access(principal, tenant_id)

    db = get_database_manager()
    tenant = await TenantRepository(db).get_by_id(str(tenant_id))
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )

    conversation_repo = ConversationRepository(db)
    conversations = await conversation_repo.list_by_tenant(str(tenant_id), limit=10000)
    messages = await conversation_repo.list_messages_all([c["id"] for c in conversations])

    return {
        "tenant_id": str(tenant_id),
        "conversations": len(conversations),
        "messages": len(messages),
        "end_users": len(await EndUserRepository(db).list_by_tenant(str(tenant_id))),
        "escalations": len(
            await EscalationRepository(db).list_by_tenant(str(tenant_id), limit=10000)
        ),
        "guardrail_evidence": len(
            await EvidenceRepository(db).list_by_tenant(str(tenant_id), limit=10000)
        ),
        "threads": len(
            await ThreadRepository(db).list_threads(str(tenant_id), limit=10000)
        ),
    }


@router.get("/tenants/{tenant_id}/end-users/{end_user_id}/export")
async def export_end_user_data(
    tenant_id: UUID,
    end_user_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    """DSR export: portable JSON bundle of one end user's data."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.application.tenant_lifecycle import TenantLifecycleService

    try:
        bundle = await TenantLifecycleService().export_end_user_data(
            str(tenant_id), str(end_user_id)
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    return JSONResponse(
        content=_jsonable(bundle),
        headers={
            "Content-Disposition": (
                f'attachment; filename="dsr-export-{end_user_id}.json"'
            )
        },
    )


@router.delete("/tenants/{tenant_id}/end-users/{end_user_id}/data", status_code=status.HTTP_200_OK)
async def erase_end_user_data(
    tenant_id: UUID,
    end_user_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """DSR erasure: delete one end user's data and revoke their sessions."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.application.tenant_lifecycle import TenantLifecycleService

    try:
        deleted = await TenantLifecycleService().erase_end_user_data(
            str(tenant_id), str(end_user_id)
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

    return {"tenant_id": str(tenant_id), "end_user_id": str(end_user_id), "deleted": deleted}


class ComplianceIncidentRequest(BaseModel):
    """Art. 73 incident-reporting hook request (P5-11)."""

    severity: str = Field(default="medium", pattern="^(low|medium|high|critical)$")
    description: str = Field(..., min_length=1, max_length=4000)


@router.get("/tenants/{tenant_id}/compliance")
async def get_compliance_posture(
    tenant_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:read")),
):
    """Compliance posture (P5-11): Art. 50 disclosure metadata + the
    re-verification checklist as of the last verification date."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.governance.compliance import ComplianceService, disclosure_payload

    db = get_database_manager()
    tenant = await TenantRepository(db).get_by_id(str(tenant_id))
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )
    surface = await SurfaceRepository(db).get_default(str(tenant_id))
    service = ComplianceService()
    return {
        "tenant_id": str(tenant_id),
        "disclosure": disclosure_payload(
            tenant_id=str(tenant_id),
            surface=surface,
            surface_type=(surface or {}).get("surface_type") or "widget",
        ),
        "posture": service.checklist(),
    }


@router.post("/tenants/{tenant_id}/compliance/incidents")
async def report_compliance_incident(
    tenant_id: UUID,
    request: ComplianceIncidentRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("tenants:write")),
):
    """Art. 73 incident reporting hook (P5-11): record the incident on the
    immutable audit trail."""
    assert_tenant_access(principal, tenant_id)

    from backend.app.governance.compliance import ComplianceService

    db = get_database_manager()
    tenant = await TenantRepository(db).get_by_id(str(tenant_id))
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )
    record = ComplianceService().report_incident(
        tenant_id=str(tenant_id),
        severity=request.severity,
        description=request.description,
    )
    await AuditRepository(db).add(
        action="compliance.incident_reported",
        resource_type="tenant",
        resource_id=str(tenant_id),
        tenant_id=str(tenant_id),
        actor_type="api_key",
        actor_id=principal.key_id,
        details=record,
    )
    logger.info(
        "compliance_incident_reported",
        tenant_id=str(tenant_id),
        severity=request.severity,
    )
    return record


@router.post("/api-keys", response_model=ApiKeyResponse)
async def create_api_key(
    request: ApiKeyCreateRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("api_keys:manage")),
):
    """Create an API key. The raw key is returned exactly once."""
    if request.role not in ("super_admin", "tenant_admin", "operator", "auditor"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="role must be one of: super_admin, tenant_admin, operator, auditor",
        )
    # P5-10 delegated admin: tenant-bound principals (tenant_admin) can only
    # manage keys inside their own tenant and may never mint super_admin keys.
    if principal.tenant_id is not None:
        if request.tenant_id is not None and str(request.tenant_id) != str(
            principal.tenant_id
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Delegated admin can only create keys for their own tenant",
            )
        if request.role == "super_admin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Delegated admin cannot create super_admin keys",
            )
        effective_tenant_id = str(principal.tenant_id)
    else:
        effective_tenant_id = request.tenant_id

    raw_key, prefix, key_hash = generate_api_key()
    db = get_database_manager()
    repo = ApiKeyRepository(db)
    record = await repo.create(
        name=request.name,
        key_hash=key_hash,
        prefix=prefix,
        role=request.role,
        tenant_id=effective_tenant_id,
        expires_at=request.expires_at,
    )

    await AuditRepository(db).add(
        action="api_key.created",
        resource_type="api_key",
        resource_id=record["id"],
        tenant_id=record["tenant_id"],
        actor_type="api_key",
        actor_id=principal.key_id,
        details={"name": request.name, "role": request.role},
    )

    return ApiKeyResponse(
        id=record["id"],
        name=record["name"],
        role=record["role"],
        tenant_id=record["tenant_id"],
        prefix=record["prefix"],
        key=raw_key,
        expires_at=record["expires_at"],
        created_at=record["created_at"],
    )


@router.get("/api-keys", response_model=list[ApiKeyListItem])
async def list_api_keys(
    principal: ApiKeyPrincipal = Depends(require_permission("api_keys:manage")),
):
    """List API keys (raw keys are never returned)."""
    db = get_database_manager()
    rows = await ApiKeyRepository(db).list_all()
    if principal.tenant_id is not None:
        rows = [r for r in rows if r["tenant_id"] == str(principal.tenant_id)]
    return [
        ApiKeyListItem(
            id=r["id"],
            name=r["name"],
            role=r["role"],
            tenant_id=r["tenant_id"],
            prefix=r["prefix"],
            revoked=r["revoked"],
            expires_at=r["expires_at"],
            last_used_at=r["last_used_at"],
            usage_count=r["usage_count"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: UUID,
    principal: ApiKeyPrincipal = Depends(require_permission("api_keys:manage")),
):
    """Revoke an API key."""
    db = get_database_manager()
    repo = ApiKeyRepository(db)
    rows = await repo.list_all()
    target = next((r for r in rows if r["id"] == str(key_id)), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key not found: {key_id}",
        )
    # P5-10 delegated admin: tenant-bound principals only revoke their own keys.
    if principal.tenant_id is not None and target["tenant_id"] != str(
        principal.tenant_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Delegated admin can only revoke keys of their own tenant",
        )
    revoked = await repo.revoke(str(key_id))
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key not found: {key_id}",
        )
    await AuditRepository(db).add(
        action="api_key.revoked",
        resource_type="api_key",
        resource_id=str(key_id),
        actor_type="api_key",
        actor_id=principal.key_id,
    )


@router.get("/audit/events", response_model=list[AuditEventResponse])
async def list_audit_events(
    tenant_id: UUID | None = None,
    principal: ApiKeyPrincipal = Depends(require_permission("audit:read")),
):
    """List audit events (immutable admin trail)."""
    if tenant_id is not None:
        assert_tenant_access(principal, tenant_id)

    db = get_database_manager()
    events = await AuditRepository(db).list_events(
        tenant_id=str(tenant_id) if tenant_id else None
    )
    return [
        AuditEventResponse(
            id=e["id"],
            tenant_id=e["tenant_id"],
            actor_type=e["actor_type"],
            actor_id=e["actor_id"],
            action=e["action"],
            resource_type=e["resource_type"],
            resource_id=e["resource_id"],
            details=e["details"],
            created_at=e["created_at"],
        )
        for e in events
    ]


@router.get("/audit/export")
async def export_audit_events(
    tenant_id: UUID | None = None,
    principal: ApiKeyPrincipal = Depends(require_permission("audit:read")),
):
    """Export the immutable audit trail as a downloadable JSON file.

    Records are append-only (no update/delete endpoints exist), so the
    export is a stable point-in-time snapshot suitable for compliance
    evidence (ISO-42001 A.9).
    """
    if tenant_id is not None:
        assert_tenant_access(principal, tenant_id)

    from datetime import datetime, timezone

    db = get_database_manager()
    events = await AuditRepository(db).list_events(
        tenant_id=str(tenant_id) if tenant_id else None,
        limit=10000,
    )
    return JSONResponse(
        content=_jsonable(
            {
                "schema_version": "1.0",
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "immutable": True,
                "events": events,
            }
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="audit-export-{tenant_id or "all"}.json"'
            )
        },
    )


@router.get("/escalations", response_model=list[EscalationResponse])
async def list_escalations(
    status_filter: str | None = None,
    principal: ApiKeyPrincipal = Depends(require_permission("escalations:read")),
):
    """List escalation records. Tenant-bound keys only see their own tenant."""
    db = get_database_manager()
    repo = EscalationRepository(db)

    if principal.tenant_id is not None:
        rows = await repo.list_by_tenant(str(principal.tenant_id), status=status_filter)
    else:
        rows = await repo.list_all(status=status_filter)
    return [
        EscalationResponse(
            id=r["id"],
            tenant_id=r["tenant_id"],
            conversation_id=r["conversation_id"],
            session_id=r["session_id"],
            category=r["category"],
            severity=r["severity"],
            status=r["status"],
            reason=r["reason"],
            summary=r["summary"],
            details=r["details"],
            channel=r["channel"],
            external_ref=r["external_ref"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


class EscalationActionRequest(BaseModel):
    """Operator action on an escalation (human-in-the-loop gate)."""

    note: str | None = None


async def _get_escalation_or_404(escalation_id: str, principal: ApiKeyPrincipal) -> dict:
    db = get_database_manager()
    repo = EscalationRepository(db)
    row = await repo.get_by_id(escalation_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Escalation not found")
    if principal.tenant_id is not None and str(principal.tenant_id) != row["tenant_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your tenant")
    return row


@router.post("/escalations/{escalation_id}/assign", response_model=EscalationResponse)
async def assign_escalation(
    escalation_id: str,
    request: EscalationActionRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("escalations:write")),
):
    """Assign the escalation to an operator (``pending`` -> ``in_review``)."""
    row = await _get_escalation_or_404(escalation_id, principal)
    if row["status"] not in ("pending", "in_review"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot assign escalation in status '{row['status']}'",
        )
    details = dict(row["details"])
    if request.note:
        details["assign_note"] = request.note
    repo = EscalationRepository(get_database_manager())
    updated = await repo.update_status(escalation_id, "in_review", details=details)
    logger.info("escalation_assigned", escalation_id=escalation_id, actor=principal.role)
    return _escalation_response(updated)


@router.post("/escalations/{escalation_id}/resolve", response_model=EscalationResponse)
async def resolve_escalation(
    escalation_id: str,
    request: EscalationActionRequest,
    principal: ApiKeyPrincipal = Depends(require_permission("escalations:write")),
):
    """Resolve the escalation (operator approval gate complete)."""
    row = await _get_escalation_or_404(escalation_id, principal)
    if row["status"] == "resolved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Escalation is already resolved",
        )
    details = dict(row["details"])
    if request.note:
        details["resolution_note"] = request.note
    repo = EscalationRepository(get_database_manager())
    updated = await repo.update_status(
        escalation_id,
        "resolved",
        details=details,
        resolved_at=datetime.now(timezone.utc),
    )
    logger.info("escalation_resolved", escalation_id=escalation_id, actor=principal.role)
    return _escalation_response(updated)


def _escalation_response(row: dict) -> EscalationResponse:
    return EscalationResponse(
        id=row["id"],
        tenant_id=row["tenant_id"],
        conversation_id=row["conversation_id"],
        session_id=row["session_id"],
        category=row["category"],
        severity=row["severity"],
        status=row["status"],
        reason=row["reason"],
        summary=row["summary"],
        details=row["details"],
        channel=row["channel"],
        external_ref=row["external_ref"],
        created_at=row["created_at"],
    )


@router.post("/conversations/{conversation_id}/pause")
async def pause_conversation(
    conversation_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
):
    """Pause a conversation: the HITL hold gate blocks new messages (423)."""
    db = get_database_manager()
    conversation_repo = ConversationRepository(db)
    if not await conversation_repo.set_status(conversation_id, "paused"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    logger.info(
        "conversation_paused",
        conversation_id=conversation_id,
        actor=principal.role,
    )
    return {"status": "paused", "conversation_id": conversation_id}


@router.post("/conversations/{conversation_id}/resume")
async def resume_conversation(
    conversation_id: str,
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
):
    """Resume a paused conversation, lifting the HITL hold gate."""
    db = get_database_manager()
    conversation_repo = ConversationRepository(db)
    if not await conversation_repo.set_status(conversation_id, "active"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found")
    logger.info(
        "conversation_resumed",
        conversation_id=conversation_id,
        actor=principal.role,
    )
    return {"status": "active", "conversation_id": conversation_id}


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
