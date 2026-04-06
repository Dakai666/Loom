# Memory Search

Loom 的記憶搜尋系統支援兩種機制：**傳統關鍵字搜尋（BM25）**和**向量相似度搜尋（Phase 5）**。

---

## 兩階段瀑布搜尋

```
recall(query)
    │
    ├─▶ Phase 5: cosine_similarity（embedding 匹配）
    │       │
    │       └─▶ 找到相似結果 → 返回
    │
    ├─▶ Phase 4: BM25 (FTS5) 加權搜尋
    │       │
    │       └─▶ 找到結果 → 返回
    │
    └─▶ recency fallback
            └─▶ 返回最近 N 筆（確保不回傳空結果）
```

---

## Phase 5：向量相似度（第一優先）

### 前提條件

`SemanticMemory` 需持有 `EmbeddingProvider` 實例（`has_embeddings == True`）。若未配置或 embedding 失敗，自動 fallback 至 BM25。

### 實現機制

向量儲存於 `semantic_entries.embedding` 欄位（JSON 字串）。查詢時以 `sqlite-vec` 的 `vec_distance_cosine()` 計算餘弦距離：

```sql
SELECT id, key, value, confidence, source, metadata, created_at, updated_at,
       1.0 - vec_distance_cosine(embedding, ?) AS score
FROM semantic_entries
WHERE embedding IS NOT NULL
ORDER BY vec_distance_cosine(embedding, ?) ASC
LIMIT ?
```

距離越小越相似，取 `1.0 - distance` 轉換為相似度分數（0–1）。

### SemanticMemory.upsert() 中的自動嵌入

```python
async def upsert(self, entry: SemanticEntry) -> bool:
    # 先 upsert 文字內容
    await self._db.execute(...)
    await self._db.commit()

    # 背景嘗試計算並寫入向量
    if self._embeddings is not None:
        try:
            vectors = await self._embeddings.embed([text])
            if vectors:
                await self._db.execute(
                    "UPDATE semantic_entries SET embedding = ? WHERE key = ?",
                    (json.dumps(vectors[0]), entry.key),
                )
                await self._db.commit()
        except Exception:
            pass  # embedding 失敗不阻擋 upsert
```

---

## Phase 4：BM25（經 SQLite FTS5）

### 實現機制

Loom 不在 Python 層自實現 BM25，而是利用 **SQLite FTS5**。FTS5 內建 BM25 排序，由 SQL 引擎直接計算。

> **v0.2.5.2 更新**：先前版本曾有 Python 層的 `Bm25Cache` 類別實驗性實作，已移除。v0.2.5.2 起全面採用 SQLite FTS5，無需 Python 層 Cache。
>
> **v0.2.6.1 清理**：`loom/core/memory/__init__.py` 中殘留的 stale `BM25` export（在 v0.2.5.2 FTS5 替換時未清除）已移除（#36）。

### FTS5 虛擬表與同步觸發

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS semantic_fts
USING fts5(key, value, content='semantic_entries', content_rowid='rowid');

-- 每次 semantic_entries INSERT/UPDATE/DELETE 時自動同步 FTS 表
CREATE TRIGGER IF NOT EXISTS semantic_entries_ai AFTER INSERT ON semantic_entries BEGIN
  INSERT INTO semantic_fts(rowid, key, value)
    VALUES (new.rowid, new.key, new.value);
END;
```

### BM25 查詢

```python
async def _search_semantic(self, query: str, limit: int) -> list[MemorySearchResult]:
    safe_query = _sanitize_fts(query)  # 將自然語言轉為 FTS5 AND 格式

    # SQLite FTS5 的 bm25() 預設為負值（越小越相關），取絕對值為正分數
    cursor = await self._semantic._db.execute("""
        SELECT e.id, e.key, e.value, e.confidence, e.source,
               e.metadata, e.created_at, e.updated_at,
               bm25(semantic_fts) AS fts_score
        FROM semantic_fts
        JOIN semantic_entries e ON semantic_fts.rowid = e.rowid
        WHERE semantic_fts MATCH ?
        ORDER BY rank
        LIMIT ?
    """, (safe_query, limit))

    for r in rows:
        score = abs(r[8]) if r[8] else 0.0  # 負值 → 正分數
```

---

## 衝突時的 BM25 vs 向量

| 情況 | 向量相似度 | BM25（FTS5） |
|------|-----------|--------------|
| 語義相似但用詞不同 | ✅ 匹配 | ❌ 不匹配 |
| 精確關鍵字匹配 | ✅ 匹配 | ✅ 匹配 |
| embedding 未配置或 API 失敗 | ❌ 跳過 | ✅ fallback 可用 |
| corpus 很大時的召回 | ✅ 更精確 | ⚠️ SQLite FTS5 原生支援 |

---

## 搜尋與 Memory Index 的關係

```
MemoryIndex（每次 session 開始時生成）
    │
    ├─ 總 fact 數量
    ├─ 總 skill 數量
    ├─ 總 compressed episode 數量
    ├─ Anti-patterns 數量（should_avoid 三元組）
    └─ 主題關鍵詞（top topics）
           │
           ▼
recall(query)  ← Agent 按需呼叫
           │
           ▼
    MemorySearch 執行搜尋
           │
           ▼
    返回相關 entries
```

Memory Index 是「目錄」，Memory Search 是「圖書館員」——目錄告訴你哪裡有東西，圖書館員幫你找到具體的書。

---

## 效能考量

| 優化 | Phase | 說明 |
|------|-------|------|
| SQLite FTS5 內建 BM25 | Phase 4 | SQL 引擎直接計算，無 Python Cache 需求 |
| FTS5 觸發器同步 | Phase 4 | INSERT/UPDATE/DELETE 自動同步，無需手動維護 |
| 向量預計算 | Phase 5 | upsert 時計算，讀取時直接用 |
| 混合策略 | Phase 5 | 向量 → BM25 → recency 三層 fallback |
| SQLite WAL | 初始 | 讀取不阻塞寫入 |
