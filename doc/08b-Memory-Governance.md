# Memory Governance

> **「每一筆記憶都帶著來源，來源決定可信度，可信度決定生死。」**

Memory Governance 是 Loom 的記憶治理層（Issue #43，v0.2.9.0 引入），以 **always-on** 的方式包裹所有語義記憶寫入路徑，提供：

1. **Trust-tier 信任分級** — 每個來源字串映射至 0.5–1.0 的信任等級
2. **矛盾偵測與自動解決** — 寫入前先比對既有事實，依信任等級決定取捨
3. **Admission Gate** — Session 壓縮前過濾低品質事實
4. **Decay Cycle** — Session 結束時清除過期/衰減記憶
5. **Hook A — Contradiction Notice**（v0.3.6.0+，Issue #281 P3-B）— 矛盾寫入時主動通知 agent

---

## 模組位置

```
loom/core/memory/
├── governance.py      ← MemoryGovernor（協調層）
├── contradiction.py   ← ContradictionDetector（偵測 + 解決）
├── semantic.py        ← SemanticMemory + TRUST_TIERS + classify_source()
├── lifecycle.py       ← MemoryLifecycle（domain × temporal decay table）
├── maintenance.py     ← MaintenanceLoop（daemon-cron decay runner）
├── pulse.py          ← MemoryPulse（Hook G session preheat + Hook A contradiction notice）
└── relational.py    ← RelationalMemory（dreaming + decay）
```

---

## 信任等級（Trust Tiers）

所有語義記憶寫入都帶有 `source` 字串，Governance 層用 `classify_source()` 映射至等級與基準信心值：

| Tier | 信心值 | 對應 source 模式 | 說明 |
|------|--------|-----------------|------|
| `user_explicit` | 1.0 | `"manual"`, `"user"` | 使用者親自輸入的指令 |
| `tool_verified` | 0.9 | `"tool:*"` | 工具執行結果驗證的事實 |
| `agent_memorize` | 0.85 | `"memorize"` | Agent 透過 `memorize` 工具主動記憶 |
| `session_compress` | 0.8 | `"session:*"` | LLM 從 Episodic 壓縮出的事實 |
| `counter_factual` | 0.75 | `"counter_factual:*"` | 反事實反思（試過但失敗的方法） |
| `agent_inferred` | 0.7 | `"skill_eval:*"` | Agent 的自我推斷 |
| `skill_evolution` | 0.65 | `"skill_evolution"` | 技能演化建議 |
| `dreaming` | 0.6 | `"dreaming"` | 離線 Dream 合成 |
| `external` | 0.5 | `"fetch:*"`, `"web:*"` | 外部 URL / 搜尋結果 |
| `unknown` | 0.5 | 其他 | 未分類來源 |

Confidence floor 規則：`entry.confidence = max(entry.confidence, tier_confidence)`

---

## MemoryGovernor

```python
governor = MemoryGovernor(
    semantic=semantic,
    procedural=procedural,
    relational=relational,
    episodic=episodic,
    db=db,
    config={                         # 對應 loom.toml [memory.governance]
        "admission_threshold": 0.5,
        "episodic_ttl_days": 30,
        "semantic_decay_threshold": 0.1,
        "relational_decay_factor": 1.5,
    },
)
```

Governor 在 `LoomSession.start()` 時建立，不需要設定開關，永遠啟用。`set_pulse()` 用於串接 `MemoryPulse`（Hook A）。

---

## 1. Governed Upsert + Hook A

`governor.governed_upsert(entry)` 包裹所有語義記憶寫入，流程如下：

```
SemanticEntry 傳入
    │
    ▼
classify_source(entry.source)
    → tier_name, tier_confidence
    → entry.confidence = max(entry.confidence, tier_confidence)
    │
    ▼
ContradictionDetector.detect(entry)
    ├─ Tier 1: 完全相同的 key + 不同 value → KEY_MATCH 矛盾
    └─ Tier 2: 共同三段前綴 + 相同 key 深度 + 低 word overlap → KEY_PREFIX 矛盾
    │
    ▼（若有矛盾）
ContradictionDetector.resolve(contradiction)
    ├─ proposed trust > existing trust  → REPLACE（新值覆蓋）
    ├─ existing trust > proposed trust  → KEEP（舊值保留，放開寫入）
    └─ trust 相等                       → SUPERSEDE（新值覆蓋，recency bias）
    │
    ▼
    Hook A（MemoryPulse.contradiction_inject）
      → 若 self._pulse 不為 None，寫入 _pending_pulses
      → once-per-key-per-session gate（via memory_meta）
      → 下一 turn drain 作為 <system-reminder> block
    │
    ▼
SemanticMemory.upsert() 或跳過
    │
    ▼
audit_log 寫入 governance 事件
```

**Hook A 設計原則**：矛盾通知的觸發與 resolution 無關——即使舊值勝出（KEEP），agent 仍需知道「有人試圖覆寫」。gate 在 session start 時清除，確保下一 session 看到同一矛盾時仍有通知。

### 回傳值

```python
@dataclass
class GovernedWriteResult:
    written: bool                # False = 被 KEEP 攔截
    trust_tier: str
    adjusted_confidence: float
    contradictions_found: int
    resolution: str | None       # "replace" | "supersede" | "kept" | None
```

---

## 2. Admission Gate

在 `compress_session()` 將 Episodic 壓縮為 Semantic 事實前，Governor 會過濾低品質候選：

```python
admitted_facts = [
    r.fact
    for r in await governor.evaluate_admission(candidate_facts, source=...)
    if r.admitted
]
```

