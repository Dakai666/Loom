import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from click.testing import CliRunner

from loom.core.cognition.chatgpt_auth import ChatGPTAuthError
from loom.core.cognition.chatgpt_provider import ChatGPTResponsesProvider
from loom.core.cognition.chatgpt_capabilities import describe_model, verify_model, ModelVerificationError
from loom.core.cognition.providers import LLMResponse, ToolUse
from loom.core.cognition.responses_sse import ResponsesProviderError
from loom.core.cognition.router import LLMRouter


def event(kind, **data):
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


def auth():
    return SimpleNamespace(access_token=lambda account=None: "loom-oauth",
                           catalog=lambda account=None: [{"slug": "model-a", "context_window": 128000}])


@pytest.mark.asyncio
async def test_public_endpoint_oauth_model_binding_image_and_usage():
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer loom-oauth"
        return httpx.Response(200, text=event("response.output_text.delta", delta="hello") + event(
            "response.completed", response={"status": "completed", "usage": {
                "input_tokens": 100, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 80}}}))
    provider = ChatGPTResponsesProvider(auth=auth(), transport=httpx.MockTransport(transport))
    router = LLMRouter().register(provider)
    result = await router.chat("chatgpt/model-a", [{"role": "system", "content": "system"}, {
        "role": "user", "content": [{"type": "text", "text": "describe"},
        {"type": "image", "source": {"kind": "url", "ref": "https://example.com/image.png"}}]}])
    body = requests[0]
    assert body["model"] == "model-a"
    assert body["stream"] is True and body["store"] is False
    assert body["instructions"] == "system"
    assert body["input"][0]["content"][1]["type"] == "input_image"
    assert not set(body) & {"max_output_tokens", "temperature", "previous_response_id"}
    assert body["reasoning"] == {"effort": "high"}
    assert result.text == "hello" and result.cache_read_input_tokens == 80
    assert provider.model == "chatgpt/gpt-6.1-sol"
    assert provider.native_context_window("chatgpt/model-a") == 128000


@pytest.mark.asyncio
async def test_namespaced_tool_call_result_replay_and_completed_only_calls():
    bodies = []
    def transport(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(200, text=event("response.completed", response={"status": "completed", "output": [{
                "type": "function_call", "namespace": "loom", "name": "lookup", "call_id": "call-1",
                "arguments": '{"query":"test"}'}]}))
        return httpx.Response(200, text=event("response.output_text.delta", delta="done") + event(
            "response.completed", response={"status": "completed"}))
    provider = ChatGPTResponsesProvider(auth=auth(), transport=httpx.MockTransport(transport))
    tools = [{"name": "lookup", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}]
    messages = [{"role": "user", "content": "lookup"}]
    result = await provider.chat(messages, tools)
    assert result.tool_uses[0].name == "lookup"
    assert bodies[0]["tools"][0]["type"] == "namespace"
    messages += [result.raw_message, provider.format_tool_result("call-1", "found")]
    replay = await provider.chat(messages, tools)
    assert replay.text == "done"
    assert bodies[1]["input"][1]["namespace"] == "loom"
    assert bodies[1]["input"][2] == {"type": "function_call_output", "call_id": "call-1", "output": "found"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [event("response.output_text.delta", delta="partial"),
                                   event("response.incomplete", response={"status": "incomplete"})])
async def test_partial_or_incomplete_stream_never_reported_as_success(stream):
    provider = ChatGPTResponsesProvider(auth=auth(), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=stream)))
    with pytest.raises(ResponsesProviderError, match="incomplete|without response.completed"):
        await provider.chat([{"role": "user", "content": "test"}])


