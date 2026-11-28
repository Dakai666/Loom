"""Open anchors are ordinary session turns; loaded local skills supply allows."""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loom.autonomy.chime import ChimeRequest
from loom.autonomy.circadian.rhythm import load_rhythm
from loom.autonomy.skill_allows import activity_permission_fields
from loom.core.config import _warn_retired_config_keys
from loom.core.harness.permissions import PermissionContext, TrustLevel
from loom.core.harness.middleware import ToolCall
from loom.core.memory.procedural import SkillGenome
from loom.core.session import LoomSession
from loom.platform.cli.skill_tools import make_load_skill_tool
from loom.platform.discord.bot import LoomDiscordBot


def _rhythm(tmp_path, extra=""):
    p = tmp_path / "rhythm.toml"
    p.write_text('[[anchors]]\nname="open-test"\ntime="10:00"\n' + extra)
    return p


@pytest.mark.parametrize("reason", ["", 'legacy_reason="old explanation"\n',
                                    'legacy_reason=42\n', 'legacy_reason=""\n'])
def test_open_anchor_ignores_old_permissions(tmp_path, caplog, reason):
    p = _rhythm(tmp_path, reason + 'allowed_tools=["memorize"]\n'
                'scope_grants=[{resource="path",action="write",selector="notes"}]\n')
    with caplog.at_level(logging.INFO):
        anchors = load_rhythm(p, workspace=tmp_path)
    assert len(anchors) == 1
    a = anchors[0]
    assert a.allowed_tools == ("memorize",)
    assert a.scope_grants == ()
    assert a.open_slot is True
    assert "legacy scope_grants" not in caplog.text


def test_open_reason_deprecation_once_per_anchor(tmp_path, caplog):
    with caplog.at_level(logging.INFO):
        for name in ("once-a", "once-a", "once-b"):
            activity_permission_fields({"legacy_reason": "old"}, name, tmp_path,
                                       open_slot=True)
    assert caplog.text.count("legacy_reason is ignored") == 2


@pytest.mark.parametrize("reason", [None, "intentional"])
def test_schedule_preserves_fields_and_logs_once(tmp_path, caplog, reason):
    entry = {"allowed_tools": ["memorize"], "scope_grants": [
        {"resource": "path", "action": "write", "selector": "notes"}]}
    if reason:
        entry["legacy_reason"] = reason
    with caplog.at_level(logging.INFO):
        for source in (f"schedule-a-{reason}", f"schedule-a-{reason}", f"schedule-b-{reason}"):
            fields = activity_permission_fields(entry, source, tmp_path)
            assert fields["allowed_tools"] == ["memorize"]
            assert fields["scope_grants"] == entry["scope_grants"]
    assert caplog.text.count("scope_grants without skills") == 2


def test_retired_config_warning_once_per_key_list(caplog):
    with caplog.at_level(logging.WARNING):
        for cfg in ({"plugins": {}}, {"plugins": {}}, {"adapters": {}}):
            _warn_retired_config_keys(cfg)
    assert caplog.text.count("contains retired") == 2


def _session(tmp_path):
    from loom.core.harness.registry import ToolRegistry
    session = LoomSession.__new__(LoomSession)
    session.workspace = tmp_path
    session.perm = PermissionContext("open-slot-test")
    session._telemetry = None
    session.registry = ToolRegistry()
    return session


def _apply(session, *, open_slot=True, input_trust="local", tools=(), grants=()):
    req = ChimeRequest("circadian:phase_test", "free time", datetime.now(timezone.utc),
                       open_slot=open_slot, input_trust=input_trust,
                       allowed_tools=tools, scope_grants=grants)
    return LoomDiscordBot._apply_chime_permissions(None, req, session)


def _skill(root, name="local-skill"):
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "helper.py").write_text("print('synthetic')\n")
    text = ('---\nname: ' + name + '\nallows:\n'
            f'  exec: [python3 skills/{name}/helper.py run]\n'
            '  write: [notes]\n  tools: [memorize]\n---\nSynthetic body.\n')
    (d / "SKILL.md").write_text(text)
    return SkillGenome(name=name, body=text)


def test_open_turn_has_no_declaration_or_ceiling_and_revoke_clears_flag(tmp_path):
    from loom.core.harness.approval import _routine_ceiling
    session = _session(tmp_path)
    added, source = _apply(session, tools=("memorize",), grants=(
        {"resource": "path", "action": "write", "selector": "notes"},))
    assert added == ["memorize"]
    assert session.perm.open_slot_source == source
    assert session.perm.declared_by("memorize", attended=False) == source
    assert session.perm.declared_by("memorize", attended=True) is None
    assert session.perm.grants == []
    assert not _routine_ceiling(session.perm, "path", "write")
    LoomDiscordBot._revoke_chime_permissions(None, session, added, source)
    assert session.perm.open_slot_source is None


