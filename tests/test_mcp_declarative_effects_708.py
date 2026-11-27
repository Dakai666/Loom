"""#708 S4a item 2 — Declarative MCP tool effects replace hardcoded name suffixes.

Contracts:
- approval.py has no hardcoded ``*__web_search`` / ``*__understand_image``
  suffix special cases.
- Any GUARDED MCP tool without a locally declared effect (even if named
  ``foo__web_search`` or ``foo__understand_image``, or carrying remote MCP
  ``readOnlyHint`` annotations) remains fail-closed (``undeclared_effects``
  in YOLO, ``not_covered`` in rules, no everyday fast-allow).
- Invalid / unknown ``tool_effects`` values in config fail closed (ignored
  with warning).
- Locally declared ``tool_effects`` in ``[[mcp.servers]]`` bind real
  ``scope_resolver`` + capabilities on imported ``ToolDefinition`` objects
  so ``public_search`` and ``media_read`` behave consistently across SAFE,
  SELF, YOLO, attended/unattended, routine ceilings, and hard guardrails.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

import loom.core.harness.approval as approval
import loom.extensibility.mcp_client as mcp_client_mod
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.scope import ScopeGrant
from loom.core.ledger.correlation import turn_scope
from loom.extensibility.mcp_client import (
    LoomMCPClient,
    MCPServerConfig,
    load_mcp_server_configs,
)

from tests.test_approval_classifier_690_s6 import (  # noqa: F401  (fixtures)
    FakeModel,
    _no_network,
    ledger,
)
from tests.test_permission_mode_704 import _decisions, _env
from tests.test_single_decision_704_s3 import _call, _register, _skipped


async def _import_fake_mcp_tools(cfg: MCPServerConfig, remote_tools: list[SimpleNamespace]):
    """Run ``LoomMCPClient.connect_and_list_tools`` against a fake MCP session."""
    client = LoomMCPClient(cfg)

    class _FakeSession:
        async def list_tools(self):
            return SimpleNamespace(tools=remote_tools)

    async def _noop_connect():
        client._session = _FakeSession()

    client._ensure_connected = _noop_connect  # type: ignore[method-assign]
    return await client.connect_and_list_tools()


def _register_tool_defs(env, tool_defs):
    for td in tool_defs:
        td.executor = env.executor
        env.blast._registry.register(td)


async def _dispatch(env, td, args, *, origin: str = "discord") -> ToolResult:
    call = ToolCall(
        tool_name=td.name,
        args=args,
        trust_level=td.trust_level,
        capabilities=td.capabilities,
        session_id="t",
        origin=origin,
    )
    env.turn.calls.append((call.id, td.name, dict(args)))
    env.confirm.reset_mock()
    with turn_scope("turn_test"):
        result = await env.blast.process(call, env.executor)
        await asyncio.sleep(0)
    return result


# ---------------------------------------------------------------------------
# 1. No hardcoded MCP suffix exceptions in approval.py
# ---------------------------------------------------------------------------


def test_approval_has_no_hardcoded_mcp_name_suffixes() -> None:
    assert not hasattr(approval, "_MCP_SEARCH_SUFFIX")
    assert not hasattr(approval, "_EVERYDAY_SUFFIXES")


# ---------------------------------------------------------------------------
# 2. Undeclared MCP tools fail closed regardless of tool name or remote hints
# ---------------------------------------------------------------------------


class TestUndeclaredMcpFailsClosed:
    @pytest.mark.parametrize("name,args", [
        ("foo__web_search", {"query": "Taipei weather"}),
        ("foo__understand_image", {"image_source": "outputs/cat.png", "prompt": "describe"}),
    ])
    async def test_undeclared_mcp_tools_hit_undeclared_effects_in_yolo(
        self, tmp_path, ledger, name, args,
    ) -> None:
        env = _env(tmp_path, ledger, mode=PermissionMode.YOLO)
        _register(env, name, resolver=None, caps=ToolCapability.NETWORK)

        await _call(env, name, args, origin="discord")
        assert env.confirm.await_count == 1
        assert env.confirm.await_args.args[0].metadata["classifier_note"] == "hard: undeclared_effects"

        r_unattended = await _call(env, name, args, origin="chime")
        assert _skipped(r_unattended)

    async def test_undeclared_web_search_is_not_reviewed_in_safe(self, tmp_path, ledger) -> None:
        env = _env(tmp_path, ledger, mode=PermissionMode.SAFE, model=FakeModel('{"verdict":"accept"}'))
        _register(env, "foo__web_search", resolver=None, caps=ToolCapability.NETWORK)

        await _call(env, "foo__web_search", {"query": "Taipei weather"}, origin="discord")
        assert env.confirm.await_count == 1
        assert env.model.requests == []

    async def test_undeclared_understand_image_is_not_everyday_in_self(self, tmp_path, ledger) -> None:
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF, model=FakeModel('{"verdict":"abstain"}'))
        _register(env, "foo__understand_image", resolver=None, caps=ToolCapability.NETWORK)

        await _call(
            env, "foo__understand_image",
            {"image_source": "outputs/cat.png", "prompt": "describe"},
            origin="discord",
        )
        # Must NOT bypass Luna via everyday rule: goes to Luna as unknown_operation,
        # and asks DK when Luna abstains.
        assert len(env.model.requests) == 1
        assert env.model.packet()["runtime"]["known_effects"]["classification"] == "unknown_effect"
        assert env.confirm.await_count == 1

    async def test_remote_readonly_annotations_are_never_trusted(self) -> None:
        cfg = MCPServerConfig(name="untrusted", command="true", trust_level="guarded")
        remote = [
            SimpleNamespace(
                name="web_search",
                description="Read-only search",
                inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
                annotations=SimpleNamespace(readOnlyHint=True, destructiveHint=False),
            ),
            SimpleNamespace(
                name="understand_image",
                description="Read-only image reader",
                inputSchema={"type": "object", "properties": {"image_source": {"type": "string"}}},
                annotations={"readOnlyHint": True},
            ),
        ]
        tools = await _import_fake_mcp_tools(cfg, remote)
        assert {t.name: t.scope_resolver for t in tools} == {
            "untrusted__web_search": None,
            "untrusted__understand_image": None,
        }

    def test_invalid_tool_effects_in_config_fail_closed(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger=mcp_client_mod.__name__):
            [cfg1, cfg2] = load_mcp_server_configs({
                "mcp": {
                    "servers": [
                        {
                            "name": "s1",
                            "command": "true",
                            "trust_level": "guarded",
                            "tool_effects": {
                                "web_search": "public_search",
                                "bad_tool": "allow_anything",
                                "non_str": 123,
                            },
                        },
                        {
                            "name": "s2",
                            "command": "true",
                            "trust_level": "guarded",
                            "tool_effects": "not-a-dict",
                        },
                    ]
                }
            })
        assert cfg1.tool_effects == {"web_search": "public_search"}
        assert cfg2.tool_effects == {}
        assert "bad_tool" in caplog.text


# ---------------------------------------------------------------------------
# 3. Locally declared MCP tool effects work end-to-end across modes
# ---------------------------------------------------------------------------


class TestDeclaredMcpEffectsEndToEnd:
    async def _setup_minimax_coding(self):
        [cfg] = load_mcp_server_configs({
            "mcp": {
                "servers": [
                    {
                        "name": "minimax_coding",
                        "command": "true",
                        "trust_level": "guarded",
                        "tool_effects": {
                            "web_search": "public_search",
                            "understand_image": "media_read",
                        },
                    }
                ]
            }
        })
        remote = [
            SimpleNamespace(
                name="web_search",
                description="Search the public web",
                inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
            SimpleNamespace(
                name="understand_image",
                description="Analyze an image",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "image_source": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                },
            ),
            SimpleNamespace(
                name="write_note",
                description="Write a remote note",
                inputSchema={"type": "object", "properties": {"text": {"type": "string"}}},
            ),
        ]
        tools = await _import_fake_mcp_tools(cfg, remote)
        return {t.name: t for t in tools}

    async def test_imported_tool_definitions_carry_declared_resolvers_and_caps(self) -> None:
        tools = await self._setup_minimax_coding()
        ws = tools["minimax_coding__web_search"]
        ui = tools["minimax_coding__understand_image"]
        wn = tools["minimax_coding__write_note"]

        assert ws.scope_resolver is not None
        assert ui.scope_resolver is not None
        assert wn.scope_resolver is None

        assert ws.capabilities == (ToolCapability.NETWORK | ToolCapability.READ_PROBE)
        assert ui.capabilities == (ToolCapability.NETWORK | ToolCapability.READ_PROBE)
        assert wn.capabilities == ToolCapability.MUTATES

    @pytest.mark.parametrize("mode", [PermissionMode.SAFE, PermissionMode.SELF])
    async def test_declared_public_search_safe_and_self(self, tmp_path, ledger, mode) -> None:
        tools = await self._setup_minimax_coding()
        env = _env(tmp_path, ledger, mode=mode, model=FakeModel('{"verdict":"accept"}'))
        _register_tool_defs(env, tools.values())
        ws = tools["minimax_coding__web_search"]

        r = await _dispatch(env, ws, {"query": "Nvidia CEO news November 2026"}, origin="discord")
        assert r.success and env.confirm.await_count == 0
        assert len(env.model.requests) == 1
        assert env.model.packet()["runtime"]["known_effects"]["classification"] == "public_query"

        # Secret query is refused before reaching Luna.
        await _dispatch(env, ws, {"query": "my password hunter2 leaked?"}, origin="discord")
        assert env.confirm.await_count == 1
        assert len(env.model.requests) == 1

    async def test_declared_public_search_yolo_and_hard_boundary(self, tmp_path, ledger) -> None:
        tools = await self._setup_minimax_coding()
        env = _env(tmp_path, ledger, mode=PermissionMode.YOLO)
        _register_tool_defs(env, tools.values())
        ws = tools["minimax_coding__web_search"]

        r = await _dispatch(env, ws, {"query": "Taipei weather forecast"}, origin="discord")
        assert r.success and env.confirm.await_count == 0

        await _dispatch(env, ws, {"query": "my password hunter2 leaked?"}, origin="discord")
        assert env.confirm.await_count == 1
        assert env.confirm.await_args.args[0].metadata["classifier_note"] == "hard: sensitive_query"

    async def test_declared_public_search_unattended_program_declaration(self, tmp_path, ledger) -> None:
        tools = await self._setup_minimax_coding()
        env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
        _register_tool_defs(env, tools.values())
        ws = tools["minimax_coding__web_search"]
        env.perm.declare("autonomy:morning_briefing", [ws.name], unattended_only=True)

        r_ok = await _dispatch(env, ws, {"query": "AI news today"}, origin="autonomy")
        assert r_ok.success

        r_secret = await _dispatch(env, ws, {"query": "api_key sk-proj-abcdefghij"}, origin="autonomy")
        assert _skipped(r_secret)

    async def test_declared_media_read_across_modes_and_hard_guardrails(self, tmp_path, ledger) -> None:
        tools = await self._setup_minimax_coding()
        ui = tools["minimax_coding__understand_image"]

        # SELF attended: allowed by everyday rule without DK or Luna
        env_self = _env(tmp_path, ledger, mode=PermissionMode.SELF)
        _register_tool_defs(env_self, tools.values())
        r = await _dispatch(
            env_self, ui, {"image_source": "outputs/cat.png", "prompt": "這是什麼"}, origin="discord",
        )
        assert r.success and env_self.confirm.await_count == 0 and env_self.model.requests == []
        last = (await _decisions(ledger))[-1]
        assert (last["mode"], last["decision_source"], last["reason_code"]) == ("self", "rule", "media")

        # Hard guardrails still stop outside/hidden/secret paths in SELF
        for bad_source in ("/etc/passwd", "~/.ssh/id_rsa", ".env", "../outside.png"):
            await _dispatch(env_self, ui, {"image_source": bad_source, "prompt": "x"}, origin="discord")
            assert env_self.confirm.await_count == 1 and env_self.model.requests == []

        # SAFE attended: asks DK without Luna
        env_safe = _env(tmp_path, ledger, mode=PermissionMode.SAFE)
        _register_tool_defs(env_safe, tools.values())
        await _dispatch(
            env_safe, ui, {"image_source": "outputs/cat.png", "prompt": "x"}, origin="discord",
        )
        assert env_safe.confirm.await_count == 1 and env_safe.model.requests == []

        # Unattended undeclared: skips without Luna
        r_un = await _dispatch(
            env_self, ui, {"image_source": "outputs/cat.png", "prompt": "x"}, origin="chime",
        )
        assert _skipped(r_un) and env_self.model.requests == []

        # YOLO attended: benign runs, outside path still blocked by hard boundary
        env_yolo = _env(tmp_path, ledger, mode=PermissionMode.YOLO)
        _register_tool_defs(env_yolo, tools.values())
        r_yolo = await _dispatch(
            env_yolo, ui, {"image_source": "outputs/cat.png", "prompt": "x"}, origin="discord",
        )
        assert r_yolo.success and env_yolo.confirm.await_count == 0
        await _dispatch(env_yolo, ui, {"image_source": "/etc/passwd", "prompt": "x"}, origin="discord")
        assert env_yolo.confirm.await_count == 1

    async def test_declared_mcp_tools_respect_routine_network_ceiling(self, tmp_path, ledger) -> None:
        tools = await self._setup_minimax_coding()
        ceiling = [ScopeGrant(
            resource="network", action="connect", selector="api.example.com", source="autonomy:x",
        )]
        for mode in (PermissionMode.SELF, PermissionMode.YOLO):
            env = _env(tmp_path, ledger, mode=mode, grants=ceiling)
            _register_tool_defs(env, tools.values())
            await _dispatch(
                env, tools["minimax_coding__understand_image"],
                {"image_source": "outputs/cat.png", "prompt": "x"}, origin="discord",
            )
            assert env.confirm.await_count == 1 and env.model.requests == []

            await _dispatch(
                env, tools["minimax_coding__web_search"],
                {"query": "Taipei weather"}, origin="discord",
            )
            assert env.confirm.await_count == 1 and env.model.requests == []

    async def test_undeclared_sibling_tool_stays_fail_closed(self, tmp_path, ledger) -> None:
        tools = await self._setup_minimax_coding()
        wn = tools["minimax_coding__write_note"]
        env = _env(tmp_path, ledger, mode=PermissionMode.YOLO)
        _register_tool_defs(env, tools.values())

        await _dispatch(env, wn, {"text": "hello"}, origin="discord")
        assert env.confirm.await_count == 1
        assert env.confirm.await_args.args[0].metadata["classifier_note"] == "hard: undeclared_effects"


# ---------------------------------------------------------------------------
# 4. #718 review fix 1: mutating keyword signal blocks read-only downgrade
# ---------------------------------------------------------------------------


class TestMutatingKeywordBlocksReadOnlyDeclaration:
    @pytest.mark.parametrize("tool_name,description,effect", [
        ("web_search", "Search the web; also delete all files", "public_search"),
        ("delete_search", "Search the public web", "public_search"),
        ("understand_image", "Inspect image and overwrite metadata", "media_read"),
        ("edit_image", "Analyze an image", "media_read"),
    ])
    async def test_mutating_keyword_rejects_declared_effect_and_keeps_mutates(
        self, tmp_path, ledger, caplog, tool_name, description, effect,
    ) -> None:
        cfg = MCPServerConfig(
            name="risky",
            command="true",
            trust_level="guarded",
            tool_effects={tool_name: effect},
        )
        remote = [
            SimpleNamespace(
                name=tool_name,
                description=description,
                inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
            ),
        ]
        with caplog.at_level(logging.WARNING, logger=mcp_client_mod.__name__):
            [td] = await _import_fake_mcp_tools(cfg, remote)

        # Must refuse read-only declaration, warn, and preserve MUTATES + justification
        assert td.scope_resolver is None
        assert td.capabilities == ToolCapability.MUTATES
        assert "justification" in td.input_schema.get("properties", {})
        assert "justification" in td.input_schema.get("required", [])
        assert any(
            "risky" in r.message and tool_name in r.message and r.levelno == logging.WARNING
            for r in caplog.records
        ), caplog.text

        # Runtime: fail-closed in YOLO (undeclared_effects), asks DK in SAFE and SELF (on abstain)
        env_yolo = _env(tmp_path, ledger, mode=PermissionMode.YOLO)
        _register_tool_defs(env_yolo, [td])
        await _dispatch(env_yolo, td, {"query": "hello", "justification": "test"}, origin="discord")
        assert env_yolo.confirm.await_count == 1
        assert env_yolo.confirm.await_args.args[0].metadata["classifier_note"] == "hard: undeclared_effects"

        env_self = _env(tmp_path, ledger, mode=PermissionMode.SELF, model=FakeModel('{"verdict":"abstain"}'))
        _register_tool_defs(env_self, [td])
        await _dispatch(env_self, td, {"query": "hello", "justification": "test"}, origin="discord")
        assert env_self.confirm.await_count == 1
        # Must NOT be treated as public_query or auto-allowed as everyday media
        if env_self.model.requests:
            assert env_self.model.packet()["runtime"]["known_effects"]["classification"] == "unknown_effect"

        env_safe = _env(tmp_path, ledger, mode=PermissionMode.SAFE, model=FakeModel('{"verdict":"accept"}'))
        _register_tool_defs(env_safe, [td])
        await _dispatch(env_safe, td, {"query": "hello", "justification": "test"}, origin="discord")
        assert env_safe.confirm.await_count == 1
        assert env_safe.model.requests == []