@pytest.mark.asyncio
async def test_midstream_quota_failure_propagates_without_fallback(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-used")
    count = 0
    def transport(request):
        nonlocal count
        count += 1
        return httpx.Response(200, text=event("response.output_text.delta", delta="partial") + event(
            "response.failed", response={"status": "failed", "error": {
                "code": "subscription_sharing_usage_limit_exceeded", "message": "app limit reached"}}))
    provider = ChatGPTResponsesProvider(auth=auth(), transport=httpx.MockTransport(transport))
    with pytest.raises(ResponsesProviderError, match="subscription_sharing_usage_limit_exceeded"):
        await provider.chat([{"role": "user", "content": "test"}])
    assert count == 1


@pytest.mark.asyncio
async def test_http_admission_preserves_detail_request_id_and_redacts_token():
    provider = ChatGPTResponsesProvider(auth=auth(), transport=httpx.MockTransport(lambda r: httpx.Response(
        403, json={"detail": "scope rejected loom-oauth"}, headers={"x-request-id": "req-123"})))
    with pytest.raises(ResponsesProviderError) as caught:
        await provider.chat([{"role": "user", "content": "test"}])
    assert "HTTP 403" in str(caught.value) and "req-123" in str(caught.value)
    assert "detail" in str(caught.value) and "loom-oauth" not in str(caught.value)


@pytest.mark.asyncio
async def test_cancelled_stream_does_not_return_partial_success():
    stop = asyncio.Event()
    stop.set()
    provider = ChatGPTResponsesProvider(auth=auth(), transport=httpx.MockTransport(lambda r: httpx.Response(
        200, text=event("response.output_text.delta", delta="partial"))))
    with pytest.raises(asyncio.CancelledError):
        async for _ in provider.stream_chat([{"role": "user", "content": "test"}], abort_signal=stop):
            pass


def test_unreadable_image_degrades_without_discarding_adjacent_text(monkeypatch):
    def unavailable(*args): raise OSError("file deleted")
    monkeypatch.setattr("loom.core.cognition.codex_responses_provider._vision.load_vision_input", unavailable)
    provider = ChatGPTResponsesProvider(auth=auth())
    payload = provider._build_payload([{"role": "user", "content": [
        {"type": "text", "text": "keep me"}, {"type": "image", "source": {"kind": "path", "ref": "missing"}},
    ]}], None, 100)
    assert payload["input"][0]["content"][0]["text"] == "keep me"
    assert "unavailable" in payload["input"][0]["content"][1]["text"].lower()


@pytest.mark.asyncio
async def test_real_vision_entrypoint_file_image_reaches_public_provider(tmp_path):
    from loom.core.cognition import vision
    from loom.core.cognition.chatgpt_capabilities import _test_image
    image, _, _ = _test_image()
    path = tmp_path / "fixture.png"
    path.write_bytes(base64.b64decode(image))
    content = vision.build_user_content("Read this image", [vision.load_vision_input(path)])
    def transport(request):
        body = json.loads(request.content)
        block = body["input"][0]["content"][1]
        assert block["type"] == "input_image"
        assert base64.b64decode(block["image_url"].split(",", 1)[1]) == path.read_bytes()
        return httpx.Response(200, text=event("response.output_text.delta", delta="seen") + event(
            "response.completed", response={"status": "completed"}))
    provider = ChatGPTResponsesProvider(auth=auth(), transport=httpx.MockTransport(transport))
    result = await provider.chat([{"role": "user", "content": content}])
    assert result.text == "seen"


def test_route_capabilities_override_generic_model_audio_metadata():
    description = describe_model({"slug": "model-a", "supported_input_modalities": ["text", "image", "audio"]})
    assert description["image_input"] == "advertised"
    assert description["audio_input"] == "unsupported_by_route"
    assert description["audio_output"] == "unsupported_by_route"
    assert describe_model({"slug": "model-b"})["image_input"] == "unknown"


@pytest.mark.asyncio
async def test_verification_checks_image_and_tool_result_not_just_catalog(monkeypatch):
    monkeypatch.setattr("loom.core.cognition.chatgpt_capabilities._test_image", lambda: ("image-data", "red", "blue"))
    messages_seen = []
    class Provider:
        _api_model = lambda self: "test-model"
        format_tool_result = lambda self, id, content: {"role": "tool", "tool_call_id": id, "content": content}
        async def chat(self, messages, tools=None):
            messages_seen.append(list(messages))
            if len(messages_seen) == 1:
                return LLMResponse(text="loom-text-ok", tool_uses=[], stop_reason="end_turn")
            if len(messages_seen) == 2:
                assert messages[0]["content"][1]["source"]["ref"].endswith("image-data")
                return LLMResponse(text="left=red;right=blue", tool_uses=[], stop_reason="end_turn")
            if len(messages_seen) == 3:
                return LLMResponse(text=None, tool_uses=[ToolUse("id", "loom_probe", {})], stop_reason="tool_use",
                                   raw_message={"role": "assistant", "tool_calls": []})
            return LLMResponse(text=messages[-1]["content"], tool_uses=[], stop_reason="end_turn")
    result = await verify_model(Provider())
    assert result["text"] == result["image_input"] == result["tools"] == "verified"
    assert len(messages_seen) == 4


@pytest.mark.asyncio
async def test_verification_keeps_prior_success_on_later_quota_failure():
    count = 0
    class Provider:
        _api_model = lambda self: "test-model"
        async def chat(self, messages, tools=None):
            nonlocal count
            count += 1
            if count == 1:
                return LLMResponse(text="loom-text-ok", tool_uses=[], stop_reason="end_turn")
            raise ResponsesProviderError(provider="ChatGPT", model="test-model", failure_type="http_error",
                                         phase="request", detail="subscription_sharing_usage_limit_exceeded")
    with pytest.raises(ModelVerificationError) as caught:
        await verify_model(Provider())
    assert caught.value.result["text"] == "verified"
    assert caught.value.result["image_input"] == "not_tested"
    assert count == 2


def test_chatgpt_effort_does_not_silently_downgrade_max_or_ultra():
    for effort in ("max", "ultra"):
        provider = ChatGPTResponsesProvider(auth=auth(), reasoning_effort=effort)
        assert provider._build_payload([], None, 128)["reasoning"]["effort"] == effort


def test_build_router_registers_chatgpt_separately_and_backup_stays_explicit(monkeypatch):
    from loom.core import session
    monkeypatch.setattr(session, "_load_env", lambda: {})
    monkeypatch.setattr(session, "_load_loom_config", lambda: {"providers": {"codex": {"enabled": True}}})
    monkeypatch.setattr("loom.core.cognition.router.get_default_model", lambda: "chatgpt/model-a")
    router = session.build_router()
    assert router.get_provider("chatgpt/model-a").name == "chatgpt"
    assert router.get_provider("codex/model-a").name == "codex"
    router2 = LLMRouter().register(SimpleNamespace(name="codex"))
    with pytest.raises(RuntimeError, match="not registered"):
        router2.get_provider("chatgpt/model-a")


def test_cli_models_reports_route_limits_without_printing_tokens(tmp_path, monkeypatch):
    from loom.platform.cli.main import cli
    monkeypatch.setattr("loom.core.cognition.chatgpt_auth.ChatGPTAuth.catalog", lambda *args, **kwargs: [
        {"slug": "model-a", "visibility": "list"}])
    result = CliRunner().invoke(cli, ["auth", "chatgpt", "models"])
    assert result.exit_code == 0, result.output
    assert "model-a" in result.output and "unsupported_by_route" in result.output


def test_cli_verify_requires_explicit_model_selection(monkeypatch):
    from loom.platform.cli.main import cli
    monkeypatch.setattr("loom.core.cognition.chatgpt_auth.ChatGPTAuth.active_account", lambda self: "a")
    monkeypatch.setattr("loom.core.cognition.chatgpt_auth.ChatGPTAuth.catalog", lambda *args, **kwargs: [
        {"slug": "model-a", "visibility": "list"}])
    result = CliRunner().invoke(cli, ["auth", "chatgpt", "verify"])
    assert result.exit_code != 0
    assert "Choose --model" in result.output


@pytest.mark.parametrize("entry,expected", [
    ({}, "high"),
    ({"default_reasoning_level": "low", "supported_reasoning_levels": [
        {"effort": "low"}, {"effort": "high"}]}, "low"),
    ({"supported_reasoning_levels": ["low", "high"]}, "high"),
    ({"supported_reasoning_levels": ["low", "medium"]}, "low"),
    ({"supported_reasoning_levels": []}, None),
])
def test_effort_uses_selected_models_catalog_with_explicit_fallback(entry, expected):
    fake_auth = auth()
    fake_auth.catalog = lambda account=None: [{"slug": "model-a", **entry}]
    provider = ChatGPTResponsesProvider(model="chatgpt/model-a", auth=fake_auth)
    payload = provider._build_payload([], None, 128)
    if expected is None:
        assert "reasoning" not in payload
    else:
        assert payload["reasoning"] == {"effort": expected}
    assert "max_output_tokens" not in payload
    assert provider.SUPPORTS_MAX_OUTPUT_TOKENS is False


def test_effort_follows_per_call_model_and_rejects_known_unsupported_setting():
    fake_auth = auth()
    fake_auth.catalog = lambda account=None: [
        {"slug": "low-only", "supported_reasoning_levels": [{"effort": "low"}]},
        {"slug": "ultra", "supported_reasoning_levels": [{"effort": "ultra"}]}]
    provider = ChatGPTResponsesProvider(model="chatgpt/low-only", auth=fake_auth)
    assert provider._build_payload([], None, 128)["reasoning"] == {"effort": "low"}
    provider.model = "chatgpt/ultra"
    assert provider._build_payload([], None, 128)["reasoning"] == {"effort": "ultra"}
    provider = ChatGPTResponsesProvider(model="chatgpt/low-only", auth=fake_auth, reasoning_effort="high")
    with pytest.raises(ValueError, match="does not support"):
        provider._build_payload([], None, 128)


@pytest.mark.parametrize("entry", [None, {"context_window": False}, {"context_window": 0}])
def test_missing_context_window_warns_once_and_recovers_after_discovery(caplog, entry):
    fake_auth = auth()
    models = [] if entry is None else [{"slug": "test", **entry}]
    fake_auth.catalog = lambda account=None: models
    provider = ChatGPTResponsesProvider(auth=fake_auth)
    for _ in range(2):
        assert provider.native_context_window("chatgpt/test") == 32768
    assert len(caplog.records) == 1
    assert "loom auth chatgpt models" in caplog.text
    models[:] = [{"slug": "test", "context_window": 500000}]
    assert provider.native_context_window("chatgpt/test") == 500000


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("supported", [[], ["low"], [{"effort": "low"}]])
async def test_tier_model_effort_mismatch_is_structured_and_never_sends_request(streaming, supported):
    fake_auth = auth()
    fake_auth.catalog = lambda account=None: [
        {"slug": "daily", "supported_reasoning_levels": ["medium"]},
        {"slug": "deep", "supported_reasoning_levels": supported}]
    sent = []
    def transport(request):
        sent.append(request)
        return httpx.Response(500)
    provider = ChatGPTResponsesProvider(model="chatgpt/daily", auth=fake_auth, reasoning_effort="medium",
                                       transport=httpx.MockTransport(transport))
    router = LLMRouter().register(provider)
    # Exercise the same for_model binding used when the session changes tiers.
    with pytest.raises(ResponsesProviderError) as caught:
        if streaming:
            async for _ in router.stream_chat("chatgpt/deep", [{"role": "user", "content": "test"}]):
                pass
        else:
            await router.chat("chatgpt/deep", [{"role": "user", "content": "test"}])
    error = caught.value
    assert error.provider == "ChatGPT"
    assert error.model == "deep"
    assert error.failure_type == "configuration_error"
    assert error.phase == "request_built"
    assert "reasoning_effort=medium" in error.detail
    assert "supported levels" in error.detail
    assert ("low" in error.detail) == bool(supported)
    assert "loom-oauth" not in str(error)
    assert provider.model == "chatgpt/daily"
    assert sent == []
