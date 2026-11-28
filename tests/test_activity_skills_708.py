"""S2: real load -> chime -> scope resolver/middleware, synthetic workspace only."""
from types import SimpleNamespace

import pytest
import yaml

from loom.autonomy.circadian.rhythm import load_rhythm
from loom.autonomy.circadian.lifecycle import _deliver_phase_chime
from loom.autonomy.schedules import load_schedules
from loom.core.harness.mode import PermissionMode
from loom.core.harness.permissions import ToolCapability
from loom.core.harness.scope import ScopeGrant
from loom.platform.discord.bot import LoomDiscordBot
from tests.test_approval_classifier_690_s6 import ledger, _no_network  # noqa: F401
from tests.test_permission_mode_704 import _env, _decisions
from tests.test_single_decision_704_s3 import _call
from tests.test_circadian_phase_chime import _make_daemon, CFG
from tests.test_memory_documents import db  # noqa: F401
from loom.autonomy.circadian import state as st

JOURNEY = "python3 skills/world-journey/journey.py now"
PET = "python3 skills/pet-cat/pet.py"


def skill(ws, name, execs=(), writes=(), tools=()):
    d = ws / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    for shape in execs:
        script = ws / shape.split()[1]
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# never executed\n")
    (d / "SKILL.md").write_text("---\n" + yaml.safe_dump({
        "name": name, "allows": {"exec": list(execs), "write": list(writes), "tools": list(tools)}
    }) + "---\nSynthetic instructions.\n")


def config(ws, kind, extra):
    path = ws / "autonomy" / ("circadian/rhythm.toml" if kind == "anchor" else "schedules.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    head = ('[[anchors]]\nname="dawn"\ntime="08:30"\n' if kind == "anchor" else
            '[[schedules]]\nname="morning_briefing"\ncron="0 1 * * *"\nintent="news"\n')
    sibling = ('\n[[anchors]]\nname="healthy"\ntime="10:00"\n' if kind == "anchor" else
               '\n[[schedules]]\nname="healthy"\nintent="ok"\n')
    path.write_text(head + extra + sibling)
    return path


def load(path, kind):
    return load_rhythm(path) if kind == "anchor" else load_schedules(path)["schedules"]


def fields(item, key):
    return getattr(item, key) if not isinstance(item, dict) else item[key]


async def dawn_request(ws, monkeypatch, extra='skills=["pet-cat", "world-journey"]\n'):
    monkeypatch.chdir(ws)
    skill(ws, "world-journey", [JOURNEY])
    skill(ws, "pet-cat", [PET + " status", PET + " buy"])
    anchor = load_rhythm(config(ws, "anchor", extra))[0]
    st.set_dir_for_test(ws / "circadian")
    try:
        daemon, requests, _ = _make_daemon()
        await _deliver_phase_chime(daemon, CFG, anchor)
        return requests[0]
    finally:
        st.set_dir_for_test(None)


def apply(env, req):
    session = SimpleNamespace(perm=env.perm, registry=env.blast._registry)
    return LoomDiscordBot._apply_chime_permissions(None, req, session)


async def test_0830_dawn_journey_now_reaches_real_scope_path(tmp_path, ledger, monkeypatch):
    env = _env(tmp_path, ledger, mode=PermissionMode.SELF, grants=[ScopeGrant(
        resource="exec", action="execute", selector=PET + " status", source="autonomy:previous")])
    req = await dawn_request(env.ws, monkeypatch)
    apply(env, req)
    r = await _call(env, "run_bash", {"command": JOURNEY}, caps=ToolCapability.EXEC)
    assert r.success, (r.error, await _decisions(ledger))
    assert env.confirm.await_count == 0


