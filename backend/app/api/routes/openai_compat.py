"""
OpenAI-compatible chat completions API (P7-3, Arch §5).

Provides ``POST /v1/chat/completions`` with the same envelope as
OpenAI's Chat API, so tenants can integrate using standard OpenAI SDKs
(``openai.ChatCompletion.create(base_url="...")``) without code changes.

Authentication: **tenant API keys only** (``X-API-Key`` header).
Session tokens are not accepted on this surface — this is the "Public
Chat API" surface per Arch §5, distinct from the widget/hosted page
surfaces that use session tokens.

Streaming uses the same SSE format as the OpenAI API:
``data: {"id":"...","choices":[{"delta":{"content":"..."}}]}``
followed by ``data: [DONE]``.
"""

import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ApiKeyPrincipal,
    assert_tenant_access,
    require_permission,
)
from backend.app.gateway.admission import (
    AdmissionLimitExceeded,
    get_admission_gate,
)
from backend.app.gateway.service import get_gateway
from backend.app.gateway.types import (
    GatewayChainExhausted,
    GatewayConfigurationError,
    GatewayError,
    GatewayQuotaExceeded,
    GatewayRequest,
)
from backend.app.infrastructure.db import (
    ConversationRepository,
    TenantRepository,
    ThreadRepository,
    get_database_manager,
)
from backend.app.modules.tenant_config import (
    get_tenant_config_service,
    policy_set_from_db,
    tenant_config_from_data,
)
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["OpenAI-Compatible"])


# ---------------------------------------------------------------------------
# Request / response models — OpenAI envelope
# ---------------------------------------------------------------------------


