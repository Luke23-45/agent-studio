"""StreamingModerationWindow unit tests (Phase 4, Arch 9.1 step 7)."""

from types import SimpleNamespace

import pytest

from backend.app.application.validation.streaming import StreamingModerationWindow


async def _allowed(text=None, allowed=True, redacted=None, violations=None, **kwargs):
    return SimpleNamespace(
        allowed=allowed, redacted_text=redacted, violations=violations or []
    )


@pytest.mark.asyncio
async def test_chunks_held_until_window_fills():
    window = StreamingModerationWindow(_allowed, window_size=10)
    released = []
    for chunk in ["hello", " world", "!"]:
        out = await window.push(chunk)
        if out is not None:
            released.append(out)
    # "hello" (5) stays under 10; "hello world" (11) crosses the window on
    # the second push and is validated/released whole. "!" stays pending.
    assert [r.text for r in released] == ["hello world"]
    assert all(r.released_raw for r in released)
    tail = await window.finish()
    assert tail is not None and tail.text == "!"


@pytest.mark.asyncio
async def test_passthrough_when_disabled():
    window = StreamingModerationWindow(None, window_size=10)
    out = await window.push("anything arbitrary long")
    assert out is not None
    assert out.text == "anything arbitrary long"
    assert out.released_raw is True
    assert not out.redacted and not out.truncated


@pytest.mark.asyncio
async def test_redaction_replaces_text_but_stream_continues():
    calls = []

    async def validator(text):
        calls.append(text)
        return await _allowed(redacted="REDACTED")

    window = StreamingModerationWindow(validator, window_size=5)
    out = await window.push("secret ssn here")
    assert out is not None
    assert out.text == "REDACTED"
    assert out.redacted is True
    assert not out.truncated
    assert calls == ["secret ssn here"]


@pytest.mark.asyncio
async def test_blocked_violation_truncates_stream():
    violations = [{"category": "HARMFUL_CONTENT"}]

    async def validator(text):
        return await _allowed(allowed=False, violations=violations)

    window = StreamingModerationWindow(validator, window_size=5)
    out = await window.push("bad output chars")
    assert out is not None
    assert out.truncated is True
    assert window.truncated is True
    assert out.text == "[content withheld]"
    assert out.violations == violations
    # Subsequent pushes release nothing.
    assert await window.push("more") is None


@pytest.mark.asyncio
async def test_validator_error_fails_open():
    async def validator(text):
        raise RuntimeError("guardrails down")

    window = StreamingModerationWindow(validator, window_size=5)
    out = await window.push("still stream this")
    assert out is not None
    assert out.text == "still stream this"
    assert out.released_raw is True
    assert not out.truncated


@pytest.mark.asyncio
async def test_finish_flushes_held_tail():
    window = StreamingModerationWindow(_allowed, window_size=100)
    out = await window.push("short")
    assert out is None  # 5 < 100 -> held
    tail = await window.finish()
    assert tail is not None and tail.text == "short"
    assert await window.finish() is None


@pytest.mark.asyncio
async def test_constructor_rejects_bad_window():
    with pytest.raises(ValueError):
        StreamingModerationWindow(None, window_size=0)


async def _collect(window: StreamingModerationWindow, chunks: list[str]):
    out = []
    for chunk in chunks:
        release = await window.push(chunk)
        if release is not None:
            out.append(release.text)
    tail = await window.finish()
    if tail is not None:
        out.append(tail.text)
    return out


@pytest.mark.asyncio
async def test_tenants_with_bigger_window_batch_more_before_release():
    """P4-3 (D-3): the per-tenant window knob is a latency/safety dial.

    A stricter tenant (larger window) holds more text : the validator sees
    a bigger context before the first release; a latency-sensitive tenant
    (smaller window) releases smaller chunks.
    """
    absorb = []
    async def validator(text):
        absorb.append(text)
        return await _allowed()

    strict = StreamingModerationWindow(validator, window_size=64)
    loose = StreamingModerationWindow(validator, window_size=8)

    text = "the quick brown fox jumps over the lazy dog again"
    strict_out = await _collect(strict, list(text))
    loose_out = await _collect(loose, list(text))

    # The strict tenant (64) holds the whole 44-char text in one window and
    # releases it as a single chunk; the loose tenant (8) streams smaller
    # validated windows as soon as they fill.
    assert strict_out == [text]
    assert "".join(loose_out) == text
    assert loose_out and all(len(chunk) <= len(text) for chunk in loose_out)
    assert any(len(chunk) < 64 for chunk in loose_out)
    # Every character was validated by the (shared) validator exactly once
    # per tenant stream.
    assert "".join(absorb) == text * 2