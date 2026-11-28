"""PR #723 review regressions: real permission paths, synthetic IO only."""

import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from loom.autonomy.skill_allows import activity_permission_fields
from loom.core.harness.permissions import ToolCapability, TrustLevel
from loom.core.harness.middleware import ToolCall
from loom.core.harness.scope import ScopeGrant
from loom.core.harness.skill_checks import SkillCheckManager
from loom.core.infra.log_once import log_once
from loom.platform.cli.skill_tools import make_load_skill_tool
from tests.test_open_slots import _apply, _session, _skill
from tests.test_permission_mode_704 import _env, _decisions
from tests.test_single_decision_704_s3 import _call, _register
from tests.test_approval_classifier_690_s6 import ledger, _no_network  # noqa: F401


def _load_call(name="local-skill", origin="chime"):
    return ToolCall(tool_name="load_skill", args={"name": name}, origin=origin,
                    session_id="t", trust_level=TrustLevel.SAFE)


@pytest.mark.parametrize("tools,expected", [(["memorize", "spawn_agent"], ["memorize", "spawn_agent"]),
                                             ("memorize", []), (None, [])])
def test_open_parser_keeps_tolerant_declarations_only(tmp_path, tools, expected):
    fields = activity_permission_fields({"allowed_tools": tools, "scope_grants": [
        {"resource": "path", "action": "write", "selector": "outputs"}]},
        "open", tmp_path, open_slot=True)
    assert fields["allowed_tools"] == expected
    assert fields["scope_grants"] == []


@pytest.mark.parametrize("name,args,caps", [
    ("memorize", {"key": "synthetic", "value": "x"}, ToolCapability.MUTATES),
    ("pursuit_write", {"name": "synthetic", "content": "x"}, ToolCapability.MUTATES),
    ("spawn_agent", {"task": "summarize a synthetic note"}, ToolCapability.MUTATES),
    ("image_generate", {"prompt": "synthetic", "output_path": "outputs/image.png"}, ToolCapability.MUTATES | ToolCapability.NETWORK),
    ("write_file", {"path": "outputs/note.md", "content": "x"}, ToolCapability.MUTATES),
    ("run_bash", {"command": "ls outputs"}, ToolCapability.EXEC),
])
async def test_declared_open_tools_match_attended_behavior(tmp_path, ledger, monkeypatch, name, args, caps):
    from loom.platform.cli.tools import (_spawn_agent_resolver, _make_pursuit_write_resolver,
                                        _make_openai_image_scope_resolver)
    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    (env.ws / "outputs").mkdir()
    resolvers = {"spawn_agent": _spawn_agent_resolver,
                 "pursuit_write": _make_pursuit_write_resolver(),
                 "image_generate": _make_openai_image_scope_resolver(env.ws)}
    if name not in ("run_bash", "write_file"):
        _register(env, name, caps=caps, resolver=resolvers.get(name))
    attended = await _call(env, name, args, caps=caps, origin="interactive")
    assert attended.success, attended.error
    session = _session(env.ws)
    session.perm = env.perm
    fields = activity_permission_fields({"allowed_tools": [name]}, "open", env.ws, open_slot=True)
    _apply(session, tools=tuple(fields["allowed_tools"]))
    result = await _call(env, name, args, caps=caps)
    assert result.success, (result.error, await _decisions(ledger))
    assert env.perm.grants == []
    assert env.confirm.await_count == 0


