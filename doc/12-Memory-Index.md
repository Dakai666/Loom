# Memory Index

Memory Index 是 Loom 在每次 Session 開始時自動生成的輕量級元數據摘要。它不是完整的記憶內容，而是一個「目錄」——讓 agent 在進入對話前就能快速判斷：「我對這個領域知道多少？」

---

## 為什麼需要 Memory Index？

沒有 Memory Index 的系統面臨兩難：

| 方案 | 問題 |
|------|------|
| 把所有記憶塞進 system prompt | token 爆炸，很快超限 |
| 每次需要時重新讀取整個 corpus | O(n) 讀取延遲 |
| 只靠 recall() 搜尋 | agent 不知道記憶的整體輪廓 |

Memory Index 解決的是「session 開始時的上下文建構」問題，而不是「運行時搜尋」問題。兩者分工明確：

```
Memory Index  →  session 開始時（一次性）
      │
      ▼
  System prompt 的 context
      │
      ▼
Memory Search  →  運行時按需呼叫
```

---

## Index 結構

```python
# loom/core/memory/index.py
@dataclass
class MemoryIndex:
    """Session 開始時生成的輕量目錄"""

    # 數量統計
    semantic_count: int              # 總 semantic fact 數量
    semantic_topics: list[str]       # 從 semantic value 詞頻統計出的熱門主題（上限 6 個）
    skill_count: int                 # 主動（未廢棄）skill 基因組數量
    skill_tags: list[str]           # 所有主動 skill 的 tags 合併去重（上限 10 個）
    episode_sessions: int            # 壓縮後的獨立 session 數量（從 semantic source 解析）
    relational_count: int            # 總關係三元組數量
    relational_predicates: list[str]  # 出現頻率最高的前 8 個 predicate

    # Anti-pattern 追蹤（Issue #26 Self-Portrait）
    anti_pattern_count: int          # 帶有 should_avoid predicate 的三元組數量
    self_triples: list[Any]          # loom-self 主詞的所有三元組（供 Self-Portrait 展示）

    _WIDTH: int = 45                # 渲染時分隔線寬度
```

> **說明**：doc/11-Memory-Search.md 先前版本的 `Bm25Cache` 類別於實際源碼中並不存在，已移除。

---

## 生成時機

`MemoryIndexer` 在 session 啟動時查詢各 Memory 類，產生 `MemoryIndex`：

```python
class MemoryIndexer:
    async def build(self) -> MemoryIndex:
        # 讀取最多 500 筆 recent semantic entries（用於話題統計）
        facts = await self._semantic.list_recent(limit=500)

        # 話題：從 value 欄位分詞，取最高頻的非停用詞（上限 6 個）
        semantic_topics = _extract_topics(facts)

        # Episode sessions：從 source 解析 session ID 去重
        # 格式 "session:abc123:fact:5" → "session:abc123" 取前兩段
        episode_sessions = len({
            ":".join(f.source.split(":")[:2])
            for f in facts if f.source and f.source.startswith("session:")
        })

        # Skills
        skills = await self._procedural.list_active()
        all_tags = {tag for s in skills for tag in s.tags}

        # Relational triples
        triples = await self._relational.query()
        relational_predicates = sorted(
            {p: sum(1 for t in triples if t.predicate == p) for p in set(t.predicate for t in triples)}.items(),
            key=lambda x: x[1], reverse=True
        )[:8]

        # Self-portrait：loom-self 三元組，should_avoid 排在最前
        self_entries = await self._relational.query(subject="loom-self")
        self_triples = sorted(
            self_entries,
            key=lambda t: ({"should_avoid": 0, "tends_to": 1}.get(t.predicate, 2), t.updated_at),
        )
```

---

## 注入 System Prompt

Memory Index 的內容以固定格式渲染後，成為 session system prompt 的一部分：

```
Memory Index
─────────────────────────────────────────────
Semantic  : 47 facts   [topics: python, loom, testing, config]
Skills    : 12 active  [tags: refactor, bash, git, python]
Episodes  : 8 sessions compressed
Relations : 34 triples  [predicates: first_applied, has_known_gap, applied_on]
─────────────────────────────────────────────
Use recall(query) to retrieve relevant entries.
Use memorize(key, value) to store a new fact.
Use query_relations(subject) to look up relationships.

Self-Portrait (loom-self observations):
  [should_avoid] write long summaries without checking context
  [tends_to] start with recall before major tasks
```

### Self-Portrait（Issue #26）

`self_triples` 中 `subject="loom-self"` 的三元組會在 Index 中直接展示，讓 agent 看到自己的行為觀察記録（「我之前應該避免什麼」、「我傾向於什麼」）。

---

## 輕量原則

Memory Index 的設計嚴守輕量原則：

| 設計決策 | 理由 |
|----------|------|
| 話題從 recent entries 抽而非全 corpus | 避免大量 DB 掃描 |
| 上限限制（topics ≤ 6, predicates ≤ 8）| 防止膨脹 |
| episode sessions 從 source 解析而非獨立表 | 節省查詢 |
| 每次 session 重新生成 | 確保時效性 |

---

## 與 Memory Search 的分工

```
┌─────────────────────────────────────────┐
│           Session 初始化                 │
├─────────────────────────────────────────┤
│                                         │
│   MemoryIndex.build()                   │
│       │                                 │
│       ├── semantic_count: 47            │  ← 輕量統計
│       ├── semantic_topics: ["loom", ...] │  ← 從 value 詞頻
│       ├── episode_sessions: 8           │  ← 從 source 解析
│       ├── relational_predicates: [...]  │  ← 從 predicate 頻率
│       └── self_triples: [...]          │  ← loom-self 觀察
│                                         │
│   ↓ 渲染後注入 system prompt            │
│                                         │
├─────────────────────────────────────────┤
│           運行時（Agent 呼叫 recall）      │
├─────────────────────────────────────────┤
│                                         │
│   recall("harness middleware")          │
│       │                                 │
│       ├── Phase 5: cosine_similarity   │  ← 精確匹配
│       ├── Phase 4: BM25 (FTS5)          │
│       └── recency fallback             │
│                                         │
└─────────────────────────────────────────┘
```

| | Memory Index | Memory Search |
|---|---|---|
| 時機 | session 開始（一次性） | 運行時按需 |
| 目的 | 告訴 agent「你知道什麼」 | 幫 agent「找到什麼」 |
| 內容 | 統計 + 話題 + predicates | 完整 entry 內容 |
| token 影響 | ~200-500 tokens | 不影響 prompt |

---

## 總結

Memory Index 是 Loom 記憶系統的「入口目錄」：

1. **Session 開始時生成** — 一次性，不影響運行時效能
2. **輕量統計** — 從現有表格解析，不另建昂貴索引
3. **雙重角色** — 既告訴 agent 自己的知識邊界，也展示自我行為觀察（Self-Portrait）
4. **明確分工** — Index 管「我知道多少」，Search 管「找到我需要的」
