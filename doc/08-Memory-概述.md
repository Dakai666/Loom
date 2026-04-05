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

## Session 生命週期的記憶流向

```
Agent 執行任務
    ↓
每次 tool call → TraceMiddleware → EpisodicMemory.write()
    ↓
Session 結束 → Compression → 轉換為 FACT → SemanticMemory.upsert()
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

## Phase 5 升級：Embedding 搜尋

| 階段 | 機制 |
|------|------|
| Phase 4 | BM25 / TF-IDF 關鍵字加權 |
| Phase 5 | MiniMax Embedding cosine similarity（第一優先）→ BM25 fallback → recency fallback |

`SemanticMemory.upsert()` 寫入時自動計算向量，失敗不阻擋寫入。搜尋時先用向量相似度，再用 BM25 fallback。

---

## 四種記憶的生命週期對照

| 類型 | Session 內寫入 | Session 結束後 | 長期保存 | 可失效 |
|------|---------------|----------------|---------|-------|
| Episodic | ✅ 每次 tool call | 壓縮為 FACT | ❌ | ❌（已轉移）|
| Semantic | ✅ Agent 呼叫 memorize | — | ✅ | ✅ 可驗證 |
| Procedural | ✅ Skill 評估結果 | — | ✅ | ✅ confidence ≤ 閾值 |
| Relational | ✅ Agent 呼叫 relate | — | ✅ | ✅ 可刪除 |

---

## 下一章

- [09-四種記憶詳解.md](09-四種記憶詳解.md) — 各記憶類型的詳細說明
- [12-Memory-Index.md](12-Memory-Index.md) — Pull Mode 的輕量索引機制
