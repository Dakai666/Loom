"""Contract tests: quota-exhaustion fallback in LLMRouter.

Invariants:
- No fallback configured → provider errors propagate unchanged.
- Only quota/entitlement failures fail over; transient/auth/protocol errors don't.
- Failover happens only before any output was yielded (no duplicated text).
- The cooldown is process-wide (quota is account-wide), not per router instance.
- Listeners hear one "failover" per incident and one "restored" when the
  primary serves again — not one event per call.
"""
from __future__ import annotations

from typing import Any

import pytest

from loom.core.cognition import router as router_mod
from loom.core.cognition.providers import LLMResponse
from loom.core.cognition.responses_sse import ResponsesProviderError
from loom.core.cognition.router import LLMRouter


def _quota_error(retry_after: float | None = None) -> ResponsesProviderError:
    return ResponsesProviderError(
        provider="ChatGPT", model="gpt-6.1-terra", failure_type="entitlement_or_quota",
        phase="headers_received", detail="HTTP 429; usage_limit_reached",
        retry_after_seconds=retry_after,
    )


def _error(failure_type: str) -> ResponsesProviderError:
    return ResponsesProviderError(
        provider="ChatGPT", model="gpt-6.1-terra", failure_type=failure_type,
        phase="headers_received", detail="boom",
    )


class FakeProvider:
    """Minimal provider: scripted failures, records every call."""

    def __init__(self, name: str, model: str, *, fail: list[Exception | None] | None = None,
                 chunks: tuple[str, ...] = ("hi",), fail_after_first_chunk: bool = False,
                 native_max: int | None = None):
        self.name = name
        self.model = model
        self.fail = list(fail or [])
        self.chunks = chunks
        self.fail_after_first_chunk = fail_after_first_chunk
        self.native_max = native_max
        self.calls: list[dict[str, Any]] = []

    def for_model(self, model: str) -> "FakeProvider":
        return self

    def native_max_tokens(self, model: str) -> int | None:
        return self.native_max

    def _next_failure(self) -> Exception | None:
        return self.fail.pop(0) if self.fail else None

    async def chat(self, messages, tools=None, max_tokens=8096) -> LLMResponse:
        self.calls.append({"kind": "chat", "max_tokens": max_tokens})
        if (exc := self._next_failure()) is not None:
            raise exc
        return LLMResponse(text=f"{self.name}-reply", tool_uses=[], stop_reason="end_turn")

    async def stream_chat(self, messages, tools=None, max_tokens=8096, *, abort_signal=None):
        self.calls.append({"kind": "stream", "max_tokens": max_tokens})
        exc = self._next_failure()
        if exc is not None and not self.fail_after_first_chunk:
            raise exc
        for i, chunk in enumerate(self.chunks):
            yield chunk, None
            if exc is not None and i == 0:
                raise exc
        yield "", LLMResponse(text="".join(self.chunks), tool_uses=[], stop_reason="end_turn")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(router_mod, "_clock", c)
    router_mod.reset_failover_state()
    yield c
    router_mod.reset_failover_state()


def _router(primary: FakeProvider, fallback: FakeProvider | None, *,
            fallback_model: str | None = "deepseek-flash", **kwargs) -> LLMRouter:
    r = LLMRouter()
    r.register(primary, default=True)
    if fallback is not None:
        r.register(fallback, default=False, fallback=False)
    if fallback_model:
        r.set_quota_fallback(fallback_model, **kwargs)
    return r


async def _drain(r: LLMRouter, model: str = "chatgpt/gpt-6.1-terra", max_tokens: int = 8096):
    chunks, final = [], None
    async for chunk, resp in r.stream_chat(model=model, messages=[], max_tokens=max_tokens):
        if resp is None:
            chunks.append(chunk)
        else:
            final = resp
    return "".join(chunks), final


async def test_without_fallback_quota_error_propagates_unchanged():
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    r = _router(primary, FakeProvider("deepseek", "deepseek-flash"), fallback_model=None)
    with pytest.raises(ResponsesProviderError) as exc:
        await _drain(r)
    assert exc.value.failure_type == "entitlement_or_quota"


