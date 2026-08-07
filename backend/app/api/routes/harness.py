"""
Harness Workbench API routes (P7-5, Arch §13, AGENTS.md).

Internal-only surface for operator sessions: provider testing, model
comparisons, prompt iteration and A/B, session fork/replay, redacted-trace
replay, and adversarial sweeps.

AGENTS.md boundary enforcement:
  - All routes require an operator API key (role >= operator).
  - Harness sessions are tagged kind='internal' — they NEVER serve
    customer traffic and NEVER touch customer session state.
  - No production tenant state is stored in the harness.
  - The harness talks to the SAME gateway the customer runtime uses
    (same policy engine, same auth boundaries) but via operator-keyed
    sessions that are permanently isolated from customer threads.
"""

import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from backend.app.api.dependencies.auth import (
    ROLE_OPERATOR,
    ROLE_SUPER_ADMIN,
    ROLE_TENANT_ADMIN,
    ApiKeyPrincipal,
    get_principal,
)
from backend.app.domain.tenant import TenantConfig
from backend.app.gateway.service import Gateway, get_gateway
from backend.app.gateway.types import (
    GatewayChainExhausted,
    GatewayConfigurationError,
    GatewayError,
    GatewayQuotaExceeded,
    GatewayRequest,
    GatewayResult,
)
from backend.app.settings.env import settings

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/harness", tags=["Harness (Internal Only)"])

# ---------------------------------------------------------------------------
# In-memory session store (operator workbench is stateless across restarts;
# durable storage is a P7-5 follow-on when needed).
# ---------------------------------------------------------------------------

_SESSIONS: dict[str, dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HarnessSessionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    kind: str = Field(
        ...,
        description="One of: provider_test, model_comparison, prompt_ab, fork_replay, adversarial_sweep",
    )
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    config: dict[str, Any] = Field(default_factory=dict)


class HarnessSendMessage(BaseModel):
    message: str = Field(..., min_length=1)
    model: str | None = None
    provider: str | None = None


class HarnessForkRequest(BaseModel):
    at_message_id: str | None = None


class HarnessReplayRequest(BaseModel):
    model: str | None = None
    provider: str | None = None


class HarnessCompareRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    models: list[dict[str, str]] = Field(
        ..., min_length=1, description="[{provider, model}, ...]"
    )
    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_message(role: str, content: str, **meta: Any) -> dict[str, Any]:
    return {
        "id": f"msg-{uuid4().hex[:16]}",
        "role": role,
        "content": content,
        "created_at": datetime.now(UTC).isoformat(),
        **meta,
    }


def _session_to_response(session: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": session["id"],
        "name": session["name"],
        "kind": session["kind"],
        "status": session["status"],
        "provider": session["provider"],
        "model": session["model"],
        "config": session["config"],
        "messages": session["messages"],
        "created_at": session["created_at"],
        "updated_at": session["updated_at"],
    }


def _require_operator(principal: ApiKeyPrincipal) -> None:
    """AGENTS.md boundary: harness is operators only, never customer-facing."""
    if principal.role not in (ROLE_SUPER_ADMIN, ROLE_TENANT_ADMIN, ROLE_OPERATOR):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Role '{principal.role}' does not have harness access (operator or above required)",
        )


def _harness_tenant_config(provider: str, model: str) -> TenantConfig:
    """Internal tenant slot for operator testing — never a customer tenant."""
    return TenantConfig(
        name="Harness Internal",
        slug="harness-internal",
        default_provider=provider or "openai",
        default_model=model or "gpt-4",
    )


async def _harness_generate(
    gateway: Gateway,
    *,
    provider: str,
    model: str,
    message: str,
    history: list[dict[str, str]],
) -> GatewayResult:
    """Run one generation through the production gateway with a pinned deployment.

    ``pinned`` forces exactly the selected provider/model — no tiering or
    fallback — so operator comparisons test one deployment at a time.
    """
    tenant_config = _harness_tenant_config(provider, model)
    request = GatewayRequest(
        tenant_id=str(tenant_config.id),
        messages=[*history, {"role": "user", "content": message}],
        request_id=f"harness-{uuid4().hex[:16]}",
        provider=provider,
        model=model,
        strategy="cost",
        temperature=0.7,
        pinned=f"{provider}:{model}",
    )
    return await gateway.generate(request, tenant_config)


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


