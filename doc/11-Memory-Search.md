# Memory Search

Loom 的記憶搜尋系統支援兩種機制：**傳統關鍵字搜尋（BM25/TF-IDF）**和**向量相似度搜尋（Phase 5）**。

---

## 兩階段瀑布搜尋

```
recall(query)
    │
    ├─▶ Phase 5: cosine_similarity（embedding 匹配）
    │       │
    │       └─▶ 找到相似結果 → 返回
    │
    ├─▶ Phase 4: BM25 加權搜尋
    │       │
    │       └─▶ 找到結果 → 返回
    │
    └─▶ recency fallback
            └─▶ 返回最近 N 筆（確保不回傳空結果）
```

---

## Phase 4：BM25 / TF-IDF

### BM25 原理

BM25（Best Matching 25）是一種基於關鍵字匹配的文件排序演算法，比簡單的 TF-IDF 更精細：

```python
def bm25_score(document: str, query: str, avg_dl: float, k1=1.5, b=0.75) -> float:
    """
    document: 待評分文件
    query: 查詢關鍵字（已分詞）
    avg_dl: 平均文檔長度
    k1, b: BM25 參數
    """
    score = 0.0
    doc_len = len(document)

    for term in query:
        tf = document.count(term)  # 詞項頻率
        if tf == 0:
            continue

        # IDF（逆文檔頻率）：詞在越多文檔出現，IDF 越低
        idf = log((N - n + 0.5) / (n + 0.5) + 1)

        # TF 正規化（長文檔的 TF 不會無限放大）
        tf_normalized = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_dl))

        score += idf * tf_normalized

    return score
```

### Loom 的 BM25 實現

```python
# loom/core/memory/search.py
class MemorySearch:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def search(self, query: str, limit: int = 5) -> list[dict]:
        # 1. 分詞（簡單空格 split，實際可用 jieba 等）
        terms = query.lower().split()

        # 2. 讀取所有 semantic entries
        entries = self.store.fetch_all("SELECT * FROM semantic_entries")

        # 3. 計算每個 entry 的 BM25 分數
        scores = []
        avg_dl = sum(len(e.value) for e in entries) / len(entries)

        for entry in entries:
            score = bm25_score(entry.value, terms, avg_dl)
            if score > 0:
                scores.append((score, entry))

        # 4. 排序返回 top N
        scores.sort(reverse=True)
        return [e for _, e in scores[:limit]]
```

### 問題：每次搜尋重建索引

BM25 需要知道 corpus 內所有文檔的長度分佈。初始實現每次 `recall()` 都重新讀取所有 entries——在 entry 數量很大時是 O(n) 瓶頸。

**Phase 5 解決方案**：見下文「Cache 機制」。

---

## Phase 5：向量相似度

### MiniMax Embedding

Phase 5 引入 MiniMax Embedding API 計算 semantic entries 的向量表示：

```python
# loom/core/cognition/embeddings.py
class MiniMaxEmbeddingProvider:
    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://api.minimax.io/v1",
        )
        self.model = "embo-01"

    async def embed(self, text: str) -> list[float]:
        response = await self.client.embeddings.create(
            model=self.model,
            text=text,
        )
        return response.data[0].embedding
```

### Cosine Similarity

```python
def cosine_similarity(a: list[float], b: list[float]) -> float:
    """計算兩個向量的餘弦相似度"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sqrt(sum(x * x for x in a))
    norm_b = sqrt(sum(x * x for x in b))
    return dot / (norm_a * norm_b) if norm_a * norm_b > 0 else 0.0
```

### SemanticMemory.upsert() 中的自動嵌入

```python
async def upsert(self, key: str, value: str, confidence: float = 0.8):
    # 先寫入文字
    await self._upsert_text(key, value, confidence)

    # 背景嘗試計算 embedding
    try:
        embedding = await self.embedding_provider.embed(value)
        await self._update_embedding(key, embedding)
    except Exception:
        # embedding 失敗不阻擋 upsert
        pass
```

### recall() 的向量搜尋路徑

```python
async def recall(self, query: str, limit: int = 5) -> list[SemanticEntry]:
    # 1. 計算 query 的向量
    query_embedding = await self.embedding_provider.embed(query)

    # 2. 讀取所有有 embedding 的 entries
    entries = await self._fetch_all_with_embeddings()

    # 3. 計算 cosine similarity
    scored = []
    for entry in entries:
        emb = json.loads(entry.embedding)
        sim = cosine_similarity(query_embedding, emb)
        scored.append((sim, entry))

    # 4. 排序
    scored.sort(reverse=True)

    return [entry for _, entry in scored[:limit]]
```

---

## BM25 Cache 機制

### 指紋（Fingerprint）

BM25 index 的核心是 corpus 的文檔長度分佈。只要 corpus 不變，就不需要重建：

```python
class Bm25Cache:
    def __init__(self, store: SQLiteStore):
        self.store = store

    def _fingerprint(self) -> tuple[int, str]:
        """返回 corpus 的指紋：(count, max_updated_at)"""
        row = self.store.fetch_one(
            "SELECT COUNT(*), MAX(updated_at) FROM semantic_entries"
        )
        return (row[0], row[1])

    def get_or_build(self) -> dict:
        """如果 cache 有效則返回，否則重建"""
        current_fp = self._fingerprint()

        if self._cache and self._cache_fp == current_fp:
            return self._cache  # Cache hit

        # Cache miss → rebuild
        self._cache = self._build_index()
        self._cache_fp = current_fp
        return self._cache
```

`fingerprint` 改變的條件：
- 有新的 semantic entry 寫入（count 改變）
- 任何 entry 的 `updated_at` 改變（max_updated_at 改變）

---

## 衝突時的 BM25 vs 向量

| 情況 | 向量相似度 | BM25 |
|------|-----------|------|
| 語義相似但用詞不同 | ✅ 匹配 | ❌ 不匹配 |
| 精確關鍵字匹配 | ✅ 匹配 | ✅ 匹配 |
| embedding API 失敗 | ❌ 無結果 | ✅ fallback 可用 |
| corpus 很大時的召回 | ✅ 更精確 | ⚠️ 需 BM25 cache |

---

## 搜尋與 Memory Index 的關係

```
MemoryIndex（每次 session 開始時生成）
    │
    ├─ 總 fact 數量
    ├─ 總 skill 數量
    ├─ 總 compressed episode 數量
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

Memory Index 是「目錄」，MemorySearch 是「圖書館員」——目錄告訴你哪裡有東西，圖書館員幫你找到具體的書。

---

## 效能考量

| 優化 | Phase | 說明 |
|------|-------|------|
| BM25 Cache | Phase 5 | 指紋比對避免每次重建 |
| 向量預計算 | Phase 5 | upsert 時計算，讀取時直接用 |
| 混合策略 | Phase 5 | 向量 → BM25 → recency 三層 fallback |
| SQLite WAL | 初始 | 讀取不阻塞寫入 |