@pytest.mark.parametrize("kind", ["anchor", "schedule"])
def test_union_is_resolved_at_load_and_not_cwd(tmp_path, monkeypatch, kind):
    skill(tmp_path, "world-journey", [JOURNEY])
    skill(tmp_path, "pet-cat", [PET + " status"])
    monkeypatch.chdir(tmp_path.parent)
    item = load(config(tmp_path, kind, 'skills=["pet-cat", "world-journey"]\n'), kind)[0]
    assert {g["selector"] for g in fields(item, "scope_grants")} == {PET + " status", JOURNEY}
    assert all(g["constraints"] == {"absolute_paths": "deny"} for g in fields(item, "scope_grants"))


@pytest.mark.parametrize("kind", ["anchor", "schedule"])
@pytest.mark.parametrize("extra", [
    'skills=["missing"]\n', 'skills="pet-cat"\n', 'skills=[]\n',
    'skills=["pet-cat", 3]\n',
    'skills=["pet-cat"]\nscope_grants=[{resource="exec", action="execute", selector="python3 skills/other/x.py"}]\n',
    'skills=["pet-cat"]\nscope_grants=[{resource="exec"}]\n',
    'skills=["pet-cat"]\nscope_grants="broken"\n',
    'skills=["pet-cat"]\nscope_grants=[{resource="exec", action="execute", selector="python3 skills/pet-cat/pet.py status", constraints={max_calls="2"}}]\n',
])
def test_bad_activity_is_loudly_rejected_without_silencing_siblings(tmp_path, kind, extra, caplog):
    skill(tmp_path, "pet-cat", [PET + " status"])
    items = load(config(tmp_path, kind, extra), kind)
    assert [fields(x, "name") for x in items] == ["healthy"]
    assert any(r.levelname == "ERROR" and ("dawn" in r.message or "morning_briefing" in r.message)
               for r in caplog.records)


@pytest.mark.parametrize("kind", ["anchor", "schedule"])
@pytest.mark.parametrize("broken", ["no_allows", "missing_script", "outside_script"])
def test_invalid_skill_is_rejected_during_load(tmp_path, kind, broken, caplog):
    skill(tmp_path, "pet-cat", [PET + " status"])
    p = tmp_path / "skills/pet-cat/SKILL.md"
    if broken == "no_allows":
        p.write_text("---\nname: pet-cat\n---\n")
    elif broken == "missing_script":
        (p.parent / "pet.py").unlink()
    else:
        p.write_text("---\nallows:\n  exec: [python3 skills/other/run.py]\n---\n")
    assert [fields(x, "name") for x in load(config(tmp_path, kind, 'skills=["pet-cat"]\n'), kind)] == ["healthy"]
    assert any(r.levelname == "ERROR" and "pet-cat" in r.message for r in caplog.records)


@pytest.mark.parametrize("kind", ["anchor", "schedule"])
def test_legacy_non_skill_scope_is_preserved_with_explicit_migration_warning(tmp_path, kind, caplog):
    item = load(config(tmp_path, kind,
        'scope_grants=[{resource="exec",action="execute",selector="workspace"}]\n'), kind)[0]
    if kind == "anchor":
        assert fields(item, "scope_grants") == ()
        assert item.open_slot
        assert not caplog.records
        return
    assert fields(item, "scope_grants")[0]["selector"] == "workspace"
    assert any(r.levelname == "WARNING" and "legacy" in r.message and "skills" in r.message
               for r in caplog.records)


