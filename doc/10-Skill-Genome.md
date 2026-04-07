# Skill Genome

Skill Genome 是 Loom 的「程序記憶」單元——它儲存的不是「事實」，而是「如何做」。每個 Skill Genome 是一個有版本、有信心分數、會自我進化的技能個體。

---

## 為什麼叫 Genome？

基因組（Genome）攜帶生物發展的完整指令。Skill Genome 類似——它是技能的「基因」，攜帶技能的所有重要特徵：成功率、使用次數、信心分數、標籤、以及技能本體（完整 SKILL.md 內容）。

就像基因會突變（版本更新）和被淘汰（confidence 過低），Skill Genome 也有完整生命週期：誕生 → 使用 → 自評 → 進化 or 廢棄。

---

## 資料結構

```python
@dataclass
class SkillGenome:
    # 識別
    name: str                       # 唯一 ID，如 "loom-engineer"
    version: int = 1                # 每次實質性更新 +1

    # 成效追蹤（由 SkillOutcomeTracker 更新）
    confidence: float = 0.8         # EMA 信心分數，0.0–1.0
    usage_count: int = 0            # 累積使用次數
    success_rate: float = 0.0       # EMA 成功率

    # 繼承
    parent_skill: str | None = None

    # 廢棄閾值
    deprecation_threshold: float = 0.3

    # 內容
    tags: list[str] = field(default_factory=list)
    body: str = ""                  # 完整 SKILL.md 原文（含 frontmatter）

    # 時間戳
    created_at: datetime | None = None
    updated_at: datetime | None = None
```

> **注意：** `confidence` 預設值為 0.8（v0.2.7.0 起）。Auto-import 會保留既有 confidence，首次匯入時設為 0.8 作為初始值。

---

## Agent Skills 三層漸進式披露