async def test_quota_error_before_output_is_served_by_fallback():
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    fallback = FakeProvider("deepseek", "deepseek-flash", chunks=("from-", "deepseek"))
    r = _router(primary, fallback)
    text, final = await _drain(r)
    assert text == "from-deepseek"
    assert final is not None and final.text == "from-deepseek"
    assert len(fallback.calls) == 1


async def test_fallback_call_uses_max_tokens_resolved_for_fallback_model():
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    fallback = FakeProvider("deepseek", "deepseek-flash")
    seen: list[str] = []

    def resolver(model: str) -> int:
        seen.append(model)
        return 131072

    r = _router(primary, fallback, max_tokens_resolver=resolver)
    await _drain(r, max_tokens=8192)
    assert seen == ["deepseek-flash"]
    assert fallback.calls[0]["max_tokens"] == 131072


@pytest.mark.parametrize("failure_type", [
    "http_error", "credential_missing", "ttfb_timeout", "idle_timeout",
    "stream_incomplete", "provider_protocol_error", "stream_error_event",
])
async def test_non_quota_failures_do_not_fail_over(failure_type):
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_error(failure_type)])
    fallback = FakeProvider("deepseek", "deepseek-flash")
    r = _router(primary, fallback)
    with pytest.raises(ResponsesProviderError):
        await _drain(r)
    assert fallback.calls == []
    assert r.effective_model("chatgpt/gpt-6.1-terra") == "chatgpt/gpt-6.1-terra"


async def test_quota_error_after_output_started_is_not_replayed_on_fallback():
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()],
                           chunks=("partial", "rest"), fail_after_first_chunk=True)
    fallback = FakeProvider("deepseek", "deepseek-flash")
    r = _router(primary, fallback)
    with pytest.raises(ResponsesProviderError):
        await _drain(r)
    assert fallback.calls == []


async def test_cooldown_skips_primary_and_is_shared_across_router_instances(clock):
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    fallback = FakeProvider("deepseek", "deepseek-flash")
    await _drain(_router(primary, fallback, cooldown_seconds=1800))

    clock.now += 60
    primary2 = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra")
    fallback2 = FakeProvider("deepseek", "deepseek-flash", chunks=("fb",))
    r2 = _router(primary2, fallback2, cooldown_seconds=1800)  # e.g. another session
    assert r2.effective_model("chatgpt/gpt-6.1-terra") == "deepseek-flash"
    text, _ = await _drain(r2)
    assert text == "fb"
    assert primary2.calls == []


async def test_retry_after_sets_cooldown_and_primary_is_retried_after_expiry(clock):
    events: list[tuple[str, str, str]] = []
    router_mod.add_failover_listener(lambda e: events.append((e.kind, e.primary_model, e.fallback_model)))
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error(retry_after=120)])
    fallback = FakeProvider("deepseek", "deepseek-flash")
    r = _router(primary, fallback, cooldown_seconds=1800)
    await _drain(r)

    clock.now += 60
    await _drain(r)
    assert len(primary.calls) == 1  # still cooling down

    clock.now += 61
    assert r.effective_model("chatgpt/gpt-6.1-terra") == "chatgpt/gpt-6.1-terra"
    text, _ = await _drain(r)
    assert text == "hi"
    assert len(primary.calls) == 2
    assert events == [
        ("failover", "chatgpt/gpt-6.1-terra", "deepseek-flash"),
        ("restored", "chatgpt/gpt-6.1-terra", "deepseek-flash"),
    ]


async def test_listener_hears_one_failover_per_incident_not_per_call(clock):
    events: list[str] = []
    router_mod.add_failover_listener(lambda e: events.append(e.kind))
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    r = _router(primary, FakeProvider("deepseek", "deepseek-flash"))
    for _ in range(3):
        await _drain(r)
    assert events == ["failover"]


async def test_failing_listener_does_not_break_the_call():
    def boom(_event):
        raise RuntimeError("listener bug")

    router_mod.add_failover_listener(boom)
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    r = _router(primary, FakeProvider("deepseek", "deepseek-flash", chunks=("ok",)))
    text, _ = await _drain(r)
    assert text == "ok"


