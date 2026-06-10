"""Issue #543 PR-1 — 3 個 artifact extractor 的 unit test.

覆蓋目標（先前確認真 0 覆蓋）：
    - _extract_write_file_artifact
    - _extract_image_generate_artifact
    - _extract_tts_generate_artifact

注：_extract_pursuit_write_artifact 已有 tests/test_pursuit.py 覆蓋，不在本 PR 範圍。
   extract_artifact_info（dispatcher）已有 test_ledger_deferred_events.py 覆蓋。

每個 extractor 都是純函式（除了 hashlib/_json 內部使用），
不需 mock、不需 session、不需 fixture——直接用 ToolCall + ToolResult 即可。
"""

from __future__ import annotations

import hashlib
import json

import pytest

from loom.core.harness.middleware import (
    ToolCall,
    ToolResult,
    _extract_image_generate_artifact,
    _extract_tts_generate_artifact,
    _extract_write_file_artifact,
)
from loom.core.harness.permissions import TrustLevel


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _tc(name: str, args: dict | None = None) -> ToolCall:
    """Minimal ToolCall — extractor 內部不讀 metadata 以外的欄位。"""
    return ToolCall(
        tool_name=name,
        args=args or {},
        trust_level=TrustLevel.SAFE,
        session_id="test",
    )


