"""Shared gateway test double for orchestration tests (P3-9).

The orchestration layer now talks to the gateway facade only. These fakes
let existing adapter-stub tests keep working unchanged: ``generate``
delegates to a legacy ``chat(messages)`` stub (converting request dicts
back to ``LLMMessage`` objects) and ``stream`` pops scripted
``GatewayStreamEvent`` lists.
"""

from backend.app.adapters.llm.provider import LLMMessage
from backend.app.gateway.types import GatewayResult, GatewayStreamEvent


def _llm_messages(messages):
    return [
        LLMMessage(
            role=m.get("role", "user"),
            content=m.get("content", ""),
            metadata=m.get("metadata") or {},
        )
        for m in messages
    ]


def result(content, model="gpt-4", provider="openai", usage=None,
           tool_calls=None, finish_reason=None):
    return GatewayResult(
        content=content,
        model=model,
        provider=provider,
        usage=usage or {},
        tool_calls=tool_calls,
        finish_reason=finish_reason,
    )


def delta(text, provider="openai", model="gpt-4"):
    return GatewayStreamEvent(type="delta", content=text, provider=provider, model=model)


def usage_event(usage, provider="openai", model="gpt-4"):
    return GatewayStreamEvent(type="usage", usage=usage, provider=provider, model=model)


def done(provider="openai", model="gpt-4", finish_reason="stop"):
    return GatewayStreamEvent(
        type="done", finish_reason=finish_reason, provider=provider, model=model
    )


def error_event(text, failure_class="general"):
    return GatewayStreamEvent(type="error", error=text, failure_class=failure_class)


class FakeGateway:
    """Programmable ``Gateway`` double.

    ``generate``: when ``chat_stub`` is set, delegates to
    ``chat_stub.chat(messages)`` (legacy adapter stubs keep working
    unchanged; ``config.tools`` is refreshed from the request so
    ``tools_seen`` assertions still hold). Otherwise pops ``chat_results``
    (``GatewayResult`` or callable taking the request) or raises
    ``chat_error``.

    ``stream``: pops ``stream_scripts`` (each a list of
    ``GatewayStreamEvent``) or raises ``stream_error``.

    Records every call: ``requests`` (``GatewayRequest``),
    ``generate_calls``, ``stream_calls``.
    """

    def __init__(
        self,
        chat_stub=None,
        chat_results=None,
        chat_error=None,
        stream_scripts=None,
        stream_error=None,
    ):
        self.chat_stub = chat_stub
        self.chat_results = list(chat_results or [])
        self.chat_error = chat_error
        self.stream_scripts = [list(s) for s in (stream_scripts or [])]
        self.stream_error = stream_error
        self.requests = []
        self.generate_calls = 0
        self.stream_calls = 0

    async def generate(self, request, tenant_config, gateway_cfg=None):
        self.generate_calls += 1
        self.requests.append(request)
        if self.chat_error is not None:
            raise self.chat_error
        if self.chat_stub is not None:
            if hasattr(self.chat_stub, "config"):
                self.chat_stub.config.tools = list(request.tools)
            response = await self.chat_stub.chat(_llm_messages(request.messages))
            return GatewayResult(
                content=getattr(response, "content", "") or "",
                model=getattr(response, "model", "") or request.model or "gpt-4",
                provider=request.provider or "openai",
                usage=getattr(response, "usage", None) or {},
                finish_reason=getattr(response, "finish_reason", None),
                tool_calls=getattr(response, "tool_calls", None),
            )
        if self.chat_results:
            item = self.chat_results.pop(0)
            return item(request) if callable(item) else item
        return result("", model=request.model or "gpt-4", provider=request.provider or "openai")

    async def stream(self, request, tenant_config, gateway_cfg=None):
        self.stream_calls += 1
        self.requests.append(request)
        if self.stream_error is not None:
            raise self.stream_error
        script = self.stream_scripts.pop(0) if self.stream_scripts else []
        for ev in script:
            yield ev