@router.get("/sessions", summary="List harness sessions (operator only)")
async def list_harness_sessions(
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> list[dict[str, Any]]:
    _require_operator(principal)
    return [_session_to_response(s) for s in _SESSIONS.values()]


@router.post("/sessions", summary="Create a harness session (operator only)")
async def create_harness_session(
    request: HarnessSessionCreate,
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    _require_operator(principal)

    valid_kinds = {"provider_test", "model_comparison", "prompt_ab", "fork_replay", "adversarial_sweep"}
    if request.kind not in valid_kinds:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid kind '{request.kind}'. Must be one of: {', '.join(sorted(valid_kinds))}",
        )

    session_id = str(uuid4())
    now = datetime.now(UTC).isoformat()

    session: dict[str, Any] = {
        "id": session_id,
        "name": request.name,
        "kind": request.kind,
        "status": "active",
        "provider": request.provider,
        "model": request.model,
        "config": request.config,
        "messages": [],
        "created_at": now,
        "updated_at": now,
        # AGENTS.md: tag all operator sessions as internal
        "_internal": True,
        "_operator_key_id": principal.key_id,
    }
    _SESSIONS[session_id] = session

    logger.info(
        "harness_session_created",
        session_id=session_id,
        kind=request.kind,
        operator=principal.key_id,
    )

    return _session_to_response(session)


@router.get("/sessions/{session_id}", summary="Get a harness session")
async def get_harness_session(
    session_id: str,
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    _require_operator(principal)
    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return _session_to_response(session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a harness session")
async def delete_harness_session(
    session_id: str,
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> None:
    _require_operator(principal)
    if session_id not in _SESSIONS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    del _SESSIONS[session_id]
    logger.info("harness_session_deleted", session_id=session_id, operator=principal.key_id)


# ---------------------------------------------------------------------------
# Send message
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/messages", summary="Send a message in a harness session")
async def send_harness_message(
    session_id: str,
    request: HarnessSendMessage,
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    _require_operator(principal)

    session = _SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    if session["status"] != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Session is {session['status']}, not active",
        )

    provider = request.provider or session["provider"]
    model = request.model or session["model"]

    # Append user message
    user_msg = _make_message("user", request.message, provider=provider, model=model)
    session["messages"].append(user_msg)

    # Call gateway
    gateway = get_gateway()
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in session["messages"][:-1]  # exclude current user message
    ]

    t0 = time.monotonic()
    try:
        result = await _harness_generate(
            gateway,
            provider=provider,
            model=model,
            message=request.message,
            history=history,
        )
    except GatewayQuotaExceeded as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc))
    except GatewayConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    except GatewayChainExhausted as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except GatewayError as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))

    latency_ms = int((time.monotonic() - t0) * 1000)
    response_text = result.content
    usage = result.usage

    assistant_msg = _make_message(
        "assistant",
        response_text,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        cost=usage.get("cost", 0.0),
        metadata={"usage": usage},
    )
    session["messages"].append(assistant_msg)
    session["updated_at"] = datetime.now(UTC).isoformat()

    logger.info(
        "harness_message_sent",
        session_id=session_id,
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        operator=principal.key_id,
    )

    return _session_to_response(session)


# ---------------------------------------------------------------------------
# Fork
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/fork", summary="Fork a harness session at a message boundary")
async def fork_harness_session(
    session_id: str,
    request: HarnessForkRequest,
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    """Fork any thread at a message boundary (Arch §7.1, P7-5).

    Creates a new active session that copies the original's history up to the
    fork point. The original session's ID and history stay unchanged; both
    can then be resumed independently. This implements the same pattern as
    OpenCode's fork and Claude Agent SDK's ``fork_session``.
    """
    _require_operator(principal)

    source = _SESSIONS.get(session_id)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    # Find fork point
    messages = source["messages"]
    if request.at_message_id:
        fork_idx = next(
            (i for i, m in enumerate(messages) if m["id"] == request.at_message_id), None
        )
        if fork_idx is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Message {request.at_message_id} not found in session",
            )
        messages_to_copy = messages[:fork_idx + 1]
    else:
        messages_to_copy = list(messages)

    now = datetime.now(UTC).isoformat()
    new_id = str(uuid4())

    import copy

    forked: dict[str, Any] = {
        **copy.deepcopy(source),
        "id": new_id,
        "name": f"{source['name']} (fork)",
        "status": "active",
        "messages": copy.deepcopy(messages_to_copy),
        "created_at": now,
        "updated_at": now,
        "_forked_from": session_id,
        "_fork_point": request.at_message_id,
    }
    _SESSIONS[new_id] = forked

    logger.info(
        "harness_session_forked",
        source_session_id=session_id,
        forked_session_id=new_id,
        fork_point=request.at_message_id,
        operator=principal.key_id,
    )

    return _session_to_response(forked)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/replay", summary="Replay a session with a different model/provider")