class ChatMessage(BaseModel):
    """A single message in the OpenAI messages array."""

    role: str = Field(..., description="One of: system, user, assistant, tool")
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible ``POST /v1/chat/completions`` request body."""

    model: str = Field(
        default="auto",
        description="Model name or 'auto' for tenant-configured routing",
    )
    messages: list[ChatMessage] = Field(
        ..., min_length=1, description="Conversation messages"
    )
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    stream: bool = Field(default=False)
    stop: str | list[str] | None = None
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    user: str | None = Field(
        default=None, description="End-user identifier for abuse tracking"
    )
    # Neryva extensions (ignored by standard OpenAI SDKs)
    tenant_slug: str | None = Field(
        default=None,
        description="Neryva: tenant slug override (defaults to the API key's tenant)",
    )
    surface_id: str | None = Field(
        default=None, description="Neryva: surface ID for persona selection"
    )


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: dict[str, Any]
    finish_reason: str | None = "stop"


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible chat completion response."""

    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: dict[str, Any]
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """SSE chunk in the OpenAI streaming format."""

    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    http_request: Request,
    principal: ApiKeyPrincipal = Depends(require_permission("conversations:write")),
):
    """OpenAI-compatible chat completions endpoint.

    Supports both streaming (``stream: true``) and non-streaming modes.
    """
    db = get_database_manager()
    tenant_repo = TenantRepository(db)

    # Tenant comes from the API key's binding or the explicit slug
    tenant_slug = request.tenant_slug
    if principal.tenant_id:
        tenant_row = await tenant_repo.get_by_id(str(principal.tenant_id))
        if tenant_row and tenant_slug and tenant_row.get("slug") != tenant_slug:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="API key is bound to a different tenant",
            )
    elif tenant_slug:
        tenant_row = await tenant_repo.get_by_slug(tenant_slug)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_slug is required when the API key is not tenant-bound",
        )

    if not tenant_row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )

    tenant_id = str(tenant_row["id"])
    assert_tenant_access(principal, tenant_id)

    # --- Build the prompt from the messages array ---
    # The last user message is the "current message"; everything before is history.
    last_user_content = ""
    for msg in reversed(request.messages):
        if msg.role == "user" and msg.content:
            last_user_content = msg.content
            break

    if not last_user_content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one user message is required",
        )

    # --- Generate a completion ID ---
    completion_id = f"chatcmpl-{uuid4().hex[:24]}"
    created_ts = int(time.time())
    model_name = request.model if request.model != "auto" else "neryva-auto"

    if request.stream:
        return StreamingResponse(
            _stream_completion(
                completion_id=completion_id,
                created_ts=created_ts,
                model_name=model_name,
                tenant_id=tenant_id,
                tenant_row=tenant_row,
                user_message=last_user_content,
                messages=request.messages,
                request=request,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return await _non_stream_completion(
        completion_id=completion_id,
        created_ts=created_ts,
        model_name=model_name,
        tenant_id=tenant_id,
        tenant_row=tenant_row,
        user_message=last_user_content,
        messages=request.messages,
        request=request,
    )


# ---------------------------------------------------------------------------
# Non-streaming completion
# ---------------------------------------------------------------------------


async def _non_stream_completion(
    *,
    completion_id: str,
    created_ts: int,
    model_name: str,
    tenant_id: str,
    tenant_row: dict,
    user_message: str,
    messages: list[ChatMessage],
    request: ChatCompletionRequest,
) -> ChatCompletionResponse:
    """Execute a non-streaming chat completion."""
    gateway = get_gateway()
    tenant_config = tenant_config_from_data(tenant_row)

    # Build history from the messages array (excluding the last user message)
    history = _build_history(messages)

    request_obj = GatewayRequest(
        tenant_id=tenant_id,
        messages=[*history, {"role": "user", "content": user_message}],
        request_id=completion_id,
        provider="openai",
        model=request.model if request.model != "auto" else None,
        strategy="cost",
        temperature=request.temperature if request.temperature is not None else 0.7,
        max_tokens=request.max_tokens,
    )

    admission = get_admission_gate()
    try:
        async with admission.admit(tenant_id):
            try:
                result = await gateway.generate(request_obj, tenant_config)
            except GatewayQuotaExceeded as exc:
                raise HTTPException(
                    status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)
                )
            except GatewayConfigurationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                )
            except GatewayChainExhausted as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
                )
            except GatewayError as exc:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc)
                )
    except AdmissionLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many concurrent requests",
            headers={"Retry-After": str(int(exc.retry_after))},
        )

    response_text = result.content
    usage_data = result.usage

    return ChatCompletionResponse(
        id=completion_id,
        created=created_ts,
        model=model_name,
        choices=[
            ChatCompletionChoice(
                index=0,
                message={"role": "assistant", "content": response_text},
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=usage_data.get("input_tokens", 0),
            completion_tokens=usage_data.get("output_tokens", 0),
            total_tokens=usage_data.get("input_tokens", 0)
            + usage_data.get("output_tokens", 0),
        ),
    )


# ---------------------------------------------------------------------------
# Streaming completion
# ---------------------------------------------------------------------------


async def _stream_completion(
    *,
    completion_id: str,
    created_ts: int,
    model_name: str,
    tenant_id: str,
    tenant_row: dict,
    user_message: str,
    messages: list[ChatMessage],
    request: ChatCompletionRequest,
) -> AsyncIterator[str]:
    """Stream chat completion chunks in the OpenAI SSE format."""
    import json

    gateway = get_gateway()
    tenant_config = tenant_config_from_data(tenant_row)
    history = _build_history(messages)

    request_obj = GatewayRequest(
        tenant_id=tenant_id,
        messages=[*history, {"role": "user", "content": user_message}],
        request_id=completion_id,
        provider="openai",
        model=request.model if request.model != "auto" else None,
        strategy="cost",
        temperature=request.temperature if request.temperature is not None else 0.7,
        max_tokens=request.max_tokens,
    )

    def _chunk(delta: dict[str, Any], finish_reason: str | None = None) -> str:
        chunk = ChatCompletionChunk(
            id=completion_id,
            created=created_ts,
            model=model_name,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=delta,
                    finish_reason=finish_reason,
                )
            ],
        )
        return f"data: {json.dumps(chunk.model_dump())}\n\n"

    def _error(error_type: str, code: str, message: str) -> str:
        error_data = {
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
            }
        }
        return f"data: {json.dumps(error_data)}\n\n"

    # Send the initial role chunk
    yield _chunk({"role": "assistant", "content": ""})

    admission = get_admission_gate()
    try:
        async with admission.admit(tenant_id):
            try:
                async for event in gateway.stream(request_obj, tenant_config):
                    if event.type == "delta" and event.content:
                        yield _chunk({"content": event.content})
                    elif event.type == "error":
                        yield _error("server_error", "provider_error", event.error or "Provider error")
                        yield "data: [DONE]\n\n"
                        return
            except GatewayQuotaExceeded as exc:
                yield _error("insufficient_quota", "quota_exceeded", str(exc))
                yield "data: [DONE]\n\n"
                return
            except GatewayConfigurationError as exc:
                yield _error("server_error", "gateway_configuration", str(exc))
                yield "data: [DONE]\n\n"
                return
            except GatewayChainExhausted as exc:
                yield _error("server_error", "provider_error", str(exc))
                yield "data: [DONE]\n\n"
                return
            except GatewayError as exc:
                yield _error("server_error", "internal_error", str(exc))
                yield "data: [DONE]\n\n"
                return
    except AdmissionLimitExceeded as exc:
        yield _error("rate_limit_exceeded", "admission_limit", "Too many concurrent requests")
        yield "data: [DONE]\n\n"
        return

    # Final chunk with finish_reason
    yield _chunk({}, finish_reason="stop")
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_history(messages: list[ChatMessage]) -> list[dict[str, str]]:
    """Convert the OpenAI messages array into the internal history format.

    Skips the last user message (already extracted as the current message)
    and maps tool/system messages into the simplified history format.
    """
    history: list[dict[str, str]] = []
    # Find index of the last user message to exclude it
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == "user":
            last_user_idx = i
            break

    for i, msg in enumerate(messages):
        if i == last_user_idx:
            continue  # Skip the current user message
        if msg.content is not None:
            history.append({"role": msg.role, "content": msg.content})

    return history