def _tr(
    name: str,
    *,
    success: bool = True,
    output: str | None = None,
    error: str | None = None,
    metadata: dict | None = None,
) -> ToolResult:
    return ToolResult(
        call_id="c1",
        tool_name=name,
        success=success,
        output=output,
        error=error,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# _extract_write_file_artifact
# ---------------------------------------------------------------------------

class TestExtractWriteFileArtifact:
    """write_file 產物萃取：digest 與 size_bytes 由 content 計算；
    location 從 result.metadata['_resolved_path'] 取得，fallback 到 call.args['path']。"""

    def test_normal_content_digest_and_size(self):
        content = "hello, world"
        encoded = content.encode("utf-8")
        call = _tc("write_file", {"path": "out.txt", "content": content})
        result = _tr("write_file", success=True, metadata={"_resolved_path": "/abs/out.txt"})

        artifact = _extract_write_file_artifact(call, result)

        assert artifact is not None
        assert artifact["artifact_type"] == "text_file"
        assert artifact["size_bytes"] == len(encoded)
        assert artifact["digest"] == "sha256:" + hashlib.sha256(encoded).hexdigest()
        assert artifact["location"] == "/abs/out.txt"

    def test_empty_content_still_hashes(self):
        """Issue #543 edge: 空字串也要有 digest，不能因 len=0 早退。"""
        call = _tc("write_file", {"path": "out.txt", "content": ""})
        result = _tr("write_file", success=True, metadata={"_resolved_path": "/abs/out.txt"})

        artifact = _extract_write_file_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0
        # 空字串的 sha256 是 well-known constant
        assert artifact["digest"] == "sha256:" + hashlib.sha256(b"").hexdigest()

    def test_none_content_does_not_crash(self):
        """extract 內部用 `or ""` 處理 None，應不 crash。"""
        call = _tc("write_file", {"path": "out.txt", "content": None})
        result = _tr("write_file", success=True, metadata={"_resolved_path": "/abs/out.txt"})

        artifact = _extract_write_file_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0

    def test_missing_content_arg_uses_empty_string(self):
        """call.args 沒給 content 時，應 fallback 到空字串。"""
        call = _tc("write_file", {"path": "out.txt"})
        result = _tr("write_file", success=True, metadata={"_resolved_path": "/abs/out.txt"})

        artifact = _extract_write_file_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0

    def test_no_resolved_path_falls_back_to_args_path(self):
        """result.metadata 沒有 _resolved_path 時，location 用 call.args['path']。"""
        call = _tc("write_file", {"path": "rel/out.txt", "content": "x"})
        result = _tr("write_file", success=True, metadata=None)

        artifact = _extract_write_file_artifact(call, result)

        assert artifact is not None
        assert artifact["location"] == "rel/out.txt"

    def test_no_resolved_path_no_args_path_location_is_none(self):
        """兩個都缺，location 應為 None（不是 crash）。"""
        call = _tc("write_file", {"content": "x"})
        result = _tr("write_file", success=True, metadata=None)

        artifact = _extract_write_file_artifact(call, result)

        assert artifact is not None
        assert artifact["location"] is None

    def test_unicode_content_utf8_encoded(self):
        """中文/Emoji content 應正確 UTF-8 編碼，size_bytes 反映 byte 而非 char。"""
        content = "你好 🌸"
        expected_size = len(content.encode("utf-8"))  # 中文字 6 bytes + emoji 4 bytes + space 1 = 11
        call = _tc("write_file", {"path": "out.txt", "content": content})
        result = _tr("write_file", success=True, metadata={"_resolved_path": "/abs/out.txt"})

        artifact = _extract_write_file_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == expected_size
        # 確保不是用 len(content)（char count）算的
        assert artifact["size_bytes"] != len(content)


# ---------------------------------------------------------------------------
# _extract_image_generate_artifact
# ---------------------------------------------------------------------------

class TestExtractImageGenerateArtifact:
    """image_generate / openai__text_to_image 產物萃取：從 result.output JSON 解析
    path/bytes/model 計算 digest。"""

    def test_normal_json_output(self):
        result_output = json.dumps({"path": "/img/foo.png", "bytes": 12345, "model": "dall-e-3"})
        call = _tc("image_generate", {"prompt": "a cat"})
        result = _tr("image_generate", success=True, output=result_output)

        artifact = _extract_image_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["artifact_type"] == "image"
        assert artifact["size_bytes"] == 12345
        assert artifact["location"] == "/img/foo.png"
        # digest 應由 path|bytes|model 計算
        expected_digest_input = b"/img/foo.png|12345|dall-e-3"
        assert artifact["digest"] == "sha256:" + hashlib.sha256(expected_digest_input).hexdigest()

    def test_zero_bytes_does_not_crash(self):
        """bytes=0 是合法情境（透通 placeholder），不應 crash。"""
        result_output = json.dumps({"path": "/img/foo.png", "bytes": 0, "model": "m"})
        call = _tc("image_generate", {})
        result = _tr("image_generate", success=True, output=result_output)

        artifact = _extract_image_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0

    def test_malformed_json_falls_back_to_empty_payload(self):
        """output 不是 JSON 不應 crash——payload={} 然後 size_bytes=0, location=None。"""
        call = _tc("image_generate", {})
        result = _tr("image_generate", success=True, output="not json {{{")

        artifact = _extract_image_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0
        assert artifact["location"] is None

    def test_none_output_does_not_crash(self):
        """result.output 為 None 不應 crash。"""
        call = _tc("image_generate", {})
        result = _tr("image_generate", success=True, output=None)

        artifact = _extract_image_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0
        assert artifact["location"] is None

    def test_missing_model_still_hashes(self):
        """model 欄位缺失不影響 digest 計算（空字串 fallback）。"""
        result_output = json.dumps({"path": "/img/foo.png", "bytes": 100})
        call = _tc("image_generate", {})
        result = _tr("image_generate", success=True, output=result_output)

        artifact = _extract_image_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 100
        # 缺 model: 應 fallback 到空字串，不 crash
        expected_digest_input = b"/img/foo.png|100|"
        assert artifact["digest"] == "sha256:" + hashlib.sha256(expected_digest_input).hexdigest()

    def test_different_bytes_produces_different_digest(self):
        """同 path 不同 bytes → digest 必須不同（這是 artifact identity 的關鍵）。"""
        call_a = _tc("image_generate", {})
        call_b = _tc("image_generate", {})
        result_a = _tr("image_generate", success=True, output=json.dumps({"path": "/p", "bytes": 100, "model": "m"}))
        result_b = _tr("image_generate", success=True, output=json.dumps({"path": "/p", "bytes": 200, "model": "m"}))

        a = _extract_image_generate_artifact(call_a, result_a)
        b = _extract_image_generate_artifact(call_b, result_b)

        assert a is not None and b is not None
        assert a["digest"] != b["digest"]

    def test_mcp_alias_openai_text_to_image_uses_same_extractor(self):
        """openai__text_to_image 走 _ARTIFACT_EXTRACTORS 字典映射到同一個 extractor。
        這裡只驗證該 alias 存在於 dispatcher，且底層 extractor 行為一致。"""
        from loom.core.harness.middleware import _ARTIFACT_EXTRACTORS, extract_artifact_info
        assert "openai__text_to_image" in _ARTIFACT_EXTRACTORS
        # dispatcher 走同一個 extractor，確認它能消化
        call = _tc("openai__text_to_image", {})
        result = _tr("openai__text_to_image", success=True, output=json.dumps({"path": "/p.png", "bytes": 50, "model": "gpt-image-2"}))
        info = extract_artifact_info(call, result)
        assert info is not None
        assert info["artifact_type"] == "image"


# ---------------------------------------------------------------------------
# _extract_tts_generate_artifact
# ---------------------------------------------------------------------------

class TestExtractTtsGenerateArtifact:
    """tts_generate 產物萃取：digest 由 path|bytes|voice_id|format 計算。"""

    def test_normal_json_output(self):
        result_output = json.dumps({
            "path": "/audio/voice.mp3",
            "bytes": 50000,
            "voice_id": "eve",
            "format": "mp3",
        })
        call = _tc("tts_generate", {"text": "hello"})
        result = _tr("tts_generate", success=True, output=result_output)

        artifact = _extract_tts_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["artifact_type"] == "audio"
        assert artifact["size_bytes"] == 50000
        assert artifact["location"] == "/audio/voice.mp3"
        expected_digest_input = b"/audio/voice.mp3|50000|eve|mp3"
        assert artifact["digest"] == "sha256:" + hashlib.sha256(expected_digest_input).hexdigest()

    def test_missing_voice_id_still_hashes(self):
        """voice_id 缺失不應 crash（空字串 fallback）。"""
        result_output = json.dumps({"path": "/a.mp3", "bytes": 100, "format": "wav"})
        call = _tc("tts_generate", {})
        result = _tr("tts_generate", success=True, output=result_output)

        artifact = _extract_tts_generate_artifact(call, result)

        assert artifact is not None
        expected_digest_input = b"/a.mp3|100||wav"
        assert artifact["digest"] == "sha256:" + hashlib.sha256(expected_digest_input).hexdigest()

    def test_missing_format_still_hashes(self):
        """format 缺失不應 crash。"""
        result_output = json.dumps({"path": "/a.mp3", "bytes": 100, "voice_id": "ara"})
        call = _tc("tts_generate", {})
        result = _tr("tts_generate", success=True, output=result_output)

        artifact = _extract_tts_generate_artifact(call, result)

        assert artifact is not None
        expected_digest_input = b"/a.mp3|100|ara|"
        assert artifact["digest"] == "sha256:" + hashlib.sha256(expected_digest_input).hexdigest()

    def test_different_voice_id_same_content_different_digest(self):
        """語音差異是 artifact identity 的核心——不同 voice_id 必須 digest 不同。"""
        call_a = _tc("tts_generate", {"text": "hello"})
        call_b = _tc("tts_generate", {"text": "hello"})
        result_a = _tr("tts_generate", success=True, output=json.dumps({"path": "/a", "bytes": 100, "voice_id": "eve", "format": "mp3"}))
        result_b = _tr("tts_generate", success=True, output=json.dumps({"path": "/a", "bytes": 100, "voice_id": "ara", "format": "mp3"}))

        a = _extract_tts_generate_artifact(call_a, result_a)
        b = _extract_tts_generate_artifact(call_b, result_b)

        assert a is not None and b is not None
        assert a["digest"] != b["digest"]

    def test_malformed_json_falls_back_to_empty(self):
        call = _tc("tts_generate", {})
        result = _tr("tts_generate", success=True, output="not json at all")

        artifact = _extract_tts_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0
        assert artifact["location"] is None
        # 所有欄位都空 → digest 是空字串組合的 hash
        assert artifact["digest"] == "sha256:" + hashlib.sha256(b"|0||").hexdigest()

    def test_none_output_does_not_crash(self):
        call = _tc("tts_generate", {})
        result = _tr("tts_generate", success=True, output=None)

        artifact = _extract_tts_generate_artifact(call, result)

        assert artifact is not None
        assert artifact["size_bytes"] == 0
        assert artifact["location"] is None