@pytest.mark.parametrize("kind,migrated", [("anchor", False), ("anchor", True), ("schedule", False), ("schedule", True)])
@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_general_declarations_survive_migration_without_becoming_skill_grants(
        tmp_path, ledger, monkeypatch, kind, mode, migrated):
    from loom.autonomy.chime import ChimeRequest
    from loom.core.harness.permissions import TrustLevel
    from tests.test_single_decision_704_s3 import _register
    from datetime import datetime, timezone

    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    skill(env.ws, "pet-cat", [PET + " status"], ["news"])
    # Register ordinary tools by their existing real effects: memorize has
    # no scoped effect; fetch_url/search use their real registered resolvers.
    from loom.platform.cli.tools import _fetch_url_resolver
    _register(env, "memorize", caps=ToolCapability.MUTATES)
    _register(env, "fetch_url", resolver=_fetch_url_resolver, caps=ToolCapability.NETWORK)
    declarations = '["memorize","web_search","fetch_url","write_file","run_bash"]'
    boundary = ('skills=["pet-cat"]\n' if migrated else
        f'scope_grants=[{{resource="exec",action="execute",selector="{PET} status"}},'
        '{resource="path",action="write",selector="news"}]\n')
    item = load(config(env.ws, kind, boundary + 'allowed_tools=' + declarations + '\n'), kind)[0]
    req = ChimeRequest(schedule_name="activity", intent="Synthetic", fired_at=datetime.now(timezone.utc),
        input_trust="local",  # synthetic operator anchor/schedule above
        allowed_tools=tuple(fields(item, "allowed_tools")), scope_grants=tuple(fields(item, "scope_grants")),
        open_slot=kind == "anchor" and not migrated)
    apply(env, req)
    for name, args, caps in [
        ("memorize", {"key": "note", "value": "Synthetic"}, ToolCapability.MUTATES),
        ("web_search", {"query": "public astronomy"}, ToolCapability.NETWORK | ToolCapability.READ_PROBE),
        ("fetch_url", {"url": "https://example.com"}, ToolCapability.NETWORK),
    ]:
        assert (await _call(env, name, args, caps=caps)).success
        assert env.confirm.await_count == 0
        assert (await _decisions(ledger))[-1]["decision_source"] == "program"
    # Declaration fast-allow never grants EXEC or crosses a write ceiling.
    if kind == "anchor" and not migrated:
        result = await _call(env, "run_bash", {"command": PET + " buy bowl"}, caps=ToolCapability.EXEC)
        assert "scope_ceiling" not in result.metadata.get("skip_note", "")
        assert (await _decisions(ledger))[-1]["decision_source"] != "grant"
        assert (await env.run("write_file", {"path": "outputs/out.md", "content": "x"})).success
        assert env.perm.grants == []
    else:
        assert not (await _call(env, "run_bash", {"command": PET + " buy bowl"}, caps=ToolCapability.EXEC)).success
        assert not (await env.run("write_file", {"path": "outputs/out.md", "content": "x"})).success
    assert not (await env.run("write_file", {"path": "news/room.toml", "content": "x"})).success
    assert not (await _call(env, "memorize", {"key": "../outside", "value": "x"}, caps=ToolCapability.MUTATES)).success
    assert not (await _call(env, "memorize", {"key": "k", "value": "x"}, caps=ToolCapability.MUTATES,
                            trust=TrustLevel.CRITICAL)).success
    assert env.confirm.await_count == 0


async def test_buy_narrowing_uses_two_calls_and_never_an_uncapped_union(tmp_path, ledger, monkeypatch):
    env = _env(tmp_path, ledger, mode=PermissionMode.SELF)
    req = await dawn_request(env.ws, monkeypatch, 'skills=["pet-cat", "world-journey"]\n'
        f'scope_grants=[{{resource="exec",action="execute",selector="{PET} buy",constraints={{max_calls=2}}}},'
        f'{{resource="exec",action="execute",selector="{PET} status"}}]\n')
    apply(env, req)
    results = [await _call(env, "run_bash", {"command": PET + " buy bowl"}, caps=ToolCapability.EXEC)
               for _ in range(3)]
    assert [r.success for r in results] == [True, True, False]
    assert (await _call(env, "run_bash", {"command": PET + " status"}, caps=ToolCapability.EXEC)).success
    assert not (await _call(env, "run_bash", {"command": JOURNEY}, caps=ToolCapability.EXEC)).success


