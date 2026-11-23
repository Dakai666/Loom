"""Permission names vs the live tool registry (#690 S4).

``allowed_tools`` in schedules.toml / rhythm.toml / room.toml were never
compared to what is actually registered: ``async_jobs`` is a skill name,
``openai__text_to_image`` belongs to a server that no longer exists, and bare
``web_search`` only exists when a Brave key is set. These tests pin the
contract of the check:

- every name is classified registered / conditionally unregistered / pending /
  unknown, with the places that grant it;
- MCP names are *pending* until the session has finished loading MCP, never
  reported unknown early;
- the check only reports — the granted ``allowed_tools`` stay exactly as
  written;
- the result reaches Loom Agent as one layer of the dawn chime, and is
  re-checked when the dawn reload re-reads the rhythm table.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.circadian import state as st
from loom.autonomy.circadian.lifecycle import (
    CircadianConfig,
    register_rhythm_anchors,
    register_triggers,
)
from loom.autonomy.circadian.rhythm import Anchor
from loom.autonomy.permission_names import (
    CONDITIONAL_TOOLS,
    NameStatus,
    classify_permission_names,
)
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolDefinition, ToolRegistry

TZ = "Asia/Taipei"
CFG = CircadianConfig(enabled=True, timezone=TZ, start="08:00", sleep="00:00")


# ---------------------------------------------------------------------------
# classify_permission_names — the pure classification
# ---------------------------------------------------------------------------


def _by_name(checks):
    return {c.name: c for c in checks}


class TestClassify:
    def test_each_status(self):
        uses = {
            "run_bash": ("rhythm dawn",),
            "async_jobs": ("schedule morning_briefing",),
            "web_search": ("rhythm curiosity", "schedule morning_briefing"),
            "minimax_coding__web_search": ("rhythm curiosity",),
            "minimax__generate_video": ("schedule morning_briefing",),
            "openai__text_to_image": ("schedule morning_briefing",),
            "substrate__recall": ("room cat",),
        }
        checks = _by_name(classify_permission_names(
            uses,
            registered={"run_bash", "minimax_coding__web_search"},
            mcp_configured={"minimax", "minimax_coding", "substrate"},
            mcp_connected={"minimax", "minimax_coding"},
        ))
        assert checks["run_bash"].status is NameStatus.REGISTERED
        # A skill name is not a tool name.
        assert checks["async_jobs"].status is NameStatus.UNKNOWN
        # Known native tool, off in this deployment (no Brave key).
        assert checks["web_search"].status is NameStatus.CONDITIONAL
        assert checks["web_search"].reason
        assert checks["web_search"].sources == (
            "rhythm curiosity", "schedule morning_briefing",
        )
        assert checks["minimax_coding__web_search"].status is NameStatus.REGISTERED
        # Server connected, but it exposes no such tool.
        assert checks["minimax__generate_video"].status is NameStatus.UNKNOWN
        # No server named "openai" is configured at all.
        assert checks["openai__text_to_image"].status is NameStatus.UNKNOWN
        # Configured server that did not connect: known, currently off.
        assert checks["substrate__recall"].status is NameStatus.CONDITIONAL
        assert "substrate" in checks["substrate__recall"].reason

    def test_mcp_names_are_pending_until_mcp_has_loaded(self):
        uses = {
            "minimax_coding__web_search": ("rhythm curiosity",),
            "openai__text_to_image": ("schedule morning_briefing",),
            "async_jobs": ("schedule morning_briefing",),
        }
        checks = _by_name(classify_permission_names(
            uses,
            registered={"run_bash"},
            mcp_configured={"minimax_coding"},
            mcp_connected=None,  # MCP load has not finished yet
        ))
        assert checks["minimax_coding__web_search"].status is NameStatus.PENDING
        # Not a configured server: no amount of waiting registers it.
        assert checks["openai__text_to_image"].status is NameStatus.UNKNOWN
        assert checks["async_jobs"].status is NameStatus.UNKNOWN


def _registered_name(factory, *args, **kwargs) -> str:
    return factory(*args, **kwargs).name


class TestConditionalCatalogue:
    """The catalogue names must be names the code really registers — a stale
    entry would hide a real typo as "conditionally unregistered"."""

    def test_every_entry_is_a_real_tool_name(self):
        from loom.platform.cli import tools as cli_tools
        from loom.platform.discord import tools as dc_tools

        built = {
            _registered_name(cli_tools.make_web_search_tool, "k"),
            _registered_name(cli_tools.make_memory_attend_tool, MagicMock()),
            _registered_name(cli_tools.make_agent_health_tool, MagicMock()),
            _registered_name(cli_tools.make_request_model_tier_tool, MagicMock()),
            _registered_name(cli_tools.make_clear_model_tier_tool, MagicMock()),
            _registered_name(cli_tools.make_set_reasoning_effort_tool, MagicMock()),
            inspect.signature(cli_tools.make_openai_image_generation_tool)
            .parameters["tool_name"].default,
            inspect.signature(cli_tools.make_xai_tts_tool)
            .parameters["tool_name"].default,
            _registered_name(dc_tools.make_send_discord_file_tool, MagicMock(), 1, Path(".")),
            _registered_name(dc_tools.make_send_discord_embed_tool, MagicMock(), 1),
            _registered_name(dc_tools.make_send_discord_select_tool, MagicMock(), 1),
            _registered_name(
                dc_tools.make_add_discord_reaction_tool, MagicMock(), 1,
                last_user_message_lookup=MagicMock(),
            ),
            _registered_name(
                dc_tools.make_create_discord_forum_post_tool, MagicMock(), Path("."),
                default_forum_id=1,
            ),
            _registered_name(
                dc_tools.make_read_discord_forum_tool, MagicMock(), default_forum_id=1,
            ),
        }
        assert set(CONDITIONAL_TOOLS) == built


# ---------------------------------------------------------------------------
# AutonomyDaemon.check_permission_names — reading the real config files
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    st.set_dir_for_test(tmp_path / "circadian")
    yield
    st.set_dir_for_test(None)


def _write(rel: str, text: str) -> Path:
    p = Path(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


LOOM_TOML = """
[autonomy]
enabled = true

