# Skill Evolution（更新版）

> 依據實際 `skill_promoter.py` / `skill_mutator.py` 更新。

---

## ⚠️ 與舊版文件的差異

| 項目 | 舊版 | 實作 |
|------|------|------|
| `fast_track` | 未提及 | `from_batch_diagnostic()` 有 `fast_track` flag，`improvement >= 0.20` 時候選候選 |
| Confidence reset | 未說明 | promote 後 `confidence = 1.0`、`success_rate = 1.0`、`usage_count = 0` |
| `from_batch_diagnostic()` | 未提及 | Grader 批量診斷的專用入口，繞過 quality_ceiling gate |
| Candidate plausibility check | 未說明 | `_looks_like_skill_md()` 要求候選與 parent 至少共享一行 |
| `mutation_strategy` 值 | `"apply_suggestions"` | 還有 `"batch_meta_skill_engineer"` 用於 batch 路径 |
| `pareto_scores` | 未提及 | 儲存 task_type → quality_score 映射 |
| Diagnostic key 重建 | 未提及 | `_diagnostic_key()` 重建 SemanticMemory key |

---

## SkillMutator — 候選產出（PR 2）

### `should_propose()` Gate

```python
def should_propose(self, diagnostic: TaskDiagnostic) -> bool:
    if not self._enabled:
        return False
    if len(diagnostic.mutation_suggestions) < self._min_suggestions:
        return False
    if diagnostic.quality_score > self._quality_ceiling:
        return False
    return True
```

### `propose_candidate()` — 單一診斷

```python
async def propose_candidate(
    parent: SkillGenome,
    diagnostic: TaskDiagnostic,
    session_id: str | None = None,
) -> MutationProposal | None:
```

流程：
1. `should_propose()` gate 檢查
2. 組裝 mutation prompt（保留 frontmatter、注入 mutation_suggestions）
3. LLM 呼叫 `router.chat()`
4. `_strip_fencing()` 移除 ``` 包裹
5. `_looks_like_skill_md()` plausibility check
6. 回傳 `MutationProposal`（含候選 + prompt preview + raw length）

**非 fatal**：LLM 失敗、空輸出、不像 SKILL.md → 回傳 `None`，不阻斷流程。

### `from_batch_diagnostic()` — Grader 批量路徑

```python
async def from_batch_diagnostic(
    parent: SkillGenome,
    batch: BatchDiagnostic,
    session_id: str | None = None,
) -> MutationProposal | None:
```

與 `propose_candidate()` 的差異：
- **無 quality_ceiling gate**（Grader 觸發 = 明確意圖）
- **fast_track flag**：當 `batch.improvement >= 0.20` 時候選被標記，呼叫方可以直接 promote 而跳過 shadow
- `mutation_strategy` 設為 `"batch_meta_skill_engineer"`

### Candidate plausibility check

```python
def _looks_like_skill_md(body, parent_body) -> bool:
    # 太短（< 80 chars）→ 拒絕
    # 與 parent 零共享 non-trivial line → 拒絕（防止 hallucination）
```

### 與 TaskReflector 的整合

TaskReflector 的 post-hook：
```python
# TaskReflector 完成 TaskDiagnostic 後
mutation_proposal = await mutator.propose_candidate(parent, diagnostic, session_id)
if mutation_proposal:
    await procedural.insert_candidate(mutation_proposal.candidate)
```

---

## SkillPromoter — 生命周期管理（PR 3）

### 狀態機（實際實作）

```
generated ──┬── shadow ── promote ──→ promoted
            │                ↑           │
            │                └────────────┘ rollback → rolled_back（終端）
            └── deprecate → deprecated（終端）
```

### promote() 的實際步驟

1. fetch candidate + parent
2. 將 current parent body 寫入 `skill_version_history`（reason='promote'）
3. 覆寫 `SkillGenome.body` → candidate_body，version += 1
4. **`confidence = 1.0`、`success_rate = 1.0`、`usage_count = 0`**（新 body 從頭累積）
5. candidate → `promoted`
6. 同 parent 的其他 shadow candidates → `deprecated`（stale — 他們 shadow 的 body 已不存在）
7. 廣播 `PromotionEvent`

### rollback() 的實際步驟

1. 取出目標版本（最新 archive 或指定 version）
2. 將 current body 寫入 archive（reason='rollback'）— 這樣 rollback 本身也可 rollback
3. 覆寫 body，version += 1（保持 monotonic），reset confidence track
4. 標記被 rollback 的 candidate → `rolled_back`
5. 廣播 `PromotionEvent`

### Deprecate() 的實際步驟

只更新候選狀態，**不觸碰 parent**。也寫入 history？**不寫**——`deprecate` 不改變 body。

### `maybe_auto_shadow()` 的觸發條件

```python
async def maybe_auto_shadow(self, candidate_id, reason=None) -> Candidate | None:
    if self._shadow_mode != "auto_c":
        return None
    if candidate.status != "generated":
        return None
    if parent.confidence > self._auto_shadow_confidence_ceiling:
        return None
    return await self.shadow(candidate_id, reason=reason or "auto")
```

觸發時機：TaskReflector 在 mutation post-hook 中呼叫。

---

## PromotionEvent

```python
@dataclass
class PromotionEvent:
    kind: str                    # "auto_shadow" | "promote" | "rollback" | "deprecate"
    skill_name: str
    candidate_id: str | None
    from_version: int | None
    to_version: int
    reason: str | None = None
    session_id: str | None = None
    occurred_at: datetime
```

訊息呈現（`one_line_summary()`）：
- `promote` → `{skill}: promoted v{from}→v{to}`
- `rollback` → `{skill}: rollback v{from}→v{to}`
- `auto_shadow` → `{skill}: candidate → shadow`
- `deprecate` → `{skill}: deprecated candidate {id}`

### 訂閱者

```python
promoter.subscribe(my_callback)  # async def callback(event: PromotionEvent)
```

Discord 整合（`LoomDiscordBot._start_session()`）：
```python
async def _discord_promotion(event) -> None:
    icon = {"promote": "🔁", "rollback": "↩️", "auto_shadow": "🫥", "deprecate": "🗑️"}.get(event.kind, "•")
    await thread_ref.send(f"{icon} **Skill lifecycle:** {event.one_line_summary()}")
session.subscribe_promotion(_discord_promotion)
```

---

## loom.toml 配置（更新）

```toml
[mutation]
# PR 2 — 候選產出
enabled          = false
model            = "auto"
quality_ceiling  = 3.5       # diagnostic.quality_score ≤ 此值才改寫
min_suggestions  = 1
max_body_chars   = 6000
fast_track_threshold = 0.20   # improvement ≥ 20% → candidate.fast_track=True

# PR 3 — 生命週期路由
shadow_mode      = "auto_c"   # off / auto_c / manual_b
shadow_fraction  = 0.5
auto_shadow_confidence_ceiling = 0.7
```

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*