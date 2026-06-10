"""Issue #543 PR-3 — session._load_plugins 安全 gate test.

覆蓋目標（先前確認真 0 覆蓋）：
    - session.py line 3051 的 _load_plugins async method
    - 涉及 plugin approval flow + Confirm.ask 互動
    - GUARDED trust — 任意 Python code 經 importlib.util 載入執行
    - 未測試 = P0 安全邊界看不見

設計：SimpleNamespace session 殼 + tmp_path 控 plugin_dir 與 workspace +
monkeypatch get_triple/upsert_triple + Confirm + loom._get_default_registry 等。
不建構完整 LoomSession。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from loom.core.session import LoomSession


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class FakeSemantic:
    """最小 SemanticMemory 殼——只給 _load_plugins 需要的 attributes。"""

    def __init__(self) -> None:
        self.upserts: list[tuple[str, str, str]] = []


class FakeMemory:
    def __init__(self) -> None:
        self.semantic = FakeSemantic()


class FakeInstallCounter:
    """模擬 _AdapterRegistry / _PluginRegistry 的 install_into——只計數。"""

    def __init__(self) -> None:
        self.install_into_count = 0

    def install_into(self, target: Any) -> int:
        self.install_into_count += 1
        return 1  # 假設每次裝一個


_UNSET = object()


def _make_session_shell(
    workspace: Path,
    memory: FakeMemory | None = _UNSET,
) -> SimpleNamespace:
    """最小 session 殼：_memory、workspace、registry 是 _load_plugins 唯一讀的屬性。
    _get_default_registry() / _get_default_plugin_registry() 由 patch_registries fixture mock。

    memory 預設值用 _UNSET sentinel：不傳時每次 new 一個 FakeMemory（避免共享可變預設值），
    顯式傳 None 時保留 None（測 _memory is None 短路）。"""
    if memory is _UNSET:
        memory = FakeMemory()
    return SimpleNamespace(
        _memory=memory,
        registry=SimpleNamespace(),  # install_into 會被傳入，target 不需有內容
        workspace=workspace,
    )


def _write_plugin(path: Path, body: str) -> None:
    """寫一個合法 Python plugin 檔（內容是無害的 stub）。"""
    path.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def patch_path(monkeypatch: pytest.MonkeyPatch):
    """把 Path('~/.loom/plugins') 改指到傳入的 tmp 目錄。

    回傳 function(plugin_dir: Path) 讓各 test 注入自己的 tmp 目錄。
    """
    original_Path = Path

    def _apply(plugin_dir: Path) -> None:
        def _patched_path(p="~/.loom/plugins", *args, **kwargs):
            if str(p) == "~/.loom/plugins":
                return plugin_dir
            return original_Path(p, *args, **kwargs)
        monkeypatch.setattr("loom.core.session.Path", _patched_path)

    return _apply


@pytest.fixture
def patch_registries(monkeypatch: pytest.MonkeyPatch):
    """Mock loom._get_default_registry() 與 _get_default_plugin_registry()。
    讓每次 install_into 計數，不實際安裝工具。

    回傳 dict {'tool': counter, 'plugin': counter}。
    """
    counters = {
        "tool": FakeInstallCounter(),
        "plugin": FakeInstallCounter(),
    }

    def fake_get_default_registry():
        return counters["tool"]

    class _FakePluginRegistry:
        """_PluginRegistry.install_into 回傳 dict[name, count]，per-name summary。"""
        def install_into(self, session):
            counters["plugin"].install_into_count += 1
            return {}  # empty dict → plugin_count = 0
    def fake_get_default_plugin_registry():
        return _FakePluginRegistry()

    monkeypatch.setattr("loom._get_default_registry", fake_get_default_registry)
    monkeypatch.setattr("loom._get_default_plugin_registry", fake_get_default_plugin_registry)

    return counters


@pytest.fixture
def patch_relational(monkeypatch: pytest.MonkeyPatch):
    """Mock get_triple 與 upsert_triple。

    回傳 dict 含:
      - 'triples': dict[(subject, predicate), entry|None] 預設回傳 None
      - 'upserts': list of (subject, predicate, object)
      - 'set_triple(subject, predicate, entry)': 預設 get_triple 行為
    """
    state: dict[str, Any] = {
        "triples": {},
        "upserts": [],
    }

    async def fake_get_triple(semantic, subject, predicate):
        return state["triples"].get((subject, predicate))

    async def fake_upsert_triple(semantic, entry):
        state["upserts"].append((entry.subject, entry.predicate, entry.object))
        state["triples"][(entry.subject, entry.predicate)] = entry

    monkeypatch.setattr("loom.core.memory.relational_bridge.get_triple", fake_get_triple)
    monkeypatch.setattr("loom.core.memory.relational_bridge.upsert_triple", fake_upsert_triple)

    return state


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestLoadPluginsNoCandidates:
    """plugin 目錄空 + workspace 沒 loom_tools.py → 安靜 return。"""

    @pytest.mark.asyncio
    async def test_empty_plugin_dir_skips_silently(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "empty_plugins"
        plugin_dir.mkdir()
        patch_path(plugin_dir)

        session_shell = _make_session_shell(workspace)
        await LoomSession._load_plugins(session_shell)

        assert patch_registries["tool"].install_into_count == 0
        assert len(patch_relational["upserts"]) == 0

    @pytest.mark.asyncio
    async def test_workspace_without_loom_tools_skips_silently(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "empty_plugins"
        plugin_dir.mkdir()
        patch_path(plugin_dir)

        session_shell = _make_session_shell(workspace)
        await LoomSession._load_plugins(session_shell)

        assert patch_registries["tool"].install_into_count == 0


class TestLoadPluginsApprovalFlow:
    """無 approved triple 時走 Confirm.ask 流程。"""

    @pytest.mark.asyncio
    async def test_first_time_plugin_prompts_user(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational, monkeypatch,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        _write_plugin(plugin_dir / "my_plugin.py", "print('hello')")
        patch_path(plugin_dir)

        # patch_relational['triples'] 預設空 → get_triple 回 None → 走 prompt
        # Confirm 答 yes
        monkeypatch.setattr("rich.prompt.Confirm", MagicMock(ask=MagicMock(return_value=True)))

        session_shell = _make_session_shell(workspace)
        await LoomSession._load_plugins(session_shell)

        # plugin 載入成功 → install_into 觸發
        assert patch_registries["tool"].install_into_count == 1
        # approval 寫入
        assert len(patch_relational["upserts"]) == 1
        upsert = patch_relational["upserts"][0]
        assert upsert[0] == "plugin:my_plugin"
        assert upsert[1] == "approved"
        assert upsert[2] == "true"

    @pytest.mark.asyncio
    async def test_user_rejects_skips_plugin(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational, monkeypatch,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        _write_plugin(plugin_dir / "evil_plugin.py", "raise Exception('pwn')")
        patch_path(plugin_dir)

        # Confirm 答 NO
        monkeypatch.setattr("rich.prompt.Confirm", MagicMock(ask=MagicMock(return_value=False)))

        session_shell = _make_session_shell(workspace)
        await LoomSession._load_plugins(session_shell)

        # plugin 被拒 → install_into 不觸發
        assert patch_registries["tool"].install_into_count == 0
        # approval 不寫入
        assert len(patch_relational["upserts"]) == 0


class TestLoadPluginsAlreadyApproved:
    """已 approved 的 plugin 跳過 Confirm.ask 直接 load。"""

    @pytest.mark.asyncio
    async def test_already_approved_skips_prompt(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational, monkeypatch,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        _write_plugin(plugin_dir / "trusted_plugin.py", "print('hi')")
        patch_path(plugin_dir)

        # 預先標記為已 approved
        patch_relational["triples"][("plugin:trusted_plugin", "approved")] = SimpleNamespace(object="true")
        # Confirm 若被呼叫就 fail
        confirm_mock = MagicMock(ask=MagicMock(side_effect=AssertionError("Confirm.ask should NOT be called for approved plugin")))
        monkeypatch.setattr("rich.prompt.Confirm", confirm_mock)

        session_shell = _make_session_shell(workspace)
        await LoomSession._load_plugins(session_shell)

        # plugin 載入
        assert patch_registries["tool"].install_into_count == 1
        # 不應寫入新 approval（既有的）
        assert len(patch_relational["upserts"]) == 0


class TestLoadPluginsErrorHandling:
    """plugin 載入失敗（exec_module 拋錯）→ 印錯、continue、不 crash。"""

    @pytest.mark.asyncio
    async def test_broken_plugin_does_not_crash_session(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational, monkeypatch,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        _write_plugin(plugin_dir / "broken.py", "raise RuntimeError('intentional')")
        patch_path(plugin_dir)

        # 預先標記為 approved 以跳過 prompt
        patch_relational["triples"][("plugin:broken", "approved")] = SimpleNamespace(object="true")
        monkeypatch.setattr("rich.prompt.Confirm", MagicMock(ask=MagicMock(return_value=True)))

        session_shell = _make_session_shell(workspace)
        # 不應 raise
        await LoomSession._load_plugins(session_shell)

        # plugin 載入失敗 → install_into 沒被觸發
        assert patch_registries["tool"].install_into_count == 0

    @pytest.mark.asyncio
    async def test_mixed_plugins_one_fails_other_loads(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational, monkeypatch,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        _write_plugin(plugin_dir / "aaa_broken.py", "raise RuntimeError('fail')")
        _write_plugin(plugin_dir / "zzz_good.py", "print('ok')")
        patch_path(plugin_dir)

        # 兩個都預先 approved
        patch_relational["triples"][("plugin:aaa_broken", "approved")] = SimpleNamespace(object="true")
        patch_relational["triples"][("plugin:zzz_good", "approved")] = SimpleNamespace(object="true")
        monkeypatch.setattr("rich.prompt.Confirm", MagicMock(ask=MagicMock(return_value=True)))

        session_shell = _make_session_shell(workspace)
        await LoomSession._load_plugins(session_shell)

        # aaa 失敗 → 0 次；zzz 成功 → 1 次。總計 1 次。
        assert patch_registries["tool"].install_into_count == 1


class TestLoadPluginsMemoryDisabled:
    """self._memory is None → Confirm 仍走，approval 不持久化（這是 P0 觀察點）。"""

    @pytest.mark.asyncio
    async def test_no_memory_still_prompts_but_does_not_persist(
        self, tmp_path: Path, patch_path, patch_registries, patch_relational, monkeypatch,
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        _write_plugin(plugin_dir / "ephemeral.py", "print('x')")
        patch_path(plugin_dir)

        # get_triple/upsert_triple 應不呼叫（_memory 短路），
        # 但 patch 已裝上——它們不會被觸發因為 _load_plugins 內 if self._memory is not None 短路
        monkeypatch.setattr("rich.prompt.Confirm", MagicMock(ask=MagicMock(return_value=True)))

        session_shell = _make_session_shell(workspace, memory=None)
        # 不應 crash
        await LoomSession._load_plugins(session_shell)

        # plugin 仍被載入（Confirm=True）
        assert patch_registries["tool"].install_into_count == 1
        # 不應有 upsert（_memory is None 短路）
        assert len(patch_relational["upserts"]) == 0
