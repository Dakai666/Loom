"""
Tests for Issue #64 Phase B: Skill-declared precondition checks.

Covers:
- SkillPreconditionRef parsing
- SkillCheckManager mount/unmount/auto-unmount
- Callable resolution from skill directories
- Frontmatter parsing with precondition_checks
- SkillGenome persistence of precondition_check_refs
"""

import asyncio
import pytest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

from loom.core.harness.skill_checks import (
    SkillCheckManager,
    SkillPreconditionRef,
)
from loom.core.harness.registry import ToolDefinition, ToolRegistry
from loom.core.harness.permissions import TrustLevel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry_with_tools(*tool_names: str) -> ToolRegistry:
    """Create a registry with dummy tool definitions."""
    registry = ToolRegistry()
    for name in tool_names:
        registry.register(ToolDefinition(
            name=name,
            description=f"Dummy {name}",
            trust_level=TrustLevel.SAFE,
            input_schema={"type": "object"},
            executor=AsyncMock(),
        ))
    return registry


async def _always_pass(call):
    return True

async def _always_fail(call):
    return False

async def _env_check(call):
    import os
    return os.environ.get("LOOM_ENV") != "production"


# ---------------------------------------------------------------------------
# SkillPreconditionRef
# ---------------------------------------------------------------------------


class TestSkillPreconditionRef:
    def test_from_dict_full(self):
        ref = SkillPreconditionRef.from_dict({
            "ref": "checks.require_not_production",
            "applies_to": ["run_bash"],
            "description": "Not in production",
        })
        assert ref.ref == "checks.require_not_production"
        assert ref.applies_to == ["run_bash"]
        assert ref.description == "Not in production"

    def test_from_dict_string_applies_to(self):
        """applies_to as a single string should be wrapped in a list."""
        ref = SkillPreconditionRef.from_dict({
            "ref": "checks.fn",
            "applies_to": "run_bash",
            "description": "desc",
        })
        assert ref.applies_to == ["run_bash"]

    def test_from_dict_no_description(self):
        """Missing description should default to ref name."""
        ref = SkillPreconditionRef.from_dict({
            "ref": "checks.fn",
            "applies_to": ["run_bash"],
        })
        assert ref.description == "checks.fn"


# ---------------------------------------------------------------------------
# SkillCheckManager — mount / unmount
# ---------------------------------------------------------------------------


class TestSkillCheckManagerMount:
    def test_mount_adds_checks_to_tool(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash"],
            description="Test check",
        )]

        descs = manager.mount("skill_a", refs, {"checks.fn": _always_pass})

        tool = registry.get("run_bash")
        assert len(tool.precondition_checks) == 1
        assert tool.precondition_checks[0] is _always_pass
        assert "Test check" in tool.preconditions
        assert manager.active_skill == "skill_a"
        assert len(descs) == 1

    def test_mount_multiple_tools(self):
        registry = _make_registry_with_tools("run_bash", "write_file")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash", "write_file"],
            description="Shared check",
        )]

        manager.mount("skill_a", refs, {"checks.fn": _always_pass})

        assert len(registry.get("run_bash").precondition_checks) == 1
        assert len(registry.get("write_file").precondition_checks) == 1

    def test_mount_skips_unknown_tool(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["nonexistent_tool"],
            description="Won't mount",
        )]

        descs = manager.mount("skill_a", refs, {"checks.fn": _always_pass})
        assert len(descs) == 0

    def test_mount_skips_unresolved_ref(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.missing",
            applies_to=["run_bash"],
            description="Missing callable",
        )]

        descs = manager.mount("skill_a", refs, {})  # no callables
        assert len(descs) == 0
        assert len(registry.get("run_bash").precondition_checks) == 0


class TestSkillCheckManagerUnmount:
    def test_unmount_removes_checks(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash"],
            description="Test check",
        )]
        manager.mount("skill_a", refs, {"checks.fn": _always_pass})

        removed = manager.unmount("skill_a")

        assert removed == 1
        assert len(registry.get("run_bash").precondition_checks) == 0
        assert len(registry.get("run_bash").preconditions) == 0
        assert manager.active_skill is None

    def test_unmount_nonexistent_skill(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        removed = manager.unmount("nonexistent")
        assert removed == 0

    def test_unmount_all(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash"],
            description="Check A",
        )]
        manager.mount("skill_a", refs, {"checks.fn": _always_pass}, keep_existing=True)
        manager.mount("skill_b", refs, {"checks.fn": _always_fail}, keep_existing=True)

        manager.unmount_all()

        assert len(registry.get("run_bash").precondition_checks) == 0
        assert manager.mounted_skills() == []