[[mcp.servers]]
name = "minimax_coding"
command = "uvx"
args = ["minimax-coding-plan-mcp"]
"""

SCHEDULES = """
[[schedules]]
name = "morning_briefing"
cron = "0 0 * * *"
intent = "news"
allowed_tools = ["run_bash", "async_jobs", "minimax_coding__web_search"]
"""

RHYTHM = """
[[anchors]]
time = "08:00"
name = "dawn"
meaning = "wake"
allowed_tools = ["run_bash", "web_search"]

[[anchors]]
time = "11:00"
name = "curiosity"
meaning = "look around"
allowed_tools = ["fetch_url"]
"""

ROOM = """
enabled = true

[[furniture]]
name = "cat"
command = ["python", "pet.py"]
allowed_tools = ["memorise"]
"""


def _tool(name: str) -> ToolDefinition:
    async def _run(call):  # pragma: no cover — never invoked
        raise AssertionError

    return ToolDefinition(
        name=name, description=name, trust_level=TrustLevel.SAFE,
        input_schema={}, executor=_run,
    )


def _session(*names: str, mcp_loaded: bool = True, connected: tuple[str, ...] = ()):
    registry = ToolRegistry()
    for n in names:
        registry.register(_tool(n))
    return SimpleNamespace(
        registry=registry,
        _mcp_loaded=mcp_loaded,
        _mcp_clients=[SimpleNamespace(_cfg=SimpleNamespace(name=s)) for s in connected],
    )


def _daemon(session=None, deliveries=None):
    from loom.autonomy.daemon import AutonomyDaemon
    from loom.notify.confirm import ConfirmFlow
    from loom.notify.router import NotificationRouter

    async def _delivery(req: ChimeRequest) -> bool:
        if deliveries is not None:
            deliveries.append(req)
        return True

    return AutonomyDaemon(
        notify_router=NotificationRouter(),
        confirm_flow=ConfirmFlow(send_fn=lambda n: None),
        loom_session=session,
        chime_delivery=_delivery,
    )


def _write_all():
    _write("loom.toml", LOOM_TOML)
    _write("autonomy/schedules.toml", SCHEDULES)
    _write("autonomy/circadian/rhythm.toml", RHYTHM)
    _write("autonomy/circadian/room.toml", ROOM)


class TestDaemonCheck:
    def test_reads_every_config_and_classifies(self, caplog):
        _write_all()
        daemon = _daemon(_session(
            "run_bash", "fetch_url", "minimax_coding__web_search",
            connected=("minimax_coding",),
        ))
        daemon.load_config("loom.toml")
        with caplog.at_level(logging.INFO, logger="loom.autonomy"):
            checks = _by_name(daemon.check_permission_names())

        assert checks["run_bash"].status is NameStatus.REGISTERED
        assert set(checks["run_bash"].sources) == {
            "schedule morning_briefing", "rhythm dawn",
        }
        assert checks["fetch_url"].status is NameStatus.REGISTERED
        assert checks["minimax_coding__web_search"].status is NameStatus.REGISTERED
        assert checks["async_jobs"].status is NameStatus.UNKNOWN
        assert checks["web_search"].status is NameStatus.CONDITIONAL
        assert checks["memorise"].status is NameStatus.UNKNOWN
        assert checks["memorise"].sources == ("room cat",)

        line = daemon.permission_names_line
        assert "async_jobs" in line and "memorise" in line and "web_search" in line
        assert "run_bash" not in line
        assert any(
            "async_jobs" in r.getMessage() and r.levelno == logging.WARNING
            for r in caplog.records
        )

    def test_check_never_changes_what_is_granted(self):
        _write_all()
        daemon = _daemon(_session("run_bash", connected=("minimax_coding",)))
        daemon.load_config("loom.toml")
        before = {t.name: list(t.allowed_tools) for t in daemon.evaluator.list()}
        daemon.check_permission_names()
        after = {t.name: list(t.allowed_tools) for t in daemon.evaluator.list()}
        assert after == before
        assert before["morning_briefing"] == [
            "run_bash", "async_jobs", "minimax_coding__web_search",
        ]

    def test_mcp_names_pending_before_mcp_loaded_then_resolved(self):
        _write_all()
        session = _session("run_bash", mcp_loaded=False)
        daemon = _daemon(session)
        daemon.load_config("loom.toml")

        early = _by_name(daemon.check_permission_names())
        assert early["minimax_coding__web_search"].status is NameStatus.PENDING
        assert "minimax_coding__web_search" in daemon.permission_names_line

        # MCP finishes connecting and registers its tools; the next check
        # (startup after setup, or the dawn reload) sees them.
        session.registry.register(_tool("minimax_coding__web_search"))
        session._mcp_loaded = True
        session._mcp_clients = [SimpleNamespace(_cfg=SimpleNamespace(name="minimax_coding"))]
        later = _by_name(daemon.check_permission_names())
        assert later["minimax_coding__web_search"].status is NameStatus.REGISTERED
        assert "minimax_coding__web_search" not in daemon.permission_names_line

    def test_all_registered_leaves_no_line(self):
        _write("loom.toml", LOOM_TOML)
        _write("autonomy/schedules.toml", """