async def test_skill_allows_are_positive_grants_without_ceiling(tmp_path, ledger, monkeypatch):
    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    (env.ws / "outputs").mkdir()
    session = _session(env.ws)
    session.perm = env.perm
    genome = _skill(env.ws)
    _apply(session)
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(return_value=genome)),
                               [env.ws / "skills"], on_loaded=session._on_skill_loaded)
    assert (await tool.executor(_load_call())).success
    for cmd in ("python3 skills/local-skill/helper.py run", "ls outputs", "git status", "gh pr view 723"):
        result = await _call(env, "run_bash", {"command": cmd}, caps=ToolCapability.EXEC)
        assert result.success, (cmd, result.error)
    for path in ("notes/own.md", "outputs/note.md", "autonomy/circadian/journal/synthetic.md"):
        result = await env.run("write_file", {"path": path, "content": "x"})
        assert result.success, (path, result.error)
    assert not (await env.run("write_file", {"path": "outputs/policy.toml", "content": "x"})).success
    assert not (await _call(env, "run_bash", {"command": "python3 skills/local-skill/helper.py run /etc/passwd"},
                            caps=ToolCapability.EXEC)).success


@pytest.mark.parametrize("open_source,grant_source,ceiling", [
    ("autonomy:open", "autonomy:open:skill:local", False),
    (None, "autonomy:open:skill:local", True),
    ("autonomy:open", "autonomy:other:skill:local", True),
    ("autonomy:open", "autonomy:bound", True),
])
def test_only_active_open_skill_source_is_exempt(tmp_path, open_source, grant_source, ceiling):
    from loom.core.harness.approval import _routine_ceiling
    session = _session(tmp_path)
    session.perm.open_slot_source = open_source
    session.perm.grant(ScopeGrant(resource="exec", action="execute", selector="x", source=grant_source))
    assert _routine_ceiling(session.perm, "exec", "execute") is ceiling


@pytest.mark.parametrize("checks", ["unapproved", "unresolvable", "no-manager", "partial", "none", "approved"])
async def test_allows_require_all_declared_checks_mounted(tmp_path, checks, caplog):
    from loom.core.harness.registry import ToolRegistry, ToolDefinition
    genome = _skill(tmp_path)
    d = tmp_path / "skills/local-skill"
    (d / "checks.py").write_text("async def guard(call):\n    return True\n")
    if checks != "none":
        genome.precondition_check_refs = [{"ref": "checks.guard", "applies_to": ["run_bash"]}]
    if checks == "unresolvable":
        genome.precondition_check_refs[0]["ref"] = "missing.guard"
    if checks == "partial":
        genome.precondition_check_refs[0]["applies_to"].append("missing_tool")
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="run_bash", description="", input_schema={},
                 trust_level=TrustLevel.GUARDED, executor=AsyncMock()))
    manager = None if checks == "no-manager" else SkillCheckManager(reg)
    session = _session(tmp_path)
    session._telemetry = {"loaded_skills": set()}
    _apply(session)
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(return_value=genome)), [tmp_path / "skills"],
        skill_check_manager=manager, confirm_fn=AsyncMock(return_value=True), on_loaded=session._on_skill_loaded)
    # Resolve-failure/approved cases settle approval on an attended load first.
    if checks in ("unresolvable", "partial", "approved"):
        assert (await tool.executor(_load_call(origin="interactive"))).success
        assert session.perm.grants == []
    with caplog.at_level(logging.INFO):
        assert (await tool.executor(_load_call())).success
        assert (await tool.executor(_load_call())).success  # dedup must also fail closed
    assert bool(session.perm.grants) is (checks in ("none", "approved"))
    assert "local-skill" in session._telemetry["loaded_skills"]
    if checks not in ("none", "approved"):
        assert "checks" in caplog.text.lower()


@pytest.mark.parametrize("origin", ["interactive", "discord"])
async def test_attended_load_never_installs_allows_even_with_stale_flag(tmp_path, origin):
    genome = _skill(tmp_path)
    session = _session(tmp_path)
    _apply(session)
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(return_value=genome)), [tmp_path / "skills"],
                               on_loaded=session._on_skill_loaded)
    assert (await tool.executor(_load_call(origin=origin))).success
    assert session.perm.grants == []
    assert session.perm.declared_tools() == frozenset()


