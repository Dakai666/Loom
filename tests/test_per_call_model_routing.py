"""
Contract: the model the router is asked for is the model on the wire.

Before this fix the router used the model name only to *pick* a provider;
the request then went out with ``provider.model``, frozen at build time.
Tier switches never touched it, so with loom.toml saying T1 =
``minimax-m3.1-flash`` / T2 = ``deepseek-flash`` the wire carried
``MiniMax-M2.7`` / ``deepseek-v4-pro`` — and every display surface showed
the configured name (the drift #471 was meant to rule out).

Invariants:
  - ``router.chat`` / ``router.stream_chat`` send exactly the requested model.
  - The registered provider is never mutated per call, so concurrent calls
    with different models (judge / reflection alongside the main turn)
    can't stomp each other.
  - Routing prefixes are still stripped for prefixed providers.
  - ``build_router`` recognises lowercase MiniMax names as the default.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from loom.core.cognition.providers import AnthropicProvider, OpenAIProvider
from loom.core.cognition.router import LLMRouter


def _fake_message() -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=1, output_tokens=1,
            cache_read_input_tokens=0, cache_creation_input_tokens=0,
        ),
    )


class _SyncMessages:
    def __init__(self) -> None:
        self.models: list[str] = []

    def create(self, **kwargs: Any) -> Any:
        self.models.append(kwargs["model"])
        return _fake_message()


class _Stream:
    def __init__(self, delay: float) -> None:
        async def _gen():
            await asyncio.sleep(delay)
            yield "ok"
        self.text_stream = _gen()

    async def __aenter__(self) -> "_Stream":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def get_final_message(self) -> Any:
        return _fake_message()


class _AsyncMessages:
    def __init__(self) -> None:
        self.models: list[str] = []

    def stream(self, **kwargs: Any) -> _Stream:
        self.models.append(kwargs["model"])
        # Longer delay for the first call so the two calls overlap.
        return _Stream(0.05 if len(self.models) == 1 else 0.0)


def _anthropic(name: str, model: str) -> AnthropicProvider:
    p = AnthropicProvider(
        api_key="test", model=model,
        base_url="https://example.invalid/anthropic", name=name,
    )
    p._client = SimpleNamespace(messages=_SyncMessages())
    p._async_client = SimpleNamespace(messages=_AsyncMessages())
    return p


def _router(*providers: AnthropicProvider) -> LLMRouter:
    r = LLMRouter()
    for i, p in enumerate(providers):
        r.register(p, default=(i == 0))
    return r


MSGS = [{"role": "user", "content": "hi"}]


async def _drain(router: LLMRouter, model: str) -> None:
    async for _ in router.stream_chat(model=model, messages=MSGS, max_tokens=64):
        pass


class TestRequestedModelReachesWire:
    async def test_chat_sends_requested_model(self):
        mm = _anthropic("minimax", "MiniMax-M2.7")
        await _router(mm).chat(model="minimax-m3.1-flash", messages=MSGS, max_tokens=64)
        assert mm._client.messages.models == ["minimax-m3.1-flash"]

    async def test_stream_sends_requested_model(self):
        mm = _anthropic("minimax", "MiniMax-M2.7")
        await _drain(_router(mm), "minimax-m3.1-flash")
        assert mm._async_client.messages.models == ["minimax-m3.1-flash"]

    async def test_same_provider_tier_switch(self):
        ds = _anthropic("deepseek", "deepseek-v4-pro")
        r = _router(_anthropic("minimax", "MiniMax-M2.7"), ds)
        await _drain(r, "deepseek-flash")
        await _drain(r, "deepseek-v4-pro")
        assert ds._async_client.messages.models == ["deepseek-flash", "deepseek-v4-pro"]

    async def test_registered_provider_not_mutated(self):
        mm = _anthropic("minimax", "MiniMax-M2.7")
        await _drain(_router(mm), "minimax-m3.1-flash")
        assert mm.model == "MiniMax-M2.7"

    async def test_concurrent_calls_keep_their_own_model(self):
        mm = _anthropic("minimax", "MiniMax-M2.7")
        r = _router(mm)
        await asyncio.gather(
            _drain(r, "minimax-m3.1-flash"),
            r.chat(model="MiniMax-M3", messages=MSGS, max_tokens=64),
        )
        assert mm._async_client.messages.models == ["minimax-m3.1-flash"]
        assert mm._client.messages.models == ["MiniMax-M3"]

    def test_prefixed_provider_strips_prefix(self):
        p = OpenAIProvider(api_key="test", model="openai/gpt-4o")
        bound = p.for_model("openai/gpt-5")
        assert bound._api_model() == "gpt-5"
        assert p._api_model() == "gpt-4o"

    def test_for_model_same_name_returns_self(self):
        p = _anthropic("minimax", "MiniMax-M2.7")
        assert p.for_model("MiniMax-M2.7") is p


class TestBuildRouterDefault:
    def test_lowercase_minimax_default_is_honoured(self, monkeypatch):
        import loom.core.session as session_mod
        monkeypatch.setattr(session_mod, "_load_env", lambda: {"MINIMAX_API_KEY": "x"})
        # One config source for the default model (#661).
        monkeypatch.setattr(session_mod, "_load_loom_config", lambda: {
            "cognition": {"default_model": "minimax-m3.1-flash"},
        })
        r = session_mod.build_router()
        assert r._providers["minimax"].model == "minimax-m3.1-flash"