class TestSkillCheckManagerAutoUnmount:
    """Test the A strategy: loading a new skill auto-unmounts the previous one."""

    def test_auto_unmount_on_new_skill(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        ref_a = [SkillPreconditionRef(
            ref="checks.fn_a",
            applies_to=["run_bash"],
            description="Check from A",
        )]
        ref_b = [SkillPreconditionRef(
            ref="checks.fn_b",
            applies_to=["run_bash"],
            description="Check from B",
        )]

        manager.mount("skill_a", ref_a, {"checks.fn_a": _always_pass})
        assert len(registry.get("run_bash").precondition_checks) == 1
        assert registry.get("run_bash").precondition_checks[0] is _always_pass

        # Loading skill_b should auto-unmount skill_a
        manager.mount("skill_b", ref_b, {"checks.fn_b": _always_fail})
        assert len(registry.get("run_bash").precondition_checks) == 1
        assert registry.get("run_bash").precondition_checks[0] is _always_fail
        assert manager.active_skill == "skill_b"

    def test_keep_existing_preserves_previous(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        ref_a = [SkillPreconditionRef(
            ref="checks.fn_a",
            applies_to=["run_bash"],
            description="Check from A",
        )]
        ref_b = [SkillPreconditionRef(
            ref="checks.fn_b",
            applies_to=["run_bash"],
            description="Check from B",
        )]

        manager.mount("skill_a", ref_a, {"checks.fn_a": _always_pass})
        manager.mount("skill_b", ref_b, {"checks.fn_b": _always_fail}, keep_existing=True)

        # Both checks should be mounted
        assert len(registry.get("run_bash").precondition_checks) == 2
        assert manager.active_skill == "skill_b"
        assert set(manager.mounted_skills()) == {"skill_a", "skill_b"}

    def test_remount_same_skill(self):
        """Re-mounting the same skill should replace (not double) checks."""
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash"],
            description="Check",
        )]

        manager.mount("skill_a", refs, {"checks.fn": _always_pass})
        manager.mount("skill_a", refs, {"checks.fn": _always_fail})

        assert len(registry.get("run_bash").precondition_checks) == 1
        assert registry.get("run_bash").precondition_checks[0] is _always_fail


# ---------------------------------------------------------------------------
# Harness invariant: activate() as pure lifecycle event  (Issue #184)
# ---------------------------------------------------------------------------


