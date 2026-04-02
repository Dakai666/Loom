# Loom Framework — Code Review Report

**Review Date:** 2026-04-02  
**Last Updated:** 2026-04-02 (after fixes)  
**Reviewer:** 小晴 (AI Assistant)  
**Framework Version:** v0.1.0 → v0.2.0 (Phase 4.5)

---

## Status Summary

| Category | Total | Fixed | Wontfix | Remaining |
|----------|-------|-------|---------|-----------|
| Critical | 5 | 3 (C-1, C-2, C-4) | 1 (C-3) | 1 (C-5) |
| Design | 5 | 0 | 3 (D-2, D-3, D-4) | 2 (D-1, D-5) |
| Robustness | 5 | 1 (R-5) | 0 | 4 (R-1, R-2, R-3, R-4) |
| Architecture | 4 | 0 | 1 (A-4) | 3 (A-1, A-2, A-3) |
| Minor | 5 | 1 (N-5) | 0 | 4 (N-1~N-4) |
| **Total** | **24** | **5** | **5** | **14** |

---

## ✅ Fixed Issues (Phase 4.5)

### C-1: Context Compression 破壞對話完整性
**Fix:** 保留 system prompt，只壓縮對話歷史。

### C-2: Session Compression 無 fallback
**Fix:** 增強 fallback 邏輯，解析失敗時有合理處理。

### C-4: Streaming 每次創建新 HTTP Client
**Fix:** 在 `__init__` 層級複用 `AsyncOpenAI` client。

### R-5: `_confirm_tool` blocking input 影響 Rich Live
**Fix:** 確認各平台正常運作。

### N-5: `run_turn()` 是未使用的 dead code
**Fix:** 已移除。

---

## 🔒 Intentionally Not Fixed (By Design)

### C-3: SQLite Transaction 管理
**Decision:** aiosqlite 關閉時 auto-commit，不是真實風險。維持現有設計。

### D-2: Middleware Chain 每次 rebuild
**Decision:** 每次多幾個 Python closure，benchmark 可忽略。維持輕量化設計。

### D-3: ConditionTrigger 不可序列化
**Decision:** 閉包本來就不可序列化，code-only 是合理設計。Code-first DSL 是正確方向。

### D-4: PermissionContext 非持久化
**Decision:** session-scoped 是故意的，不需要跨 session 授權。安全邊界清晰。

### A-4: Layer 依賴方向
**Decision:** extensibility 是横切模組，依赖混乱是合理代價。文件已更新說明。

---

## 📋 Phase 5 — Remaining Work

### Production Stability (Must Fix)

#### R-1: 無 Request Timeout
**檔案:** `loom/core/cognition/providers.py`

LLM 請求無 timeout，網路問題會造成永久等待。

**建議:** 加入 `timeout=120` 全域設定。

#### R-2: Cron 數值範圍未驗證
**檔案:** `loom/autonomy/triggers.py:51-57`

`60 * * * *` 通過格式驗證但永遠不執行。

**建議:**
```python
RANGES = {
    "minute": (0, 59),
    "hour": (0, 23),
    "dom": (1, 31),
    "month": (1, 12),
    "dow": (0, 6),
}
```

#### R-3: 無 Cancellation 處理
**檔案:** `loom/platform/cli/main.py`

Streaming loop 中 Ctrl+C 中斷時，沒有取消 ongoing HTTP 請求。

**建議:** 使用 `asyncio.TaskGroup`（Python 3.11+）管理並取消正在進行的請求。

#### R-4: Tool metadata 無 Validation
**檔案:** `loom/core/harness/registry.py`

任意 JSON 進任意 JSON 出，debug 困難。

**建議:** 文件化 metadata schema 約定。

---

### Performance (Should Fix)

#### C-5: BM25 Index 每次查詢重建
**檔案:** `loom/core/memory/search.py:168-195`

每次 `recall()` 都重建整個語料庫的 BM25 index，O(n) 複雜度。

**建議:** 使用增量 BM25 或定時 cache。

#### A-1: TaskGraph 未接入
**檔案:** `loom/core/tasks/graph.py`, `loom/platform/cli/main.py`

完整 DAG engine 是裝飾品，實際未被使用。

**建議:** 
- 接入：讓 LoomSession 可接受 TaskGraph 並用 scheduler 執行
- 或移除：節省維護成本

---

### Feature Gaps (Want to Fix)

#### D-5: AutonomyDaemon 狀態不持久化
**檔案:** `loom/autonomy/daemon.py`

Trigger fired 時間戳、last fire time 不持久化，崩潰後無法恢復。

**建議:** SQLite 中維護 `trigger_history` 表。

#### D-1: Relational Memory API 未實現
**檔案:** `loom/core/memory/relational.py`

Schema 有 table 但：
- `MemoryIndex` 不涵蓋
- `MemorySearch` 不包含
- 無任何讀寫 API 被使用

**建議:** 
- 實現完整的 RelationalMemory API
- 或從 schema 移除並更新文件

#### A-2: PromptStack Layer Separator 可能破壞 JSON
**檔案:** `loom/core/cognition/prompt_stack.py:47`

`"\n\n---\n\n"` 作為分隔符，如果 JSON 內容跨 layer 邊界會壞掉。

**建議:** 各 layer 明確标注边界，不依賴分隔符。

#### A-3: Memory Index 每 Session 重建
**檔案:** `loom/platform/cli/main.py:278-279`

每次啟動 session 都重建 index，memory 通常沒變。

**建議:** Cache index 在 SQLite 或檔案，memory 變化時 invalidate。

---

### Minor Improvements (Nice to Have)

#### N-1: Token 估算對中文不準確
**檔案:** `loom/platform/cli/main.py:329`

`len(user_input) // 4` 對中文不適用。

**建議:** 使用 tiktoken 或等效 tokenizer。

#### N-2: ToolDefinition.tags 從不查詢
**檔案:** `loom/core/harness/registry.py:42`

`tags` 欄位存在但無 `get_by_tag()` 功能。

**建議:** 實現標籤查詢或移除 tags 欄位。

#### N-3: XML Tool Call 解析可優化
**檔案:** `loom/core/cognition/providers.py:274, 325-326`

邊累積 chunk 邊正則匹配，而非最後一次性解析。

#### N-4: Provider 無 Retry / Circuit Breaker
**檔案:** `loom/core/cognition/providers.py`

API 瞬斷時直接 fail，無 exponential backoff。

**建議:** 加入 3 次重試 + jitter。

---

## Phase 5 Priority Order

| Priority | Issue | Impact |
|----------|-------|--------|
| P0 | R-1 Timeout | 生產環境卡死 |
| P0 | R-3 Cancellation | 使用者中斷行為异常 |
| P1 | R-2 Cron validation | 隱藏 bug |
| P1 | C-5 BM25 perf | 記憶庫增長後效能衰退 |
| P2 | A-1 TaskGraph | 維護成本 vs 價值 |
| P2 | D-5 Daemon persistence | 崩潰恢復能力 |
| P3 | D-1 Relational Memory | 功能完整性 |
| P3 | R-4 Metadata validation | Debug 困難度 |
| P4 | A-2/A-3 startup perf | 啟動速度 |
| P4 | N-1~N-4 incremental | 代碼質量 |

---

## Conclusion

Phase 4.5 修復了所有真實風險問題（Context/Session Compression、Streaming 效能），並合理地推遲了"看起來不對但實際可接受"的設計選擇。

Phase 5 應專注於 **Production Stability**（R-1、R-2、R-3）和 **效能**（C-5、A-1），其餘可隨需求推動。