async def test_non_streaming_chat_also_fails_over():
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    fallback = FakeProvider("deepseek", "deepseek-flash")
    r = _router(primary, fallback)
    resp = await r.chat(model="chatgpt/gpt-6.1-terra", messages=[])
    assert resp.text == "deepseek-reply"


async def test_quota_error_on_the_fallback_itself_propagates():
    primary = FakeProvider("deepseek", "deepseek-flash", fail=[_quota_error()])
    r = _router(primary, None)
    with pytest.raises(ResponsesProviderError):
        await _drain(r, model="deepseek-flash")


async def test_fallback_provider_not_registered_disables_failover():
    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error()])
    r = _router(primary, None)
    with pytest.raises(ResponsesProviderError):
        await _drain(r)


# ── Wiring: config, session view, operator notification ────────────────────

def _build(monkeypatch, cognition: dict, env: dict):
    from loom.core import session
    monkeypatch.setattr(session, "_load_env", lambda: env)
    monkeypatch.setattr(session, "_load_loom_config", lambda: {"cognition": cognition})
    monkeypatch.setattr(router_mod, "get_default_model", lambda: "chatgpt/gpt-6.1-terra")
    return session.build_router()


async def test_build_router_wires_fallback_model_with_its_own_output_budget(monkeypatch):
    r = _build(monkeypatch, {"fallback_model": "deepseek-flash"}, {"DEEPSEEK_API_KEY": "k"})
    assert r._quota_fallback == "deepseek-flash"
    # Fallback calls get the fallback model's native cap, not the primary's.
    assert r._fallback_max_tokens("deepseek-flash", 8192) == 131072


def test_build_router_disables_fallback_when_its_provider_is_unavailable(monkeypatch, caplog):
    r = _build(monkeypatch, {"fallback_model": "deepseek-flash"}, {})
    assert r._quota_fallback is None
    assert "quota fallback disabled" in caplog.text


def test_build_router_without_fallback_model_keeps_current_behaviour(monkeypatch):
    r = _build(monkeypatch, {}, {"DEEPSEEK_API_KEY": "k"})
    assert r._quota_fallback is None


def test_session_reports_the_serving_model_during_failover(clock):
    from loom.core.session import LoomSession

    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra")
    r = _router(primary, FakeProvider("deepseek", "deepseek-flash"))
    sess = LoomSession.__new__(LoomSession)
    sess.router = r
    sess._active_model = lambda: "chatgpt/gpt-6.1-terra"
    assert sess.current_model() == "chatgpt/gpt-6.1-terra"
    router_mod._cooldown_until["chatgpt"] = clock.now + 600
    assert sess.current_model() == "deepseek-flash"
    clock.now += 601
    assert sess.current_model() == "chatgpt/gpt-6.1-terra"


def test_session_serving_model_tolerates_routers_without_fallback_support():
    from types import SimpleNamespace
    from loom.core.session import LoomSession

    sess = LoomSession.__new__(LoomSession)
    sess.router = SimpleNamespace()
    assert sess._serving_model("m") == "m"


async def test_daemon_notifies_operator_once_per_failover_and_on_restore(clock):
    import asyncio
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.types import NotificationType

    sent = []

    class Notify:
        async def send(self, notification):
            sent.append(notification)

    daemon = AutonomyDaemon.__new__(AutonomyDaemon)
    daemon._notify = Notify()
    daemon._failover_notices = set()
    router_mod.add_failover_listener(daemon._on_quota_failover)

    primary = FakeProvider("chatgpt", "chatgpt/gpt-6.1-terra", fail=[_quota_error(retry_after=600)])
    r = _router(primary, FakeProvider("deepseek", "deepseek-flash"))
    await _drain(r)
    await _drain(r)
    clock.now += 601
    await _drain(r)
    await asyncio.sleep(0)

    assert [n.type for n in sent] == [NotificationType.ALERT, NotificationType.INFO]
    assert "deepseek-flash" in sent[0].body and "10 min" in sent[0].body
