# Context Budget（更新版）

> 依據 `loom/core/cognition/context.py` 實際實作更新。

---

## ⚠️ 與舊版文件的差異

舊版描述了 `can_fit()`、`consume()` 等**不存在的 API**。實際實作：

- Token 估算用 `len(str) // 4`（~4 chars/token heuristic）
- `record_response()` 接收**provider 回報的 input_tokens**，並**替換**（非累加）`used_tokens`
- `record_messages()` 用於 compression 後重新計算
- `should_compress()` 基於 `usage_fraction >= compression_threshold`（預設 0.80）

---

## ContextBudget 核心 API

```python
@dataclass
class ContextBudget:
    total_tokens: int                      # 模型 context window 大小
    compression_threshold: float = 0.80   # 觸發壓縮的 fraction
    used_tokens: int = field(default=0)   # 內部追蹤

    # --- 帳務方法 ---
    def record_response(self, input_tokens: int, output_tokens: int) -> None:
        """
        從 provider 回報的 token 使用量更新預算。

        重要：input_tokens 是「模型處理的總 context」，
        所以 used_tokens 是 **替換**（replace）而非累加。
        input_tokens == 0 → no-op（避免 provider 未回報時把真實讀數覆蓋成零）。
        """
        if input_tokens <= 0:
            return
        self.used_tokens = input_tokens + output_tokens

    def record_messages(self, messages: list[dict]) -> None:
        """Compression 後從頭計算訊息列表的 token 總數"""
        self.used_tokens = sum(estimate_tokens(m) for m in messages)

    def add(self, tokens: int) -> None:
        """手動遞加（用於微調）"""
        self.used_tokens += tokens

    # --- 查詢方法 ---
    @property
    def remaining(self) -> int: ...
    @property
    def usage_fraction(self) -> float: ...
    def should_compress(self) -> bool: ...
    def fits(self, text: str) -> bool: ...  # text 是否能在剩餘空間內

    def reset(self) -> None:
        """Compression 完成後重置"""
        self.used_tokens = 0
```

---

## Token 估算

```python
def estimate_tokens(obj: Any) -> int:
    if isinstance(obj, str):
        return max(1, len(obj) // 4)
    try:
        return max(1, len(json.dumps(obj, ensure_ascii=False)) // 4)
    except (TypeError, ValueError):
        return 1
```

精度約 ±15%（英文），足夠觸發 threshold 判斷。

---

## Trigger 閾值

| Threshold | 意義 |
|-----------|------|
| `usage_fraction >= 0.80`（預設）| 觸發 compression |
| `usage_fraction >= 0.90` | 嚴重警告 |

Compression 由 `LoomSession.compress_session()` 實際執行：
1. 從 EpisodicMemory 取出 session 的 turn logs
2. 呼叫 LLM 摘要
3. 寫入 SemanticMemory
4. 刪除已壓縮的 Episodic entries
5. `budget.reset()`

---

## 與 MemoryGovernor 的整合

Compression 的產出（摘要事實）經過 Admission Gate：

```python
# MemoryGovernor.evaluate_admission() 評估每個候選事實
admitted = [
    r.fact for r in await governor.evaluate_admission(candidates, ...)
    if r.admitted
]
```

Admission Gate 評分標準（各 0.0–1.0，加權平均）：
- **Length**（權重 0.2）
- **Info Density**（權重 0.3）
- **Novelty**（權重 0.5）— 與近期事實的 word overlap，越高越拒絕

詳見 [08b-Memory-Governance.md](08b-Memory-Governance.md)。

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*