@pytest.mark.parametrize("suffix", [
    " && echo x", " || echo x", "; echo x", " | cat", "\necho x", "$(echo x)",
    " `echo x`", " > out.txt", " 2> out.txt", " < input", " >> out", " & echo x",
    " --path /etc/passwd", " --x $(python3 other.py)",
])
async def test_dawn_shapes_refuse_other_effects(tmp_path, ledger, monkeypatch, suffix):
    env = _env(tmp_path, ledger)
    req = await dawn_request(env.ws, monkeypatch)
    apply(env, req)
    r = await _call(env, "run_bash", {"command": JOURNEY + suffix}, caps=ToolCapability.EXEC)
    assert not r.success


@pytest.mark.parametrize("command", [
    "env " + JOURNEY, "X=1 " + JOURNEY, "(" + JOURNEY + ")",
    "python3 skills/world-journey/other.py now", "python3 -c 'x' skills/world-journey/journey.py now",
    "python3 skills/world-journey/journey.py nowhere", PET + " play",
])
async def test_dawn_refuses_other_scripts_and_prefix_escapes(tmp_path, ledger, monkeypatch, command):
    env = _env(tmp_path, ledger)
    apply(env, await dawn_request(env.ws, monkeypatch))
    assert not (await _call(env, "run_bash", {"command": command}, caps=ToolCapability.EXEC)).success


@pytest.mark.parametrize("mode", list(PermissionMode))
@pytest.mark.parametrize("origin", ["discord", "chime"])
async def test_real_load_skill_neither_needs_nor_creates_grants(tmp_path, ledger, db, monkeypatch, mode, origin):
    from loom.core.harness.middleware import ToolCall
    from loom.core.memory.procedural import ProceduralMemory, SkillGenome
    from loom.platform.cli.skill_tools import make_load_skill_tool

    env = _env(tmp_path, ledger, mode=mode)
    # An already bounded activity may load a different skill, but its ceiling
    # must not grow. A conversation with no routine starts with no grants.
    if origin == "chime":
        apply(env, await dawn_request(env.ws, monkeypatch))
    skill(env.ws, "extra", ["python3 skills/extra/extra.py"])
    proc = ProceduralMemory(db)
    await proc.upsert(SkillGenome(name="extra", body=(env.ws / "skills/extra/SKILL.md").read_text()))
    tool = make_load_skill_tool(proc, skills_dirs=[env.ws / "skills"])
    env.blast._registry.register(tool)
    before = [(g.resource, g.selector, dict(g.constraints)) for g in env.perm.grants]
    call = ToolCall(tool_name=tool.name, args={"name": "extra"}, origin=origin,
                    trust_level=tool.trust_level, capabilities=tool.capabilities, session_id="t")
    from loom.core.ledger.correlation import turn_scope
    with turn_scope("load_test"):
        result = await env.blast.process(call, tool.executor)
    assert result.success and 'skill_content name="extra"' in result.output
    assert [(g.resource, g.selector, dict(g.constraints)) for g in env.perm.grants] == before
    assert env.perm.mode is mode
    assert env.confirm.await_count == 0
    # Loading does not bypass a ceiling or mode. YOLO without a routine
    # ceiling already permits this call by mode, independently of allows.
    r = await _call(env, "run_bash", {"command": "python3 skills/extra/extra.py"}, caps=ToolCapability.EXEC)
    assert r.success is (mode is PermissionMode.YOLO and origin == "discord")


async def test_news_schedule_through_daemon_chime_and_revoke(tmp_path, ledger, monkeypatch):
    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    news = "python3 skills/news-aggregator/scripts/fetch_news.py"
    skill(env.ws, "news-aggregator", [news], ["news"])
    config(env.ws, "schedule", 'skills=["news-aggregator"]\nmode="chime"\n'
           '[schedules.target]\ntype="discord_thread"\nid="123"\n')
    loom_config = env.ws / "loom.toml"
    loom_config.write_text('[autonomy]\nenabled=true\n')
    daemon, requests, _ = _make_daemon()
    assert daemon.load_config(loom_config) == 2
    trigger = next(t for t in daemon.evaluator.list() if t.name == "morning_briefing")
    await daemon._deliver_chime(await daemon._planner.handle(trigger, {}))
    req = requests[0]
    added, source = apply(env, req)
    assert (await _call(env, "run_bash", {"command": news + " --limit 8"}, caps=ToolCapability.EXEC)).success
    assert (await env.run("write_file", {"path": "news/today.md", "content": "Synthetic."})).success
    assert not (await env.run("write_file", {"path": "outputs/out.md", "content": "Synthetic."})).success
    LoomDiscordBot._revoke_chime_permissions(None, SimpleNamespace(perm=env.perm), added, source)
    assert not (await _call(env, "run_bash", {"command": news}, caps=ToolCapability.EXEC)).success