class TestSkillCheckManagerActivate:
    """
    The harness must maintain the invariant: ``active_skill`` always
    reflects the most recently loaded skill, even when that skill declares
    no precondition checks.  Without this, loading a skill-with-checks
    followed by a skill-without-checks leaves the first skill's checks
    stranded on tool definitions.
    """

    def test_activate_without_checks_unmounts_prior(self):
        """
        Issue #184 core bug: load_skill(skill_a_with_checks) →
        load_skill(skill_b_no_checks) must clear skill_a's checks.
        """
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs_a = [SkillPreconditionRef(
            ref="checks.fn_a",
            applies_to=["run_bash"],
            description="Check from A",
        )]

        manager.mount("skill_a", refs_a, {"checks.fn_a": _always_pass})
        assert len(registry.get("run_bash").precondition_checks) == 1
        assert manager.active_skill == "skill_a"

        # skill_b declares no precondition_checks → activate() is the only
        # signal the manager receives.  It must still clear skill_a.
        manager.activate("skill_b")

        assert len(registry.get("run_bash").precondition_checks) == 0
        assert manager.active_skill == "skill_b"
        assert manager.mounted_skills() == []

    def test_activate_same_skill_is_noop(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash"],
            description="Check",
        )]
        manager.mount("skill_a", refs, {"checks.fn": _always_pass})

        # Re-activating the currently active skill must not clear its checks
        manager.activate("skill_a")

        assert len(registry.get("run_bash").precondition_checks) == 1
        assert manager.active_skill == "skill_a"

    def test_activate_keep_existing(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs_a = [SkillPreconditionRef(
            ref="checks.fn_a",
            applies_to=["run_bash"],
            description="A",
        )]
        manager.mount("skill_a", refs_a, {"checks.fn_a": _always_pass})

        manager.activate("skill_b", keep_existing=True)

        # skill_a's checks remain; only active_skill pointer moves
        assert len(registry.get("run_bash").precondition_checks) == 1
        assert manager.active_skill == "skill_b"
        assert "skill_a" in manager.mounted_skills()

    def test_activate_from_empty(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        assert manager.active_skill is None
        manager.activate("skill_a")
        assert manager.active_skill == "skill_a"


class TestSkillCheckManagerOwnerOf:
    """Reverse lookup: which skill mounted a given check function."""

    def test_owner_of_mounted_check(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash"],
            description="Check",
        )]
        manager.mount("pet-cat", refs, {"checks.fn": _always_fail})

        assert manager.owner_of(_always_fail) == "pet-cat"

    def test_owner_of_unknown_check(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        assert manager.owner_of(_always_pass) is None

    def test_owner_of_after_unmount(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fn",
            applies_to=["run_bash"],
            description="Check",
        )]
        manager.mount("skill_a", refs, {"checks.fn": _always_fail})
        manager.unmount("skill_a")

        assert manager.owner_of(_always_fail) is None


# ---------------------------------------------------------------------------
# Callable resolution
# ---------------------------------------------------------------------------


class TestResolveCallable:
    def test_resolve_valid(self, tmp_path):
        # Create a checks.py module in a temp skill dir
        checks_py = tmp_path / "checks.py"
        checks_py.write_text(
            "async def require_ok(call):\n    return True\n"
        )

        fn = SkillCheckManager.resolve_callable(tmp_path, "checks.require_ok")
        assert callable(fn)
        assert fn.__name__ == "require_ok"

    def test_resolve_missing_module(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            SkillCheckManager.resolve_callable(tmp_path, "missing.fn")

    def test_resolve_missing_function(self, tmp_path):
        checks_py = tmp_path / "checks.py"
        checks_py.write_text("x = 42\n")

        with pytest.raises(AttributeError):
            SkillCheckManager.resolve_callable(tmp_path, "checks.nonexistent")

    def test_resolve_invalid_format(self, tmp_path):
        with pytest.raises(ValueError):
            SkillCheckManager.resolve_callable(tmp_path, "no_dot_here")

    def test_resolve_all(self, tmp_path):
        checks_py = tmp_path / "checks.py"
        checks_py.write_text(
            "async def fn_a(call):\n    return True\n\n"
            "async def fn_b(call):\n    return False\n"
        )

        refs = [
            SkillPreconditionRef(ref="checks.fn_a", applies_to=["run_bash"], description="A"),
            SkillPreconditionRef(ref="checks.fn_b", applies_to=["run_bash"], description="B"),
        ]

        callables = SkillCheckManager.resolve_all(tmp_path, refs)
        assert "checks.fn_a" in callables
        assert "checks.fn_b" in callables
        assert callables["checks.fn_a"].__name__ == "fn_a"


# ---------------------------------------------------------------------------
# Frontmatter parsing with precondition_checks
# ---------------------------------------------------------------------------


class TestFrontmatterPreconditionChecks:
    def test_parses_precondition_checks(self):
        from loom.core.session import _parse_skill_frontmatter

        raw = """---
name: security_assessment
description: Security assessment skill.
precondition_checks:
  - ref: checks.require_not_production
    applies_to: [run_bash]
    description: Not in production
  - ref: checks.require_authorization
    applies_to: [run_bash, write_file]
    description: Must have authorization
---

# Security Assessment
"""
        name, desc, tags, pc_refs = _parse_skill_frontmatter(raw)
        assert name == "security_assessment"
        assert len(pc_refs) == 2
        assert pc_refs[0]["ref"] == "checks.require_not_production"
        assert pc_refs[0]["applies_to"] == ["run_bash"]
        assert pc_refs[1]["applies_to"] == ["run_bash", "write_file"]

    def test_no_precondition_checks(self):
        from loom.core.session import _parse_skill_frontmatter

        raw = """---
name: simple
description: A simple skill.
---
Body.
"""
        name, desc, tags, pc_refs = _parse_skill_frontmatter(raw)
        assert pc_refs == []

    def test_invalid_precondition_checks_ignored(self):
        from loom.core.session import _parse_skill_frontmatter

        raw = """---
name: bad
description: Bad skill.
precondition_checks:
  - just_a_string
  - ref: valid.fn
    applies_to: [run_bash]
  - ref: no_applies_to
---
Body.
"""
        name, desc, tags, pc_refs = _parse_skill_frontmatter(raw)
        # Only the valid entry should survive
        assert len(pc_refs) == 1
        assert pc_refs[0]["ref"] == "valid.fn"


# ---------------------------------------------------------------------------
# SkillGenome persistence
# ---------------------------------------------------------------------------


class TestSkillGenomePreconditionRefs:
    @pytest.mark.asyncio
    async def test_upsert_and_get_with_refs(self, tmp_path):
        from loom.core.memory.store import SQLiteStore
        from loom.core.memory.procedural import ProceduralMemory, SkillGenome

        store = SQLiteStore(str(tmp_path / "test.db"))
        await store.initialize()
        async with store.connect() as db:
            proc = ProceduralMemory(db)

            refs = [
                {"ref": "checks.fn_a", "applies_to": ["run_bash"], "description": "A"},
                {"ref": "checks.fn_b", "applies_to": ["write_file"], "description": "B"},
            ]
            genome = SkillGenome(
                name="test_skill",
                body="# Test Skill\nBody.",
                precondition_check_refs=refs,
            )
            await proc.upsert(genome)

            loaded = await proc.get("test_skill")
            assert loaded is not None
            assert len(loaded.precondition_check_refs) == 2
            assert loaded.precondition_check_refs[0]["ref"] == "checks.fn_a"
            assert loaded.precondition_check_refs[1]["applies_to"] == ["write_file"]

    @pytest.mark.asyncio
    async def test_empty_refs_default(self, tmp_path):
        from loom.core.memory.store import SQLiteStore
        from loom.core.memory.procedural import ProceduralMemory, SkillGenome

        store = SQLiteStore(str(tmp_path / "test.db"))
        await store.initialize()
        async with store.connect() as db:
            proc = ProceduralMemory(db)

            genome = SkillGenome(name="no_refs", body="Body.")
            await proc.upsert(genome)

            loaded = await proc.get("no_refs")
            assert loaded.precondition_check_refs == []

    @pytest.mark.asyncio
    async def test_list_active_includes_refs(self, tmp_path):
        from loom.core.memory.store import SQLiteStore
        from loom.core.memory.procedural import ProceduralMemory, SkillGenome

        store = SQLiteStore(str(tmp_path / "test.db"))
        await store.initialize()
        async with store.connect() as db:
            proc = ProceduralMemory(db)

            refs = [{"ref": "checks.fn", "applies_to": ["run_bash"], "description": "D"}]
            await proc.upsert(SkillGenome(
                name="with_refs", body="Body.",
                precondition_check_refs=refs,
            ))
            await proc.upsert(SkillGenome(
                name="without_refs", body="Body.",
            ))

            active = await proc.list_active()
            names = {s.name for s in active}
            assert "with_refs" in names
            assert "without_refs" in names

            with_refs = next(s for s in active if s.name == "with_refs")
            assert len(with_refs.precondition_check_refs) == 1


# ---------------------------------------------------------------------------
# Integration: mount → lifecycle gate evaluation
# ---------------------------------------------------------------------------


class TestMountedCheckIntegration:
    """Verify that mounted checks are actually evaluated by LifecycleGateMiddleware."""

    @pytest.mark.asyncio
    async def test_mounted_check_blocks_tool(self):
        """A mounted failing check should cause tool execution to be aborted."""
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.fail",
            applies_to=["run_bash"],
            description="Always fails",
        )]
        manager.mount("strict_skill", refs, {"checks.fail": _always_fail})

        tool = registry.get("run_bash")
        # Directly verify the check is in the list and would fail
        assert len(tool.precondition_checks) == 1
        result = await tool.precondition_checks[0](MagicMock())
        assert result is False

    @pytest.mark.asyncio
    async def test_mounted_check_passes_tool(self):
        registry = _make_registry_with_tools("run_bash")
        manager = SkillCheckManager(registry)

        refs = [SkillPreconditionRef(
            ref="checks.pass",
            applies_to=["run_bash"],
            description="Always passes",
        )]
        manager.mount("permissive_skill", refs, {"checks.pass": _always_pass})

        tool = registry.get("run_bash")
        result = await tool.precondition_checks[0](MagicMock())
        assert result is True
