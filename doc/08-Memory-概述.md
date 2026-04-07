# Memory Layer 概述

> **「記憶是架構的基底，不是後期加上的插件。」**

Memory Layer 是 Loom 的長期知識庫。它從第一行 code 就內建於架構中，而不是作為一個可插拔的「功能」存在。

---

## 四種記憶類型

Loom 的記憶系統模擬人類認知中的四種記憶類型：

```
┌──────────────────────────────────────────────────────────────┐
│                     人類認知           Loom                   │
├──────────────────────────────────────────────────────────────┤
│  情節記憶（Episodic）  │  剛發生的事       │  EpisodicMemory │
│  語義記憶（Semantic）  │  知道的事實       │  SemanticMemory │
│  程序記憶（Procedural）│  如何做的技能     │  ProceduralMemory│
│  關係記憶（Relational）│  事物間的關係     │  RelationalMemory│
└──────────────────────────────────────────────────────────────┘
```

### Anti-pattern 記憶（v0.2.5.1）

當工具執行失敗且該工具有 SkillGenome 記錄時，會觸發**反事實反思**（Counter-factual Reflection）：

```
execution_error 發生
    ↓
LLM 分析：「這個失敗是什麼 pattern 造成的？下次應避免什麼？」
    ↓
寫入 SemanticMemory：  key = "skill:<name>:anti_pattern:<timestamp>"
寫入 RelationalMemory：(skill:<name>, has_anti_pattern, …)
                       (loom-self, should_avoid:<tool_name>, …)
```

`MemoryIndex` 在 session 啟動時讀取 `should_avoid` 三元組，agent 在每次對話開始就知道自己過去踩過的坑。

---

## 後端：SQLite WAL 模式

所有記憶統一使用 SQLite，WAL（Write-Ahead Logging）模式：

```python
store = SQLiteStore(path="~/.loom/memory.db")
store.execute("PRAGMA journal_mode=WAL")
```

**為什麼用 SQLite？**

| 考量 | 決策 |
|------|------|
| 延遲特性 | 本地 daemon，網路資料庫過度設計 |
| 併發需求 | WAL 模式支援讀者並行寫入（寫不阻塞讀）|
| 部署複雜度 | 單檔案，無需 DBA |
| 故障恢復 | WAL 確保 transactions 的 durability |

---

## Session Log 結構化（v0.2.5.3）

`session_log` 表格（記錄對話歷史）新增結構化欄位：

| 欄位 | 說明 |
|------|------|
| `raw_json` | 工具 `tool_use` / `tool_result` blocks 分別儲存，與 human-readable `content` 分離 |
| `idx_session_log_role` | 加速查詢特定 role 的所有訊息（如「列出 session X 的所有工具呼叫」）|

`load_messages()` 三層 fallback：
1. `raw_json` 欄位 → 結構化解析
2. legacy `format=raw_message` → 向後相容
3. plain text → 最舊記錄的純文字 fallback

---

## 統一路徑與初始化

所有 Memory 類別共享同一個 `SQLiteStore` 實例（連接池）：

```python
# loom/core/memory/store.py
class SQLiteStore:
    _instance: SQLiteStore | None = None

    def __init__(self, db_path: str = "~/.loom/memory.db"):
        self.path = os.path.expanduser(db_path)
        self.conn = aiosqlite.connect(self.path)
        self.conn.execute("PRAGMA journal_mode=WAL")

    @classmethod
    def get_instance(cls) -> "SQLiteStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
```

各 Memory 類別在初始化時接收同一 store 實例，確保所有操作在同一 transaction 上下文內。

---

## Memory Governance（v0.2.9.0）

所有語義記憶寫入都通過一個永遠開啟的治理層 `MemoryGovernor`，提供：

