"""
Tests for Phase 4C — Extensibility Layer.

Coverage
--------
LensResult
  - basic construction
  - is_empty True when all lists empty
  - is_empty False when skills present
  - is_empty False when adapters present

BaseLens / LensRegistry
  - register and get by name
  - detect returns matching lenses
  - detect returns empty for unsupported
  - extract with explicit lens_name
  - extract auto-detects
  - extract returns None when no match
  - registered_names property

HermesLens
  - supports() True for dict with 'skills'
  - supports() False for dict without 'skills'
  - supports() True for JSON string with 'skills'
  - extract() happy path — skills, tags, confidence
  - extract() falls back to 'description' when 'body' absent
  - extract() skips skill with no name (adds warning)
  - extract() skips skill with no body/description (adds warning)
  - extract() respects confidence from source
  - extract() defaults confidence to 0.8
  - extract() handles empty skills list gracefully
  - extract() invalid source → empty result + warning

ClawCodeLens
  - supports() True for dict with 'tools'
  - supports() False for dict without 'tools'
  - extract() converts tools to adapters (name, description, trust_level)
  - extract() handles unknown trust → defaults to 'safe' with warning
  - extract() extracts middleware_patterns
  - extract() skips tool with no name (adds warning)
  - extract() uses default input_schema when 'parameters' absent

SkillImportPipeline (async, real DB)
  - process() returns one decision per skill
  - schema validation rejects missing 'name'
  - schema validation rejects missing 'body'
  - confidence gate rejects below threshold
  - confidence gate approves above threshold
  - dedup rejects already-existing skill
  - import_approved() persists approved skills
  - import_approved() skips rejected skills
  - import_approved() returns correct count
  - custom min_confidence respected

AdapterRegistry
  - register() then get()
  - all() returns all registered
  - install_into() copies tools to ToolRegistry
  - @registry.tool uses function name
  - @registry.tool uses docstring as description
  - @registry.tool respects custom description
  - @registry.tool sets trust_level correctly
  - @registry.tool registers into registry
  - from_lens_result() builds registry from LensResult adapters
  - from_lens_result() placeholder executor returns error result

RelationalMemory (async, real DB)
  - upsert() inserts new entry
  - get() retrieves by subject+predicate
  - get() returns None for missing pair
  - upsert() updates object for same (subject, predicate)
  - query() by subject returns all matching
  - query() by predicate returns all matching
  - query() by both is exact lookup
  - query() no filter returns all entries
  - query() returns empty list when no entries
  - delete() removes entry, returns True
  - delete() returns False when entry not found
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from loom.core.memory.store import SQLiteStore
from loom.core.memory.procedural import SkillGenome, ProceduralMemory
from loom.core.memory.relational import RelationalEntry, RelationalMemory
from loom.core.harness.middleware import ToolCall, ToolResult
from loom.core.harness.permissions import TrustLevel
from loom.core.harness.registry import ToolRegistry
from loom.extensibility.lens import BaseLens, LensResult, LensRegistry
from loom.extensibility.hermes import HermesLens
from loom.extensibility.claw import ClawCodeLens
from loom.extensibility.pipeline import ImportDecision, SkillImportPipeline
from loom.extensibility.adapter import AdapterRegistry


# ---------------------------------------------------------------------------
# DB Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test.db")


@pytest_asyncio.fixture
async def store(tmp_db):
    s = SQLiteStore(tmp_db)
    await s.initialize()
    return s


@pytest_asyncio.fixture
async def db_conn(store):
    async with store.connect() as conn:
        yield conn


@pytest_asyncio.fixture
async def procedural(db_conn):
    return ProceduralMemory(db_conn)


@pytest_asyncio.fixture
async def relational(db_conn):
    return RelationalMemory(db_conn)


def _tool_call(name: str = "t", args: dict | None = None) -> ToolCall:
    return ToolCall(
        id="test-id", tool_name=name,
        args=args or {},
        trust_level=TrustLevel.SAFE,
        session_id="s1",
    )


# ---------------------------------------------------------------------------
# Concrete BaseLens for testing
# ---------------------------------------------------------------------------

class _DummyLens(BaseLens):
    name = "dummy"
    version = "test"

    def supports(self, source):
        parsed = self._parse(source)
        return isinstance(parsed, dict) and "dummy" in parsed

    def extract(self, source):
        return LensResult(source="dummy", skills=[{"name": "x", "body": "y"}])


# ===========================================================================
# LensResult
# ===========================================================================

class TestLensResult:
    def test_basic_construction(self):
        r = LensResult(source="test")
        assert r.source == "test"
        assert r.skills == []
        assert r.warnings == []

    def test_is_empty_true_when_all_empty(self):
        assert LensResult(source="t").is_empty is True

    def test_is_empty_false_with_skills(self):
        r = LensResult(source="t", skills=[{"name": "x", "body": "y"}])
        assert r.is_empty is False

    def test_is_empty_false_with_adapters(self):
        r = LensResult(source="t", platform_adapters=[{"name": "tool"}])
        assert r.is_empty is False

    def test_is_empty_false_with_middleware(self):
        r = LensResult(source="t", middleware_patterns=[{"name": "m"}])
        assert r.is_empty is False


# ===========================================================================
# LensRegistry
# ===========================================================================

class TestLensRegistry:
    def test_register_and_get(self):
        reg = LensRegistry()
        lens = _DummyLens()
        reg.register(lens)
        assert reg.get("dummy") is lens

    def test_get_unknown_returns_none(self):
        reg = LensRegistry()
        assert reg.get("missing") is None

    def test_detect_returns_matching_lenses(self):
        reg = LensRegistry()
        reg.register(_DummyLens())
        matches = reg.detect({"dummy": True})
        assert len(matches) == 1
        assert matches[0].name == "dummy"

    def test_detect_returns_empty_for_unsupported(self):
        reg = LensRegistry()
        reg.register(_DummyLens())
        assert reg.detect({"other_key": True}) == []

    def test_extract_with_lens_name(self):
        reg = LensRegistry()
        reg.register(_DummyLens())
        result = reg.extract({"dummy": True}, lens_name="dummy")
        assert result is not None
        assert result.source == "dummy"

    def test_extract_auto_detect(self):
        reg = LensRegistry()
        reg.register(_DummyLens())
        result = reg.extract({"dummy": True})
        assert result is not None

    def test_extract_returns_none_when_no_match(self):
        reg = LensRegistry()
        assert reg.extract({"unknown": True}) is None

    def test_extract_unknown_lens_name_returns_none(self):
        reg = LensRegistry()
        reg.register(_DummyLens())
        assert reg.extract({"dummy": True}, lens_name="ghost") is None

    def test_registered_names(self):
        reg = LensRegistry()
        reg.register(_DummyLens())
        assert "dummy" in reg.registered_names


# ===========================================================================
# HermesLens
# ===========================================================================

class TestHermesLens:
    def _lens(self):
        return HermesLens()

    def test_supports_dict_with_skills(self):
        assert self._lens().supports({"skills": []}) is True

    def test_supports_false_without_skills(self):
        assert self._lens().supports({"tools": []}) is False

    def test_supports_json_string(self):
        import json
        s = json.dumps({"skills": []})
        assert self._lens().supports(s) is True

    def test_extract_happy_path(self):
        source = {
            "skills": [
                {
                    "name": "refactor",
                    "body": "Extract when over 30 lines",
                    "tags": ["python"],
                    "confidence": 0.9,
                }
            ]
        }
        result = self._lens().extract(source)
        assert len(result.skills) == 1
        s = result.skills[0]
        assert s["name"] == "refactor"
        assert s["body"] == "Extract when over 30 lines"
        assert "python" in s["tags"]
        assert s["confidence"] == pytest.approx(0.9)
        assert result.warnings == []

    def test_extract_uses_description_as_body_fallback(self):
        source = {"skills": [{"name": "skill", "description": "desc body"}]}
        result = self._lens().extract(source)
        assert result.skills[0]["body"] == "desc body"

    def test_extract_skips_skill_without_name(self):
        source = {"skills": [{"body": "no name here"}]}
        result = self._lens().extract(source)
        assert len(result.skills) == 0
        assert any("no 'name'" in w for w in result.warnings)

    def test_extract_skips_skill_without_body(self):
        source = {"skills": [{"name": "orphan"}]}
        result = self._lens().extract(source)
        assert len(result.skills) == 0
        assert any("orphan" in w for w in result.warnings)

    def test_extract_confidence_defaults_to_0_8(self):
        source = {"skills": [{"name": "x", "body": "y"}]}
        result = self._lens().extract(source)
        assert result.skills[0]["confidence"] == pytest.approx(0.8)

    def test_extract_empty_skills_list(self):
        result = self._lens().extract({"skills": []})
        assert result.skills == []
        assert result.warnings == []

    def test_extract_invalid_source_returns_warning(self):
        result = self._lens().extract("not valid json {{{}}")
        assert result.is_empty
        assert len(result.warnings) > 0

    def test_extract_skills_list_not_a_list(self):
        result = self._lens().extract({"skills": "not a list"})
        assert result.is_empty
        assert len(result.warnings) > 0


# ===========================================================================
# ClawCodeLens
# ===========================================================================

class TestClawCodeLens:
    def _lens(self):
        return ClawCodeLens()

    def test_supports_dict_with_tools(self):
        assert self._lens().supports({"tools": []}) is True

    def test_supports_false_without_tools(self):
        assert self._lens().supports({"skills": []}) is False

    def test_extract_tools_to_adapters(self):
        source = {
            "tools": [
                {
                    "name": "search_web",
                    "description": "Search the web",
                    "trust": "safe",
                    "tags": ["web"],
                }
            ]
        }
        result = self._lens().extract(source)
        assert len(result.platform_adapters) == 1
        a = result.platform_adapters[0]
        assert a["name"] == "search_web"
        assert a["trust_level"] == "safe"
        assert "web" in a["tags"]

    def test_extract_unknown_trust_defaults_safe_with_warning(self):
        source = {"tools": [{"name": "t", "description": "d", "trust": "mega"}]}
        result = self._lens().extract(source)
        assert result.platform_adapters[0]["trust_level"] == "safe"
        assert any("unknown trust" in w for w in result.warnings)

    def test_extract_middleware_patterns(self):
        source = {
            "tools": [],
            "middleware": [{"name": "RateLimit", "description": "Throttles calls"}],
        }
        result = self._lens().extract(source)
        assert len(result.middleware_patterns) == 1
        assert result.middleware_patterns[0]["name"] == "RateLimit"

    def test_extract_skips_tool_without_name(self):
        source = {"tools": [{"description": "orphan"}]}
        result = self._lens().extract(source)
        assert len(result.platform_adapters) == 0
        assert any("no 'name'" in w for w in result.warnings)

    def test_extract_default_input_schema(self):
        source = {"tools": [{"name": "t", "trust": "safe"}]}
        result = self._lens().extract(source)
        schema = result.platform_adapters[0]["input_schema"]
        assert schema["type"] == "object"

    def test_extract_invalid_source_returns_warning(self):
        result = self._lens().extract("not json at all")
        assert result.is_empty
        assert len(result.warnings) > 0


# ===========================================================================
# SkillImportPipeline
# ===========================================================================

class TestSkillImportPipeline:
    def _make_skill(self, name="skill", body="body text", confidence=0.8, tags=None):
        return {"name": name, "body": body, "confidence": confidence, "tags": tags or []}

    async def test_process_returns_one_decision_per_skill(self, procedural):
        pipeline = SkillImportPipeline(procedural)
        decisions = await pipeline.process([self._make_skill("a"), self._make_skill("b")])
        assert len(decisions) == 2

    async def test_schema_rejects_missing_name(self, procedural):
        pipeline = SkillImportPipeline(procedural)
        [d] = await pipeline.process([{"body": "body"}])
        assert d.approved is False
        assert "name" in d.reason

    async def test_schema_rejects_missing_body(self, procedural):
        pipeline = SkillImportPipeline(procedural)
        [d] = await pipeline.process([{"name": "x"}])
        assert d.approved is False
        assert "body" in d.reason

    async def test_confidence_gate_rejects_below_threshold(self, procedural):
        pipeline = SkillImportPipeline(procedural, min_confidence=0.6)
        [d] = await pipeline.process([self._make_skill(confidence=0.4)])
        assert d.approved is False
        assert "threshold" in d.reason

    async def test_confidence_gate_approves_above_threshold(self, procedural):
        pipeline = SkillImportPipeline(procedural, min_confidence=0.5)
        [d] = await pipeline.process([self._make_skill(confidence=0.7)])
        assert d.approved is True

    async def test_dedup_rejects_existing_skill(self, procedural):
        await procedural.upsert(SkillGenome(name="existing", body="already there"))
        pipeline = SkillImportPipeline(procedural)
        [d] = await pipeline.process([self._make_skill("existing")])
        assert d.approved is False
        assert "already exists" in d.reason

    async def test_import_approved_persists_skills(self, procedural):
        pipeline = SkillImportPipeline(procedural)
        skills = [self._make_skill("new_skill")]
        decisions = await pipeline.process(skills)
        count = await pipeline.import_approved(decisions, skills)
        assert count == 1
        stored = await procedural.get("new_skill")
        assert stored is not None
        assert stored.body == "body text"

    async def test_import_approved_skips_rejected(self, procedural):
        pipeline = SkillImportPipeline(procedural, min_confidence=0.9)
        skills = [self._make_skill("low", confidence=0.3)]
        decisions = await pipeline.process(skills)
        count = await pipeline.import_approved(decisions, skills)
        assert count == 0
        assert await procedural.get("low") is None

    async def test_import_approved_returns_correct_count(self, procedural):
        pipeline = SkillImportPipeline(procedural, min_confidence=0.5)
        skills = [
            self._make_skill("ok1", confidence=0.8),
            self._make_skill("ok2", confidence=0.7),
            self._make_skill("bad", confidence=0.2),
        ]
        decisions = await pipeline.process(skills)
        count = await pipeline.import_approved(decisions, skills)
        assert count == 2

    async def test_custom_min_confidence(self, procedural):
        pipeline = SkillImportPipeline(procedural, min_confidence=0.95)
        [d] = await pipeline.process([self._make_skill(confidence=0.9)])
        assert d.approved is False


# ===========================================================================
# AdapterRegistry
# ===========================================================================

class TestAdapterRegistry:
    def _make_tool_def(self, name="my_tool"):
        from loom.core.harness.registry import ToolDefinition

        async def _fn(call: ToolCall) -> ToolResult:
            return ToolResult(call_id=call.id, tool_name=name, success=True, output="ok")

        return ToolDefinition(
            name=name,
            description="A test tool",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object", "properties": {}},
            executor=_fn,
        )

    def test_register_and_get(self):
        reg = AdapterRegistry()
        td = self._make_tool_def()
        reg.register(td)
        assert reg.get("my_tool") is td

    def test_get_unknown_returns_none(self):
        assert AdapterRegistry().get("ghost") is None

    def test_all_returns_all_registered(self):
        reg = AdapterRegistry()
        reg.register(self._make_tool_def("a"))
        reg.register(self._make_tool_def("b"))
        assert len(reg.all()) == 2

    def test_install_into_copies_to_registry(self):
        adapter_reg = AdapterRegistry()
        adapter_reg.register(self._make_tool_def("tool_x"))

        tool_reg = ToolRegistry()
        count = adapter_reg.install_into(tool_reg)
        assert count == 1
        assert tool_reg.get("tool_x") is not None

    def test_decorator_uses_function_name(self):
        reg = AdapterRegistry()

        @reg.tool()
        async def my_cool_tool(call: ToolCall) -> ToolResult: ...

        assert reg.get("my_cool_tool") is not None

    def test_decorator_uses_docstring(self):
        reg = AdapterRegistry()

        @reg.tool()
        async def documented_tool(call: ToolCall) -> ToolResult:
            """This is the docstring."""

        td = reg.get("documented_tool")
        assert "docstring" in td.description

    def test_decorator_respects_custom_description(self):
        reg = AdapterRegistry()

        @reg.tool(description="custom desc")
        async def custom_tool(call: ToolCall) -> ToolResult:
            """Should not be used."""

        assert reg.get("custom_tool").description == "custom desc"

    def test_decorator_trust_level_string(self):
        reg = AdapterRegistry()

        @reg.tool(trust_level="guarded")
        async def guarded_tool(call: ToolCall) -> ToolResult: ...

        assert reg.get("guarded_tool").trust_level == TrustLevel.GUARDED

    def test_decorator_trust_level_enum(self):
        reg = AdapterRegistry()

        @reg.tool(trust_level=TrustLevel.CRITICAL)
        async def critical_tool(call: ToolCall) -> ToolResult: ...

        assert reg.get("critical_tool").trust_level == TrustLevel.CRITICAL

    def test_decorator_registers_into_registry(self):
        reg = AdapterRegistry()

        @reg.tool()
        async def auto_registered(call: ToolCall) -> ToolResult: ...

        assert "auto_registered" in [t.name for t in reg.all()]

    def test_from_lens_result_builds_registry(self):
        result = LensResult(
            source="test",
            platform_adapters=[
                {"name": "web_search", "description": "Search the web", "trust_level": "safe"},
                {"name": "send_mail", "description": "Send email", "trust_level": "guarded"},
            ],
        )
        reg = AdapterRegistry.from_lens_result(result)
        assert reg.get("web_search") is not None
        assert reg.get("web_search").trust_level == TrustLevel.SAFE
        assert reg.get("send_mail").trust_level == TrustLevel.GUARDED

    async def test_placeholder_executor_returns_error(self):
        result = LensResult(
            source="test",
            platform_adapters=[{"name": "ghost_tool"}],
        )
        reg = AdapterRegistry.from_lens_result(result)
        tool = reg.get("ghost_tool")
        res = await tool.executor(_tool_call("ghost_tool"))
        assert res.success is False
        assert "ghost_tool" in res.error


# ===========================================================================
# RelationalMemory
# ===========================================================================

class TestRelationalMemory:
    def _entry(self, subject="user", predicate="prefers", obj="concise responses", **kw):
        return RelationalEntry(subject=subject, predicate=predicate, object=obj, **kw)

    async def test_upsert_inserts_new_entry(self, relational):
        await relational.upsert(self._entry())
        got = await relational.get("user", "prefers")
        assert got is not None
        assert got.object == "concise responses"

    async def test_get_returns_none_for_missing(self, relational):
        assert await relational.get("user", "nonexistent") is None

    async def test_upsert_updates_existing(self, relational):
        await relational.upsert(self._entry(obj="verbose"))
        await relational.upsert(self._entry(obj="concise"))
        got = await relational.get("user", "prefers")
        assert got.object == "concise"

    async def test_query_by_subject(self, relational):
        await relational.upsert(self._entry(predicate="prefers", obj="A"))
        await relational.upsert(self._entry(predicate="avoids", obj="B"))
        results = await relational.query(subject="user")
        assert len(results) == 2
        predicates = {r.predicate for r in results}
        assert predicates == {"prefers", "avoids"}

    async def test_query_by_predicate(self, relational):
        await relational.upsert(RelationalEntry(subject="user", predicate="uses", object="sqlite"))
        await relational.upsert(RelationalEntry(subject="project", predicate="uses", object="python"))
        results = await relational.query(predicate="uses")
        assert len(results) == 2

    async def test_query_by_both(self, relational):
        await relational.upsert(self._entry())
        results = await relational.query(subject="user", predicate="prefers")
        assert len(results) == 1
        assert results[0].object == "concise responses"

    async def test_query_all_no_filter(self, relational):
        await relational.upsert(RelationalEntry(subject="a", predicate="p1", object="x"))
        await relational.upsert(RelationalEntry(subject="b", predicate="p2", object="y"))
        results = await relational.query()
        assert len(results) == 2

    async def test_query_empty_returns_empty_list(self, relational):
        assert await relational.query(subject="nobody") == []

    async def test_delete_removes_entry(self, relational):
        await relational.upsert(self._entry())
        deleted = await relational.delete("user", "prefers")
        assert deleted is True
        assert await relational.get("user", "prefers") is None

    async def test_delete_returns_false_when_not_found(self, relational):
        assert await relational.delete("nobody", "nothing") is False

    async def test_source_and_confidence_preserved(self, relational):
        entry = self._entry(confidence=0.75)
        entry.source = "manual"
        await relational.upsert(entry)
        got = await relational.get("user", "prefers")
        assert got.confidence == pytest.approx(0.75)
        assert got.source == "manual"
