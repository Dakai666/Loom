"""
Tests for skill-specific precondition checks (Issue #64 Phase B).

Each skill's checks.py is tested against realistic tool call scenarios.
"""

import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock


def _make_call(**args):
    """Create a mock ToolCall with given args."""
    call = MagicMock()
    call.args = args
    return call


# ---------------------------------------------------------------------------
# loom_engineer checks
# ---------------------------------------------------------------------------


class TestLoomEngineerChecks:

    @pytest.mark.asyncio
    async def test_require_git_repo_in_repo(self):
        """Should pass when inside a git repo (this test runs in one)."""
        from skills.loom_engineer.checks import require_git_repo
        call = _make_call(command="git status")
        assert await require_git_repo(call) is True

    @pytest.mark.asyncio
    async def test_reject_force_push_blocks(self):
        from skills.loom_engineer.checks import reject_force_push

        assert await reject_force_push(_make_call(command="git push --force origin main")) is False
        assert await reject_force_push(_make_call(command="git push -f origin main")) is False
        assert await reject_force_push(_make_call(command="git push --force-with-lease")) is False

    @pytest.mark.asyncio
    async def test_reject_force_push_allows_normal(self):
        from skills.loom_engineer.checks import reject_force_push

        assert await reject_force_push(_make_call(command="git push origin main")) is True
        assert await reject_force_push(_make_call(command="git status")) is True
        assert await reject_force_push(_make_call(command="pytest tests/")) is True


# ---------------------------------------------------------------------------
# systematic_code_analyst checks
# ---------------------------------------------------------------------------


class TestSystematicCodeAnalystChecks:

    @pytest.mark.asyncio
    async def test_reject_write_always_blocks(self):
        """Analysis skill is read-only — all writes must be blocked."""
        from skills.systematic_code_analyst.checks import reject_write_operations

        assert await reject_write_operations(_make_call(path="/any/file.py")) is False
        assert await reject_write_operations(_make_call(path="README.md")) is False
        assert await reject_write_operations(_make_call()) is False


# ---------------------------------------------------------------------------
# meta-skill-engineer checks
# ---------------------------------------------------------------------------


class TestMetaSkillEngineerChecks:

    @pytest.mark.asyncio
    async def test_require_skills_dir_allows(self):
        """Paths containing /skills/ should pass."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "meta_checks",
            str(Path(__file__).parents[1] / "skills" / "meta-skill-engineer" / "checks.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert await mod.require_skills_dir_target(
            _make_call(path="skills/my-skill/SKILL.md")
        ) is True
        assert await mod.require_skills_dir_target(
            _make_call(path="/home/user/project/skills/test/test-01.md")
        ) is True

    @pytest.mark.asyncio
    async def test_require_skills_dir_blocks(self):
        """Paths outside skills/ should fail."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "meta_checks",
            str(Path(__file__).parents[1] / "skills" / "meta-skill-engineer" / "checks.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert await mod.require_skills_dir_target(
            _make_call(path="loom/core/session.py")
        ) is False
        assert await mod.require_skills_dir_target(
            _make_call(path="/etc/passwd")
        ) is False


# ---------------------------------------------------------------------------
# security_assessment checks
# ---------------------------------------------------------------------------


class TestSecurityAssessmentChecks:

    @pytest.mark.asyncio
    async def test_reject_destructive_blocks(self):
        from skills.security_assessment.checks import reject_destructive_commands

        assert await reject_destructive_commands(_make_call(command="rm -rf /")) is False
        assert await reject_destructive_commands(_make_call(command='sqlite3 db "DROP TABLE users"')) is False
        assert await reject_destructive_commands(_make_call(command="dd if=/dev/zero of=/dev/sda")) is False

    @pytest.mark.asyncio
    async def test_reject_destructive_allows_safe(self):
        from skills.security_assessment.checks import reject_destructive_commands

        assert await reject_destructive_commands(_make_call(command="nmap -sV target.com")) is True
        assert await reject_destructive_commands(_make_call(command="semgrep --config=auto .")) is True
        assert await reject_destructive_commands(_make_call(command="ls -la")) is True

    @pytest.mark.asyncio
    async def test_reject_production_env(self):
        from skills.security_assessment.checks import reject_production_env

        # Should pass in test environment
        call = _make_call(command="nmap localhost")
        original = os.environ.get("LOOM_ENV")
        try:
            os.environ.pop("LOOM_ENV", None)
            assert await reject_production_env(call) is True

            os.environ["LOOM_ENV"] = "production"
            assert await reject_production_env(call) is False

            os.environ["LOOM_ENV"] = "development"
            assert await reject_production_env(call) is True
        finally:
            if original is not None:
                os.environ["LOOM_ENV"] = original
            else:
                os.environ.pop("LOOM_ENV", None)


# ---------------------------------------------------------------------------
# memory_hygiene checks
# ---------------------------------------------------------------------------


class TestMemoryHygieneChecks:

    @pytest.mark.asyncio
    async def test_reject_direct_db_mutation_blocks(self):
        from skills.memory_hygiene.checks import reject_direct_db_mutation

        assert await reject_direct_db_mutation(
            _make_call(command='sqlite3 ~/.loom/memory.db "DELETE FROM semantic_entries"')
        ) is False
        assert await reject_direct_db_mutation(
            _make_call(command='sqlite3 memory.db "DROP TABLE skill_genomes"')
        ) is False
        assert await reject_direct_db_mutation(
            _make_call(command='sqlite3 memory.db "INSERT INTO audit_log VALUES (...)"')
        ) is False

    @pytest.mark.asyncio
    async def test_reject_direct_db_mutation_allows_reads(self):
        from skills.memory_hygiene.checks import reject_direct_db_mutation

        assert await reject_direct_db_mutation(
            _make_call(command='sqlite3 memory.db ".schema"')
        ) is True
        assert await reject_direct_db_mutation(
            _make_call(command="ls -la ~/.loom/")
        ) is True

    @pytest.mark.asyncio
    async def test_require_memory_backup_allows_readonly(self):
        from skills.memory_hygiene.checks import require_memory_backup

        # Read-only commands should always pass
        assert await require_memory_backup(
            _make_call(command="ls -la ~/.loom/")
        ) is True
        assert await require_memory_backup(
            _make_call(command="du -sh ~/.loom/memory.db")
        ) is True

    @pytest.mark.asyncio
    async def test_require_memory_backup_blocks_without_backup(self, tmp_path, monkeypatch):
        from skills.memory_hygiene.checks import require_memory_backup

        # Point HOME to tmp_path so no backup exists
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".loom").mkdir()

        assert await require_memory_backup(
            _make_call(command="python cleanup_script.py")
        ) is False

    @pytest.mark.asyncio
    async def test_require_memory_backup_passes_with_backup(self, tmp_path, monkeypatch):
        from skills.memory_hygiene.checks import require_memory_backup

        monkeypatch.setenv("HOME", str(tmp_path))
        loom_dir = tmp_path / ".loom"
        loom_dir.mkdir()
        (loom_dir / "memory.db.bak").write_text("backup")

        assert await require_memory_backup(
            _make_call(command="python cleanup_script.py")
        ) is True
