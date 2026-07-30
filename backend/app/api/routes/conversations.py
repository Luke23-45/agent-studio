"""
API routes for the Neryva Agent Studio backend.

Provides REST endpoints for conversations, tenants, and policies.
"""

from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.adapters.dlp import get_pii_service
from backend.app.application.orchestration import create_orchestration_service
from backend.app.domain.policy import PolicyAction
from backend.app.domain.tenant import TenantConfig
from backend.app.modules.guardrails import create_guardrails_service
from backend.app.modules.tenant_config import get_tenant_config_service

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


class TenantCreateRequest(BaseModel):
    """Request model for creating a tenant."""

    name: str = Field(..., min_length=1)
    slug: str = Field(..., min_length=1)
    allowed_topics: list[str] = Field(default_factory=list)
    blocked_topics: list[str] = Field(default_factory=list)
    escalation_threshold: float = Field(default=0.7, ge=0.0, le=1.0)


class TenantResponse(BaseModel):
    """Response model for tenant."""

    id: str
    name: str
    slug: str
    allowed_topics: list[str]
    blocked_topics: list[str]
    escalation_threshold: float


@router.post("/conversations", response_model=ConversationResponse)
async def process_conversation(request: ConversationRequest):
    """Process a conversation message."""
    logger.info("conversation_request", tenant_slug=request.tenant_slug)

    # Load tenant config
    tenant_service = get_tenant_config_service()
    tenant_config = tenant_service.load_config_by_slug(request.tenant_slug)

    if not tenant_config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {request.tenant_slug}",
        )

    # PII redaction
    pii_service = get_pii_service()
    pii_result = pii_service.process_message(request.message)

    # Guardrails check
    guardrails_service = create_guardrails_service()
    input_validation = await guardrails_service.validate_input(pii_result.redacted_text)

    if not input_validation["is_valid"] and input_validation["blocked"]:
        return ConversationResponse(
            response="I cannot help with that request.",
            confidence=1.0,
            handoff_required=False,
            session_id=request.session_id or "new",
        )

    # Create orchestration service
    from backend.app.settings.env import settings

    # Get API key from settings (in production, this would come from tenant config)
    llm_api_key = settings.model_dump().get("OPENAI_API_KEY", "dummy-key")

    # Create policy set for tenant
    policy_set = tenant_service.get_policy_set(tenant_config.id)
    if not policy_set:
        policy_set = tenant_service.create_policy_set(tenant_config.id, "default")

    orchestration = create_orchestration_service(
        tenant_config=tenant_config,
        policy_set=policy_set,
        llm_api_key=llm_api_key,
    )

    # Process message
    try:
        result = await orchestration.process_message(
            user_message=request.message,
            redacted_message=pii_result.redacted_text,
        )

        if result.get("error"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=result["error"],
            )

        return ConversationResponse(
            response=result.get("model_response", ""),
            confidence=result.get("confidence", 0.0),
            handoff_required=result.get("handoff_required", False),
            session_id=request.session_id or "new",
        )
    except Exception as e:
        logger.error("conversation_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )


@router.post("/tenants", response_model=TenantResponse)
async def create_tenant(request: TenantCreateRequest):
    """Create a new tenant."""
    logger.info("tenant_create_request", slug=request.slug)

    tenant_service = get_tenant_config_service()

    # Check if slug already exists
    existing = tenant_service.load_config_by_slug(request.slug)
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

    # Save config
    tenant_service.save_config(config)

    # Create default policy set
    tenant_service.create_policy_set(config.id, "default")

    return TenantResponse(
        id=str(config.id),
        name=config.name,
        slug=config.slug,
        allowed_topics=config.allowed_topics,
        blocked_topics=config.blocked_topics,
        escalation_threshold=config.escalation_threshold,
    )


@router.get("/tenants/{tenant_id}", response_model=TenantResponse)
async def get_tenant(tenant_id: UUID):
    """Get a tenant by ID."""
    logger.info("tenant_get_request", tenant_id=tenant_id)

    tenant_service = get_tenant_config_service()
    config = tenant_service.load_config(tenant_id)

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant not found: {tenant_id}",
        )

    return TenantResponse(
        id=str(config.id),
        name=config.name,
        slug=config.slug,
        allowed_topics=config.allowed_topics,
        blocked_topics=config.blocked_topics,
        escalation_threshold=config.escalation_threshold,
    )


@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy"}