[[schedules]]
name = "s"
cron = "0 0 * * *"
intent = "x"
allowed_tools = ["run_bash"]
""")
        daemon = _daemon(_session("run_bash"))
        daemon.load_config("loom.toml")
        daemon.check_permission_names()
        assert daemon.permission_names_line == ""

    def test_no_session_means_no_check(self):
        _write_all()
        daemon = _daemon(None)
        daemon.load_config("loom.toml")
        assert daemon.check_permission_names() == []
        assert daemon.permission_names_line == ""


# ---------------------------------------------------------------------------
# Where Loom Agent reads it: the dawn chime, refreshed by the dawn reload
# ---------------------------------------------------------------------------


async def _fire(daemon, trigger_name: str) -> None:
    trigger = next(t for t in daemon.evaluator.list() if t.name == trigger_name)
    await daemon._direct_handlers[trigger_name](trigger, {})


class TestDawnChime:
    async def test_dawn_chime_carries_the_mismatch_layer(self):
        _write_all()
        deliveries: list[ChimeRequest] = []
        daemon = _daemon(_session("run_bash", "fetch_url"), deliveries)
        daemon.load_config("loom.toml")
        daemon.check_permission_names()
        anchors = [
            Anchor(time="08:00", name="dawn", meaning="wake"),
            Anchor(time="11:00", name="curiosity", meaning="look around"),
        ]
        register_rhythm_anchors(daemon, CFG, anchors)

        await _fire(daemon, "circadian:phase_dawn")
        await _fire(daemon, "circadian:phase_curiosity")
        dawn, curiosity = deliveries
        assert "async_jobs" in dawn.intent
        assert daemon.permission_names_line in dawn.intent
        # Dawn only: other phases are not re-told the same thing.
        assert "async_jobs" not in curiosity.intent

    async def test_dawn_reload_rechecks_the_edited_table(self):
        from tests.test_circadian_phase_chime import FakePlatform

        _write("loom.toml", LOOM_TOML)
        _write("autonomy/schedules.toml", "")
        _write("autonomy/circadian/rhythm.toml", """
[[anchors]]
time = "08:00"
name = "dawn"
meaning = "wake"
allowed_tools = ["run_bash"]
""")
        daemon = _daemon(_session("run_bash"))
        daemon.load_config("loom.toml")
        daemon.check_permission_names()
        assert daemon.permission_names_line == ""

        register_triggers(daemon, FakePlatform(), CFG)
        _write("autonomy/circadian/rhythm.toml", """
[[anchors]]
time = "08:00"
name = "dawn"
meaning = "wake"
allowed_tools = ["run_bash", "memorise"]
""")
        await _fire(daemon, "circadian:dawn_spawn")
        assert "memorise" in daemon.permission_names_line