async def test_foreign_exact_name_does_not_borrow_workspace_variant(tmp_path):
    _skill(tmp_path, "my_skill")
    foreign = tmp_path / "foreign"
    genome = _skill(foreign, "my-skill")
    session = _session(tmp_path)
    _apply(session)
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(return_value=genome)),
        [tmp_path / "skills", foreign / "skills"], on_loaded=session._on_skill_loaded)
    result = await tool.executor(_load_call("my-skill"))
    assert result.success
    assert str(foreign / "skills/my-skill") in result.output
    assert session.perm.grants == []


def test_schedule_info_does_not_swallow_later_warning(tmp_path, caplog):
    entry = {"scope_grants": [], "legacy_reason": "intentional"}
    with caplog.at_level(logging.INFO):
        for _ in range(2):
            activity_permission_fields(entry, "same-schedule", tmp_path)
        del entry["legacy_reason"]
        for _ in range(2):
            activity_permission_fields(entry, "same-schedule", tmp_path)
    assert [r.levelno for r in caplog.records] == [logging.INFO, logging.WARNING]


def test_log_once_threads_share_keys(caplog):
    logger = logging.getLogger("open-slot-log-threads")
    with caplog.at_level(logging.INFO):
        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(lambda i: log_once(logger, logging.INFO, i % 3, "key %s", i % 3), range(150)))
    assert sorted(r.getMessage() for r in caplog.records) == ["key 0", "key 1", "key 2"]


async def test_invalid_allows_loads_body_without_grants(tmp_path, caplog):
    genome = _skill(tmp_path)
    (tmp_path / "skills/local-skill/SKILL.md").write_text("---\nallows: {tools: [run_bash]}\n---\n")
    session = _session(tmp_path)
    _apply(session)
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(return_value=genome)),
                               [tmp_path / "skills"], on_loaded=session._on_skill_loaded)
    result = await tool.executor(_load_call())
    assert result.success
    assert session.perm.grants == []
    assert "gets no allows" in caplog.text


async def test_declaring_exec_does_not_authorize_skill_script(tmp_path, ledger, monkeypatch, caplog):
    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    _skill(env.ws)
    session = _session(env.ws)
    session.perm = env.perm
    session.registry = env.blast._registry
    _apply(session, tools=("run_bash",))
    assert "declaration never covers shell" in caplog.text
    assert env.perm.grants == []
    await _call(env, "run_bash", {"command": "python3 skills/local-skill/helper.py run"}, caps=ToolCapability.EXEC)
    assert (await _decisions(ledger))[-1]["decision_source"] not in ("program", "grant")


async def test_unmounted_checks_revoke_prior_skill_allows(tmp_path):
    from loom.core.harness.registry import ToolRegistry, ToolDefinition
    guarded = _skill(tmp_path)
    guarded.precondition_check_refs = [{"ref": "checks.guard", "applies_to": ["run_bash"]}]
    (tmp_path / "skills/local-skill/checks.py").write_text("async def guard(call):\n    return True\n")
    other = _skill(tmp_path, "other-skill")
    genomes = {g.name: g for g in (guarded, other)}
    reg = ToolRegistry()
    reg.register(ToolDefinition(name="run_bash", description="", input_schema={},
                 trust_level=TrustLevel.GUARDED, executor=AsyncMock()))
    session = _session(tmp_path)
    _apply(session)
    manager = SkillCheckManager(reg)
    tool = make_load_skill_tool(SimpleNamespace(get=AsyncMock(side_effect=lambda n: genomes.get(n))),
        [tmp_path / "skills"], skill_check_manager=manager,
        confirm_fn=AsyncMock(return_value=True), on_loaded=session._on_skill_loaded)
    assert (await tool.executor(_load_call(origin="interactive"))).success
    assert (await tool.executor(_load_call())).success
    assert any(g.source.endswith(":local-skill") for g in session.perm.grants)
    assert (await tool.executor(_load_call("other-skill"))).success
    assert "local-skill" not in manager.mounted_skills()
    assert not any(g.source.endswith(":local-skill") for g in session.perm.grants)
    assert (await tool.executor(_load_call())).success
    assert not any(g.source.endswith(":local-skill") for g in session.perm.grants)