async def replay_harness_session(
    session_id: str,
    request: HarnessReplayRequest,
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    """Replay all user turns from an existing session through a new model.

    Creates a new session with the same user messages, re-running each
    through the specified provider/model. Used for model comparison via
    controlled replay (Arch §13, P7-5 redacted-trace replay).
    """
    _require_operator(principal)

    source = _SESSIONS.get(session_id)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    provider = request.provider or source["provider"]
    model = request.model or source["model"]

    now = datetime.now(UTC).isoformat()
    new_id = str(uuid4())

    replay_session: dict[str, Any] = {
        "id": new_id,
        "name": f"{source['name']} (replay @ {model})",
        "kind": "fork_replay",
        "status": "active",
        "provider": provider,
        "model": model,
        "config": source["config"],
        "messages": [],
        "created_at": now,
        "updated_at": now,
        "_internal": True,
        "_replayed_from": session_id,
        "_operator_key_id": principal.key_id,
    }
    _SESSIONS[new_id] = replay_session

    # Replay user turns
    gateway = get_gateway()
    history: list[dict[str, str]] = []

    for msg in source["messages"]:
        if msg["role"] != "user":
            continue
        user_msg = _make_message("user", msg["content"])
        replay_session["messages"].append(user_msg)

        t0 = time.monotonic()
        try:
            result = await _harness_generate(
                gateway,
                provider=provider,
                model=model,
                message=msg["content"],
                history=history,
            )
        except GatewayError as exc:
            error_msg = _make_message("assistant", f"[Error during replay: {exc}]", error=True)
            replay_session["messages"].append(error_msg)
            history.append({"role": "user", "content": msg["content"]})
            history.append({"role": "assistant", "content": str(exc)})
            continue

        latency_ms = int((time.monotonic() - t0) * 1000)
        response_text = result.content
        usage = result.usage

        assistant_msg = _make_message(
            "assistant",
            response_text,
            provider=provider,
            model=model,
            latency_ms=latency_ms,
            tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            cost=usage.get("cost", 0.0),
            metadata={"usage": usage, "replayed": True},
        )
        replay_session["messages"].append(assistant_msg)
        history.append({"role": "user", "content": msg["content"]})
        history.append({"role": "assistant", "content": response_text})

    replay_session["updated_at"] = datetime.now(UTC).isoformat()

    logger.info(
        "harness_session_replayed",
        source_session_id=session_id,
        replay_session_id=new_id,
        provider=provider,
        model=model,
        operator=principal.key_id,
    )

    return _session_to_response(replay_session)


# ---------------------------------------------------------------------------
# Model comparison
# ---------------------------------------------------------------------------


@router.post("/compare", summary="Run a single prompt against multiple models simultaneously")
async def run_model_comparison(
    request: HarnessCompareRequest,
    principal: ApiKeyPrincipal = Depends(get_principal),
) -> dict[str, Any]:
    """Execute a comparison prompt against multiple provider/model pairs.

    Results are collected in parallel and returned together for side-by-side
    comparison. All calls are tagged internal (AGENTS.md boundary).
    """
    import asyncio

    _require_operator(principal)

    if len(request.models) > 10:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Maximum 10 models per comparison",
        )

    gateway = get_gateway()

    async def call_model(entry: dict[str, str]) -> dict[str, Any]:
        provider = entry.get("provider", "")
        model = entry.get("model", "")
        t0 = time.monotonic()
        try:
            result = await _harness_generate(
                gateway,
                provider=provider,
                model=model,
                message=request.prompt,
                history=[],
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            response_text = result.content
            usage = result.usage
            return {
                "provider": provider,
                "model": model,
                "response": response_text,
                "latency_ms": latency_ms,
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost": usage.get("cost", 0.0),
                "error": None,
            }
        except GatewayError as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            return {
                "provider": provider,
                "model": model,
                "response": "",
                "latency_ms": latency_ms,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.0,
                "error": str(exc),
            }

    results = await asyncio.gather(*[call_model(m) for m in request.models])

    comparison_id = str(uuid4())

    logger.info(
        "harness_comparison_run",
        comparison_id=comparison_id,
        model_count=len(request.models),
        operator=principal.key_id,
    )

    return {
        "id": comparison_id,
        "session_id": None,
        "prompt": request.prompt,
        "results": list(results),
        "created_at": datetime.now(UTC).isoformat(),
    }