- **Trust-tier 信任分級**：每個來源字串（`"manual"`, `"session:*"`, `"dreaming"` 等）映射至 0.5–1.0 的信任等級，confidence floor 由 tier 決定
- **矛盾偵測**：寫入前比對現有事實，依信任等級自動 REPLACE / KEEP / SUPERSEDE
- **Admission Gate**：Session 壓縮前過濾低品質 / 重複事實（信心值 < 0.5）
- **Decay Cycle**：Session 結束時清除 Episodic TTL 過期條目、Semantic 低信心條目、Relational dreaming 三元組

詳見 [08b-Memory-Governance.md](08b-Memory-Governance.md)

---

## Session 生命週期的記憶流向

```
Agent 執行任務
    ↓
每次 tool call → TraceMiddleware → EpisodicMemory.write()
    ↓
Session 結束 → compress_session()
    → MemoryGovernor.evaluate_admission()  ← Admission Gate（v0.2.9.0）
    → 轉換為 FACT → MemoryGovernor.governed_upsert()
    → 矛盾偵測 → SemanticMemory.upsert() 或跳過
    ↓
Session.stop() → MemoryGovernor.run_decay_cycle()  ← Decay Cycle（v0.2.9.0）
    ↓
Semantic / Procedural / Relational → 長期保存
```

---

## 與其他層的關係

```
Memory Layer（基底）
    │
    ├──▶ Harness Layer（TraceMiddleware 寫入 episodic）
    ├──▶ Cognition Layer（Reflection API 讀取）
    ├──▶ Autonomy Engine（Action Planner 取用 context）
    └──▶ Task Engine（可選：寫入執行結果到 semantic）
```

---

## Phase 4 的演進：Pull Mode

### 舊模式（Push）

```
Session 開始 → 載入全部 semantic facts → 塞入 context → 稀釋
```

缺點：無關事實占據 context 空間熱門關鍵詞的 token。

### 新模式（Pull，Phase 4）

```
Session 開始 → 載入 MemoryIndex（輕量目錄）→ Agent 按需呼叫 recall()
```

MemoryIndex 是一個極輕量的摘要（約 100-200 tokens），永遠駐留 context。Agent 透過 `recall(query)` 按需召回相關內容，而不是一次全部載入。

詳細：請參閱 [12-Memory-Index.md](12-Memory-Index.md)

---

## Phase 5 升級：Embedding 搜尋 + SQLite FTS5

| 階段 | 機制 |
|------|------|
| Phase 5 | MiniMax Embedding cosine similarity（**第一優先**）→ SQLite FTS5 BM25 fallback → recency fallback |
| Phase 4 | SQLite FTS5 BM25（全功能）|

v0.2.5.2 將 Phase 4 的 Python 層 BM25 替換為 **SQLite FTS5**：

```sql
-- 自動同步的虛擬表（INSERT/UPDATE/DELETE 觸發器）
CREATE VIRTUAL TABLE semantic_fts
USING fts5(key, value, content='semantic_entries', content_rowid='rowid');
```

優點：SQLite 原生 BM25 計算、正確 Unicode 分詞、記憶體佔用低。`initialize()` 啟動時執行 `rebuild`，所有現有記錄自動建立索引。

---

## 四種記憶的生命週期對照

| 類型 | Session 內寫入 | Session 結束後 | 長期保存 | 可失效 |
|------|---------------|----------------|---------|-------|
| Episodic | ✅ 每次 tool call | 壓縮為 FACT（Admission Gate 過濾） | ❌ | ✅ TTL 30 天（Decay Cycle）|
| Semantic | ✅ governed_upsert（Trust tier + 矛盾偵測） | — | ✅ | ✅ confidence 衰減 < 0.1 |
| Procedural | ✅ Skill 評估結果 | — | ✅ | ✅ confidence ≤ 閾值 |
| Relational | ✅ Agent 呼叫 relate、Anti-pattern 反思寫入 | — | ✅ | ✅ dreaming 加速衰減 |

---

## 下一章

- [09-四種記憶詳解.md](09-四種記憶詳解.md) — 各記憶類型的詳細說明
- [12-Memory-Index.md](12-Memory-Index.md) — Pull Mode 的輕量索引機制