Skill Genome 遵循 [Agent Skills spec](https://agentskills.io/specification) 的三層披露模型：

| 層級 | 觸發時機 | 內容 |
|------|---------|------|
| **Tier 1** | Session 啟動 | `<available_skills>` XML catalog 注入 system prompt（name + description） |
| **Tier 2** | Agent 判斷任務匹配時 | `load_skill(name)` → 完整 SKILL.md body + evolution hints + 資源清單 |
| **Tier 3** | 按需 | Agent 直接讀取 scripts/ references/ assets/ 內的附屬檔案 |

### Tier 1 — 系統提示注入

每次 session 啟動，MemoryIndex 自動掃描 skills 目錄並注入：

```xml
<available_skills>
<skill>
  <name>loom-engineer</name>
  <description>Full implementation cycle from issue to PR.</description>
</skill>
<skill>
  <name>code-analyst</name>
  <description>Deep code analysis and architecture review.</description>
</skill>
</available_skills>

The skills listed above provide specialized instructions for specific tasks.
When a task matches a skill's description, call load_skill(name) to load
its full instructions before proceeding.
```

### Tier 2 — 按需載入

```
load_skill("loom-engineer")
↓
<skill_content name="loom-engineer">
[evolution_hints 如有]
[完整 SKILL.md body，已去除 frontmatter]
[skill_resources 如有]
</skill_content>
```

---

## SKILL.md 格式

技能以 Markdown 檔案定義，搭配 YAML frontmatter：

```markdown
---
name: loom-engineer
description: Full implementation cycle from issue to PR.
tags:
  - git
  - python
  - engineering
---

# Loom Engineer

當任務需要完整的開發週期時使用此技能。

## Workflow

1. 讀取 Issue 了解需求
2. 閱讀相關程式碼建立理解
3. 實作變更
4. 執行測試
5. 提交 PR

## 原則

- 先讀後改，不猜測現有行為
- 一次一個合理的 commit
```

### 目錄結構（可選）

```
skills/
└── loom-engineer/
    ├── SKILL.md           ← 必須
    ├── scripts/
    │   └── check_pr.py
    └── references/
        └── coding-guide.md
```

---

## 自動匯入機制

Session 啟動時，LoomSession 自動掃描兩個位置：

1. `<workspace>/skills/*/SKILL.md` — 專案級技能（優先）
2. `~/.loom/skills/*/SKILL.md` — 使用者級技能

**更新邏輯：**
- 若技能不存在 → 匯入，`confidence = 0.8`
- 若 mtime 或 body 有變 → 更新（version +1，保留 confidence/usage_count）
- 同名技能專案級優先（dedup by name）
- YAML 解析失敗或無 description → 跳過（不 crash）

---

## Quality-Gradient Self-Assessment

v0.2.7.0 起，Skill Genome 的 confidence 由 **質量梯度自評** 驅動，取代舊的 binary success/failure。

### 閉環流程

```
Agent 呼叫 load_skill(name)          ← Tier 2 激活
    ↓ record_activation(name, turn)
Agent 使用技能完成任務
    ↓ record_tool_usage()（每次工具呼叫）
TurnDone — _trigger_skill_assessment()
    ↓
SkillOutcomeTracker.maybe_evaluate()
    ↓ 背景 asyncio task
LLM 自評提示（1–5 分制）
    ↓ _parse_assessment()
score → EMA 更新 confidence + success_rate
    ↓ procedural.upsert()
outcome 寫入 SemanticMemory
（key: skill:<name>:outcome:<timestamp>）
```

### 自評提示

```
You just completed a task using the skill "{skill_name}".
Context: {turn_summary}

Rate your execution:
  1 = Poor    2 = Below average    3 = Adequate
  4 = Good    5 = Excellent

Respond with ONLY:
{"score": <1-5>, "summary": "<one sentence>"}
```

### Parser 容錯

實測部分模型（如 MiniMax-M2.7）會忽略 JSON-only 指令，輸出自然語言包夾 JSON。Parser 採三階段 fallback：

1. **直接 JSON parse** — 理想路徑
2. **regex `{...}` 提取** — 從混合輸出中找第一個物件
3. **鍵值 regex** — 逐一提取 `"score"` / `"summary"` 值

### EMA 更新

```python
ALPHA = 0.15

normalised = score / 5.0          # → 0.0–1.0
confidence = (1 - ALPHA) * confidence + ALPHA * normalised
success_rate = (1 - ALPHA) * success_rate + ALPHA * normalised
usage_count += 1
```

alpha=0.15 意味著新評分佔 15% 權重，歷史值佔 85%——穩定但仍對趨勢敏感。

---

## Evolution Hook — 主動進化

`SkillEvolutionHook` 在 session 結束（`stop()`）時觸發，對 confidence 低、使用次數足夠的技能生成改進建議：

**觸發條件：**
- `confidence < 0.6` AND `usage_count >= 3`

**動作：**
1. 從 SemanticMemory 查詢近期 outcome 記錄和 anti-patterns
2. 呼叫 LLM 生成 1–2 條具體改進建議
3. 寫入 SemanticMemory（key: `skill:<name>:evolution_hint:<ts>`）
4. 下次 `load_skill()` 時，hints 以 `<evolution_hints>` 標籤出現在輸出中

---

## Graceful Shutdown

`stop()` 在關閉 DB 前，會等待所有 pending `skill_eval:*` background tasks 完成（timeout=5s），確保自評結果一定落地：

```python
pending_evals = [t for t in asyncio.all_tasks()
                 if t.get_name().startswith("skill_eval:")]
if pending_evals:
    done, still_pending = await asyncio.wait(pending_evals, timeout=5.0)
```

---

## 自動廢棄

當 `confidence ≤ deprecation_threshold`（預設 0.3）時，Skill Genome 標記為廢棄：

- 不再被 `list_active()` 返回
- 不再出現在 `<available_skills>` catalog
- 仍保留在 DB 中供人工審查或手動恢復

---

## 與其他記憶的區別

| 維度 | Semantic Memory | Procedural Memory（Skill Genome）|
|------|----------------|----------------------------------|
| **儲存內容** | 事實 | 過程/方法（完整 SKILL.md） |
| **更新觸發** | Agent 主動 memorize | 自評 + auto-import mtime 檢查 |
| **Confidence 意義** | 事實可靠程度 | 技能質量分數（1–5 EMA） |
| **可見性** | 需 recall 主動搜尋 | Tier 1 自動注入 system prompt |
| **進化機制** | 無 | SkillEvolutionHook 主動生成改進建議 |

---

## Skill 健康報告（Reflection API）

```
Skill Genome Health Report
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ID                  V   Confidence   Uses   Rate    Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
loom-engineer       3   0.87 ██████  14     92%     ✅ active
code-analyst        2   0.71 █████   8      75%     ✅ active
bash-deploy         1   0.31 ▂        3     33%     ⚠️ warning
legacy-script       2   0.22 ▁        5     40%     🔴 deprecated
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 4 skills, 2 active, 1 warning, 1 deprecated
```
