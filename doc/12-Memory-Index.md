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
    semantic_count: int       # 總 fact 數量
    episodic_count: int       # 總壓縮 episode 數量
    skill_count: int         # 總 skill 基因組數量
    relational_count: int     # 總關係三元組數量
    
    # 主題摘要（按 confidence 排序的 top topics）
    top_topics: list[str]
    
    # 最近 context（最近 N 次 session 的壓縮摘要）
    recent_contexts: list[str]
    
    # 健康狀態
    low_confidence_skills: list[str]   # confidence < 0.3 的 skill
    stale_episodes: list[str]           # 超過 30 天未更新的 episode
    
    # 生成時間戳
    generated_at: datetime
```

---

## 生成時機

```python
# loom/core/memory/index.py
class MemoryIndexBuilder:
    """在 session 啟動時自動生成"""
    
    async def build(self) -> MemoryIndex:
        store = self._get_store()
        
        # 並行讀取各類記憶的元數據（不走完整內容）
        semantic_count = store.count("semantic_entries")
        episodic_count = store.count("episodes")
        skill_count = store.count("skill_genomes")
        relational_count = store.count("relational_triples")
        
        # 提取 top topics（從 key 欄位做簡單聚合）
        top_topics = self._aggregate_topics(limit=10)
        
        # 讀取最近 N 次 session 的壓縮摘要
        recent_contexts = store.fetch_recent_episodes(limit=3)
        
        # 健康檢查
        low_conf_skills = store.fetch_skills_where("confidence < 0.3")
        stale_episodes = store.fetch_episodes_where(
            "last_accessed < datetime('now', '-30 days')"
        )
        
        return MemoryIndex(
            semantic_count=semantic_count,
            episodic_count=episodic_count,
            skill_count=skill_count,
            relational_count=relational_count,
            top_topics=top_topics,
            recent_contexts=recent_contexts,
            low_confidence_skills=low_conf_skills,
            stale_episodes=stale_episodes,
            generated_at=datetime.now(),
        )
```

---

## 注入 System Prompt

Memory Index 的內容會在 session 初始化時注入到 system prompt 的開頭：

```markdown
## 📊 Memory Index（本次 session 生成）

**記憶總覽**
- 12 個 Facts（topics: loom, harness, tool, middleware, memory, system）
- 14 次 Session 的壓縮記憶
- 8 個 Skills（0 active）
- 8 個關係三元組

**最近上下文**
- 上次修改 loom.toml 的決策（trust level 設定）
- 用戶偏好簡潔回答
- 專案 Next 框架的擴充規劃

**⚠️ 注意**
- 2 個低 confidence skills（需關注）
- 1 個 30+ 天未訪問的 episode

---

## 你的身份（SOUL.md）
...
```

---

## 輕量原則

Memory Index 的設計嚴守輕量原則：

| 設計決策 | 理由 |
|----------|------|
| 只讀取元數據（count/key/summary），不讀取完整內容 | 避免 O(n) 讀取 |
| 固定長度列表（top topics 限制 10 個） | 防止膨脹 |
| 壓縮過的 episode 直接取摘要，不重新處理 | 節省 CPU |
| 定期失效（每個新 session 重新生成） | 確保時效性 |

---

## 與 Memory Search 的分工

```
┌─────────────────────────────────────────┐
│           Session 初始化                 │
├─────────────────────────────────────────┤
│                                         │
│   MemoryIndex.build()                   │
│       │                                 │
│       ├── semantic_count: 12            │  ← 輕量統計
│       ├── top_topics: ["loom", ...]     │  ← 從 key 聚合
│       └── low_confidence_skills: [...]  │  ← 健康檢查
│                                         │
│   ↓ 注入 system prompt                  │
│                                         │
├─────────────────────────────────────────┤
│           運行時（Agent 呼叫 recall）      │
├─────────────────────────────────────────┤
│                                         │
│   recall("harness middleware")          │
│       │                                 │
│       ├── Phase 5: cosine_similarity   │  ← 精確匹配
│       ├── Phase 4: BM25                 │
│       └── recency fallback             │
│                                         │
└─────────────────────────────────────────┘
```

| | Memory Index | Memory Search |
|---|---|---|
| 時機 | session 開始（一次性） | 運行時按需 |
| 目的 | 告訴 agent「你知道什麼」 | 幫 agent「找到什麼」 |
| 內容 | 統計 + 摘要 | 完整 entry 內容 |
| token 影響 | ~200-500 tokens | 不影響 prompt |

---

## 低 Confidence Skills 的特殊處理

當 Memory Index 偵測到 confidence < 0.3 的 skill，會在 index 中特別標記：

```python
# MemoryIndexBuilder 中的邏輯
if low_conf_skills:
    self._append_warning(
        f"⚠️ {len(low_conf_skills)} 個 skills 處於低 confidence 狀態，"
        f"可能不準確：{', '.join(low_conf_skills[:3])}"
    )
```

這讓 agent 在看到明顯不靠譜的 skill 時能有所警覺，特別是當用戶問到相關領域時。

---

## 總結

Memory Index 是 Loom 記憶系統的「入口目錄」：

1. **Session 開始時生成** — 一次性，不影響運行時效能
2. **輕量統計** — 只讀元數據，不掃描完整內容
3. **雙重角色** — 既告訴 agent 自己的知識邊界，也標記需要注意的問題
4. **明確分工** — Index 管「我知道多少」，Search 管「找到我需要的」