@pytest.mark.parametrize("kind", ["open", "external-skill", "bound", "external-chime", "symlink"])
async def test_load_skill_grants_only_local_open_turn(tmp_path, kind):
    session = _session(tmp_path)
    root = tmp_path / "foreign" if kind in ("external-skill", "symlink") else tmp_path
    genome = _skill(root)
    if kind == "symlink":
        (tmp_path / "skills").mkdir()
        (tmp_path / "skills" / genome.name).symlink_to(root / "skills" / genome.name)
    procedural = SimpleNamespace(get=AsyncMock(return_value=genome))
    tool = make_load_skill_tool(procedural, [tmp_path / "skills", root / "skills"],
                                on_loaded=session._on_skill_loaded)
    added, source = _apply(session, open_slot=kind != "bound",
                           input_trust="external" if kind == "external-chime" else "local")
    call = ToolCall(id="load", tool_name="load_skill", args={"name": genome.name},
                    session_id="open-slot-test", origin="chime", trust_level=TrustLevel.SAFE)
    result = await tool.executor(call)
    assert result.success
    if kind == "open":
        assert session.perm.declared_by("memorize", attended=False)
        assert session.perm.declared_by("memorize", attended=True) is None
        assert {(g.resource, g.action, g.selector) for g in session.perm.grants} == {
            ("exec", "execute", "python3 skills/local-skill/helper.py run"),
            ("path", "write", "notes")}
        assert session.perm.grants[0].constraints["absolute_paths"] == "deny"
        # Dedup within the turn must not duplicate grants.
        await tool.executor(call)
        assert len(session.perm.grants) == 2
    else:
        assert session.perm.grants == []
        assert session.perm.declared_tools() == frozenset()
    LoomDiscordBot._revoke_chime_permissions(None, session, added, source)
    assert session.perm.grants == []
    assert session.perm.boundaries == {}
    assert session.perm.open_slot_source is None
    # A session-deduplicated skill still supplies allows in a later open turn.
    if kind == "open":
        added, source = _apply(session)
        await tool.executor(call)
        assert len(session.perm.grants) == 2
        LoomDiscordBot._revoke_chime_permissions(None, session, added, source)


async def test_bound_skill_load_keeps_explicit_narrowing(tmp_path):
    genome = _skill(tmp_path)
    session = _session(tmp_path)
    anchor = load_rhythm(_rhythm(tmp_path,
        'skills=["local-skill"]\nallowed_tools=[]\n'
        'scope_grants=[{resource="exec", action="execute", '
        'selector="python3 skills/local-skill/helper.py run", constraints={max_calls=1}}]\n'),
        workspace=tmp_path)[0]
    added, source = _apply(session, open_slot=anchor.open_slot,
                           tools=anchor.allowed_tools, grants=anchor.scope_grants)
    before = [(g.resource, g.selector, dict(g.constraints)) for g in session.perm.grants]
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(return_value=genome)),
                               [tmp_path / "skills"], on_loaded=session._on_skill_loaded)
    result = await tool.executor(ToolCall(tool_name="load_skill", args={"name": genome.name},
        session_id="test", origin="chime", trust_level=TrustLevel.SAFE))
    assert result.success
    assert [(g.resource, g.selector, dict(g.constraints)) for g in session.perm.grants] == before
    assert session.perm.grants[0].constraints["max_calls"] == 1
    assert session.perm.declared_tools() == frozenset()
    LoomDiscordBot._revoke_chime_permissions(None, session, added, source)


# Real scope resolvers/middleware with stub executors: no scripts or life IO.
from tests.test_approval_classifier_690_s6 import ledger, _no_network  # noqa: F401,E402


async def test_loaded_allows_reaches_real_harness(tmp_path, ledger, monkeypatch):
    from loom.core.harness.permissions import ToolCapability
    from tests.test_permission_mode_704 import _env
    from tests.test_single_decision_704_s3 import _call, _register
    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    genome = _skill(env.ws)
    session = _session(env.ws)
    session.perm = env.perm
    added, source = _apply(session)
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(return_value=genome)),
                               [env.ws / "skills"], on_loaded=session._on_skill_loaded)
    result = await tool.executor(ToolCall(tool_name="load_skill", args={"name": genome.name},
        session_id="test", origin="chime", trust_level=TrustLevel.SAFE))
    assert result.success
    assert (await _call(env, "run_bash", {"command": "python3 skills/local-skill/helper.py run"},
                        caps=ToolCapability.EXEC)).success
    assert not (await _call(env, "run_bash", {"command": "python3 skills/local-skill/helper.py other"},
                            caps=ToolCapability.EXEC)).success
    assert (await env.run("write_file", {"path": "notes/synthetic.md", "content": "x"}, origin="chime")).success
    # Additive grants do not fence the ordinary classifier path.
    assert (await env.run("write_file", {"path": "elsewhere/synthetic.md", "content": "x"}, origin="chime")).success
    _register(env, "memorize", caps=ToolCapability.MUTATES)
    assert (await _call(env, "memorize", {"key": "synthetic", "value": "x"},
                        caps=ToolCapability.MUTATES)).success
    assert env.confirm.await_count == 0
    LoomDiscordBot._revoke_chime_permissions(None, session, added, source)


async def test_phase_chime_forwards_open_slot(tmp_path, monkeypatch):
    from loom.autonomy.circadian import state as st
    from loom.autonomy.circadian.lifecycle import CircadianConfig, _deliver_phase_chime
    monkeypatch.chdir(tmp_path)
    st.set_dir_for_test(tmp_path / "state")
    try:
        anchor = load_rhythm(_rhythm(tmp_path), workspace=tmp_path)[0]
        daemon = SimpleNamespace(permission_names_line="", deliver_chime=AsyncMock(return_value=True))
        await _deliver_phase_chime(daemon, CircadianConfig(), anchor, ())
        assert daemon.deliver_chime.call_args.args[0].open_slot is True
    finally:
        st.set_dir_for_test(None)