評分標準（各 0.0–1.0，加權平均）：

| 指標 | 權重 | 說明 |
|------|------|------|
| **Length** | 0.2 | < 10 字元 → 0.1；10–20 → 0.3；20–500 → 0.8；> 500 → 0.5 |
| **Info Density** | 0.3 | 非停用詞比例 |
| **Novelty** | 0.5 | 1 − 與近期事實的最大 word overlap；> 0.8 overlap → 直接拒絕（duplicate）|

預設 `admission_threshold = 0.5`，可在 `loom.toml` 調整。

---

## 3. Decay Cycle — Domain × Temporal Decay Table

v0.3.6.0（Issue #281 P2）起，衰減不再由單一 threshold 決定，而是由 `MemoryLifecycle` 的 `(domain, temporal)` 矩陣定義：

| domain | temporal | 觸發條件 |
|--------|----------|---------|
| knowledge | recent | 不衰減 |
| knowledge | archived | 30 天衰減週期 |
| project | recent | 45 天衰減週期 |
| project | archived | 14 天衰減週期 |
| self | any | 永不衰減 |
| user | any | 永不衰減 |

每個 semantic entry 的 `effective_confidence` 依 90 天半衰期遞減，`last_accessed_at` 作為「最近接觸時間」——被 recall 的 fact 會 refresh 半衰期計時。

**Relational decay**：`source='dreaming'` 的三元組使用 `decay_factor = 1.5`（有效半衰期 60 天 vs. 標準 90 天），其他 relational triples 在 `temporal=archived` 時以 30 天衰減。

### MaintenanceLoop（daemon-cron）

`MaintenanceLoop`（Issue #281 P3-A）是 daemon 程序，以 cron schedule 執行 decay cycle：

```
* /5 * * * *   # 預設每 5 分鐘檢查一次
```

設計原則：
- **`run()` throttle**：每次執行前檢查是否有上一次還在跑的 instance；防止重疊（overlapping）執行
- 與互動式 session 的 `run_decay_cycle()` 完全獨立，互不干擾
- 適合伺服器長時間運行時的背景維護

---

## 4. Dream 2.0 — Themed Round-Robin Sampling

v0.3.6.0（Issue #281 P3-C）起，`dream_cycle` 的 sampler 支援 themed sampling。

核心改動：sampler 在每次 call 時輪詢（round-robin）非空 domain，確保 Dream 合成的三元組來自多樣的 fact-types，而非過度集中於單一 domain。

```
dream_cycle(sample_size=15)
  → 依序取用 knowledge recent / project recent / knowledge archived / ...
  → 每輪 call 輪詢到下一個非空 domain
```

這讓 relational triples 更能捕捉 cross-domain 的關係，而非只是同一個 topic 內部的瑣碎連結。

---

## 矛盾偵測策略（ContradictionDetector）

### Tier 1 — Exact Key Match

相同 key + 不同 value → 定義為衝突，`similarity_score = 1.0`，立即返回，跳過 Tier 2。

### Tier 2 — Key Prefix Match

條件（三個都要滿足才標記為潛在衝突）：
1. 提議的 key 至少有 3 段（`a:b:c`）
2. 共同前綴為前 3 段（`a:b:c`）
3. 現有 entry 與提議 key 的深度相同（避免父子鍵的誤判）
4. word overlap < 0.3（值差異夠大才是矛盾）

> 設計理由：`user:preference:theme` 和 `user:preference:font` 雖有相同 `:2` 前綴，但深度相同且前 3 段不同，不會被誤判為矛盾。

### Tier 3 — Embedding Similarity（待實作）

`Resolution.MERGE` 預留介面，未來用 LLM 仲裁高相似度的語義對立事實。

---

## Audit Log

所有 Governance 事件寫入 `audit_log` 表，`tool_name` 前綴為 `governance:`：

```sql
SELECT tool_name, error, details, created_at
FROM audit_log
WHERE tool_name LIKE 'governance:%'
ORDER BY created_at DESC LIMIT 20;
```

| `tool_name` | 觸發時機 |
|-------------|---------|
| `governance:contradiction` | KEEP 決定（被攔截的寫入） |
| `governance:write` | 有矛盾但仍寫入（REPLACE / SUPERSEDE） |
| `governance:admission` | Admission Gate 有拒絕 |
| `governance:decay` | Decay Cycle 有刪除 |

---

## 與 memorize 工具的整合

`make_memorize_tool(semantic, governor=governor)` 注入 Governor。Agent 呼叫 `memorize` 時：

- `source` 設為 `"memorize"` → 映射至 `agent_memorize`（trust = 0.85）
- 若被 KEEP 攔截 → 工具回傳 `success=True`，說明原因（不是錯誤）
- 若有矛盾但仍寫入 → 回傳說明解決了幾個矛盾

這確保 Agent 自己記憶的事實不會意外覆蓋使用者明確設定的高信任記憶。

---

## 設定參考

```toml
[memory.governance]
admission_threshold      = 0.5    # Admission Gate 門檻（0.0–1.0）
episodic_ttl_days        = 30     # Episodic TTL（天）
semantic_decay_threshold = 0.1    # Semantic prune 門檻（legacy，見 Decay Table）
relational_decay_factor  = 1.5    # Dreaming triples 加速衰減係數

[memory.maintenance]
enabled     = true
cron        = "*/5 * * * *"     # daemon-cron schedule（UTC）
run_throttle = true              # 防止 overlapping 執行（預設開啟）
```

詳見 [37-loom-toml-參考.md](37-loom-toml-參考.md)。
