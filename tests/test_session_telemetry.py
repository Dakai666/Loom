"""Issue #543 PR-2 — session._telemetry_record_tool unit test.

覆蓋目標（先前確認真 0 覆蓋）：
    - session.py line 3458 的 _telemetry_record_tool method
    - 這是 hot-path 工具記錄點，dispatch 順序/平行都走它
    - 一旦有 bug，整個觀測面失真

設計：建最小 session 殼（SimpleNamespace），注入 _telemetry mock，
bound method 取出來呼叫。不需建構完整 LoomSession 實例。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from loom.core.harness.middleware import ToolResult
from loom.core.session import LoomSession


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

class FakeToolCallDim:
    """模擬 ToolCallDimension 的 record() 與內部計數。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def record(
        self,
        tool_name: str,
        *,
        success: bool,
        duration_ms: float,
        error_msg: str | None,
    ) -> None:
        self.calls.append({
            "tool_name": tool_name,
            "success": success,
            "duration_ms": duration_ms,
            "error_msg": error_msg,
        })


class FakeTelemetry:
    """模擬 AgentTelemetryTracker 的 get() 與 mark_dirty()。"""

    def __init__(self, tool_call_dim: FakeToolCallDim | None = None) -> None:
        self._dims: dict[str, Any] = {}
        if tool_call_dim is not None:
            self._dims["tool_call"] = tool_call_dim
        self.mark_dirty_count = 0

    def get(self, name: str) -> Any:
        return self._dims.get(name)

    def mark_dirty(self) -> None:
        self.mark_dirty_count += 1


def _make_session_shell(telemetry: Any) -> SimpleNamespace:
    """最小 session 殼——只放 _telemetry 屬性。bound method 從 class 取出。"""
    return SimpleNamespace(_telemetry=telemetry)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTelemetryRecordToolTelemetryDisabled:
    """self._telemetry is None → 早退，不應做任何 record。"""

    def test_none_telemetry_early_exits(self):
        session_shell = _make_session_shell(None)
        result = ToolResult(call_id="c1", tool_name="read_file", success=True)

        # bound method 從 class 取出
        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result, duration_ms=10.0,
        )

        # 無 _telemetry → 不應有 side effect，無 exception


class TestTelemetryRecordToolMissingDimension:
    """tool_call dim 缺失 → 早退（不應 record 也不應 mark_dirty）。"""

    def test_missing_tool_call_dim_early_exits(self):
        telemetry = FakeTelemetry(tool_call_dim=None)  # 故意不註冊 tool_call
        session_shell = _make_session_shell(telemetry)
        result = ToolResult(call_id="c1", tool_name="read_file", success=True)

        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result, duration_ms=10.0,
        )

        assert telemetry.mark_dirty_count == 0


class TestTelemetryRecordToolSuccessPath:
    """正常 record：成功 result 應被記錄 success=True、error_msg=None。"""

    def test_success_result_records_correctly(self):
        dim = FakeToolCallDim()
        telemetry = FakeTelemetry(tool_call_dim=dim)
        session_shell = _make_session_shell(telemetry)
        result = ToolResult(call_id="c1", tool_name="read_file", success=True, output="ok")

        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result, duration_ms=42.0,
        )

        assert len(dim.calls) == 1
        assert dim.calls[0] == {
            "tool_name": "read_file",
            "success": True,
            "duration_ms": 42.0,
            "error_msg": None,
        }
        # mark_dirty 應被觸發一次
        assert telemetry.mark_dirty_count == 1

    def test_success_with_stray_error_suppresses_error_msg(self):
        """success=True 但 result.error 意外有值時，error_msg 仍應為 None
        （production 是 `result.error if not result.success else None`）。"""
        dim = FakeToolCallDim()
        telemetry = FakeTelemetry(tool_call_dim=dim)
        session_shell = _make_session_shell(telemetry)
        result = ToolResult(
            call_id="c1", tool_name="read_file", success=True, error="stray",
        )

        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result, duration_ms=5.0,
        )

        assert dim.calls[0]["success"] is True
        assert dim.calls[0]["error_msg"] is None

    def test_mark_dirty_fires_after_record(self):
        dim = FakeToolCallDim()
        telemetry = FakeTelemetry(tool_call_dim=dim)
        session_shell = _make_session_shell(telemetry)
        result = ToolResult(call_id="c1", tool_name="read_file", success=True)

        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result, duration_ms=5.0,
        )

        # mark_dirty 必須在 record 之後觸發
        assert telemetry.mark_dirty_count == 1


class TestTelemetryRecordToolFailurePath:
    """失敗 result：應記錄 success=False、error_msg 帶 result.error。"""

    def test_failure_result_records_error_msg(self):
        dim = FakeToolCallDim()
        telemetry = FakeTelemetry(tool_call_dim=dim)
        session_shell = _make_session_shell(telemetry)
        result = ToolResult(
            call_id="c1", tool_name="read_file", success=False, error="File not found",
        )

        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result, duration_ms=10.0,
        )

        assert len(dim.calls) == 1
        assert dim.calls[0]["success"] is False
        assert dim.calls[0]["error_msg"] == "File not found"
        assert telemetry.mark_dirty_count == 1


class TestTelemetryRecordToolMultipleCalls:
    """連續多次呼叫：每次都記錄、不互相覆蓋。"""

    def test_consecutive_calls_each_recorded(self):
        dim = FakeToolCallDim()
        telemetry = FakeTelemetry(tool_call_dim=dim)
        session_shell = _make_session_shell(telemetry)

        # 第一次：read_file 成功
        result_a = ToolResult(call_id="c1", tool_name="read_file", success=True)
        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result_a, duration_ms=10.0,
        )
        # 第二次：write_file 失敗
        result_b = ToolResult(
            call_id="c2", tool_name="write_file", success=False, error="permission_denied",
        )
        LoomSession._telemetry_record_tool(
            session_shell, "write_file", result=result_b, duration_ms=20.0,
        )
        # 第三次：run_bash 成功
        result_c = ToolResult(call_id="c3", tool_name="run_bash", success=True)
        LoomSession._telemetry_record_tool(
            session_shell, "run_bash", result=result_c, duration_ms=100.0,
        )

        assert len(dim.calls) == 3
        assert dim.calls[0]["tool_name"] == "read_file"
        assert dim.calls[1]["tool_name"] == "write_file"
        assert dim.calls[1]["error_msg"] == "permission_denied"
        assert dim.calls[2]["tool_name"] == "run_bash"
        # mark_dirty 觸發 3 次
        assert telemetry.mark_dirty_count == 3


class TestTelemetryRecordToolDurationPassthrough:
    """duration_ms 應原封不動傳給 dim.record()。"""

    @pytest.mark.parametrize("duration", [0.0, 0.5, 100.0, 9999.99])
    def test_duration_passes_through(self, duration):
        dim = FakeToolCallDim()
        telemetry = FakeTelemetry(tool_call_dim=dim)
        session_shell = _make_session_shell(telemetry)
        result = ToolResult(call_id="c1", tool_name="read_file", success=True)

        LoomSession._telemetry_record_tool(
            session_shell, "read_file", result=result, duration_ms=duration,
        )

        assert dim.calls[0]["duration_ms"] == duration