@pytest.mark.parametrize("mode", list(PermissionMode))
async def test_general_media_cannot_hide_a_path_expansion_behind_new_network_effect(
        tmp_path, ledger, monkeypatch, mode):
    from loom.autonomy.chime import ChimeRequest
    from loom.platform.cli.tools import _make_openai_image_scope_resolver
    from tests.test_single_decision_704_s3 import _register
    from datetime import datetime, timezone

    env = _env(tmp_path, ledger, mode=mode)
    monkeypatch.chdir(env.ws)
    skill(env.ws, "news-aggregator", ["python3 skills/news-aggregator/scripts/fetch_news.py"], ["news"])
    item = load_schedules(config(env.ws, "schedule", 'skills=["news-aggregator"]\n'
                          'allowed_tools=["image_generate"]\n'))["schedules"][0]
    caps = ToolCapability.NETWORK | ToolCapability.MUTATES
    _register(env, "image_generate", resolver=_make_openai_image_scope_resolver(env.ws), caps=caps)
    apply(env, ChimeRequest(schedule_name="news", intent="Synthetic", fired_at=datetime.now(timezone.utc),
        input_trust="local",  # synthetic operator news schedule
                           allowed_tools=tuple(item["allowed_tools"]), scope_grants=tuple(item["scope_grants"])))
    assert (await _call(env, "image_generate", {"prompt": "cat", "output_path": "news/cat.png"}, caps=caps)).success
    out = await _call(env, "image_generate", {"prompt": "cat", "output_path": "outputs/cat.png"}, caps=caps)
    assert not out.success
    assert "scope_ceiling" in out.metadata["skip_note"]


async def test_recurring_pet_turns_each_keep_the_buy_cap_and_other_care(tmp_path, ledger, monkeypatch):
    env = _env(tmp_path, ledger)
    monkeypatch.chdir(env.ws)
    skill(env.ws, "pet-cat", [PET + " status", PET + " buy"])
    p = config(env.ws, "anchor", 'skills=["pet-cat"]\nallowed_tools=["memorize"]\n'
        f'scope_grants=[{{resource="exec",action="execute",selector="{PET} buy",constraints={{max_calls=2}}}},'
        f'{{resource="exec",action="execute",selector="{PET} status"}}]\n')
    p.write_text(p.read_text().replace('name="dawn"\ntime="08:30"',
                                       'name="pet"\ntime=["10:00","19:00"]'))
    anchors = [a for a in load_rhythm(p) if a.name == "pet"]
    assert len(anchors) == 2
    st.set_dir_for_test(env.ws / "circadian")
    try:
        daemon, requests, _ = _make_daemon()
        for anchor in anchors:
            await _deliver_phase_chime(daemon, CFG, anchor)
            added, source = apply(env, requests[-1])
            results = [await _call(env, "run_bash", {"command": PET + " buy bowl"}, caps=ToolCapability.EXEC)
                       for _ in range(3)]
            assert [r.success for r in results] == [True, True, False]
            assert (await _call(env, "run_bash", {"command": PET + " status"}, caps=ToolCapability.EXEC)).success
            LoomDiscordBot._revoke_chime_permissions(None, SimpleNamespace(perm=env.perm), added, source)
            assert env.perm.grants == []
    finally:
        st.set_dir_for_test(None)
