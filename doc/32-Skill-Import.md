# Skill Import

Skill Import 讓 Loom 在 session 啟動時自動發現並匯入 SKILL.md 技能檔案，無需手動執行匯入命令。

---

## 設計原則

v0.2.7.0 起，Skill Import 改為 **session 啟動時自動掃描**，取代舊的手動 `loom import` 流程。核心設計原則：

- **零摩擦**：放一個 SKILL.md 進 skills 目錄，下次 `loom chat` 就能用
- **冪等**：同一個 SKILL.md 多次啟動不會重複匯入，只在內容有變時更新
- **漸進式披露**：技能不在 context 中全量展開，只有被呼叫時才注入完整內容

---

## 掃描位置與優先順序

```
Session 啟動
    ↓
掃描 <workspace>/skills/*/SKILL.md    ← 專案級（優先）
    ↓
掃描 ~/.loom/skills/*/SKILL.md        ← 使用者級
    ↓
同名技能：專案級優先，使用者級跳過
```

### 目錄結構範例

```
my-project/
└── skills/
    ├── loom-engineer/
    │   ├── SKILL.md              ← 必須
    │   ├── scripts/
    │   │   └── check_pr.py       ← Tier 3 資源
    │   └── references/
    │       └── coding-guide.md
    └── code-analyst/
        └── SKILL.md

~/.loom/skills/
└── my-personal-skill/
    └── SKILL.md
```

---

## SKILL.md 格式

### 必要結構

```markdown
---
name: loom-engineer
description: Full implementation cycle from issue to PR.
tags:
  - git
  - python
---

# Loom Engineer

技能正文從這裡開始...
```

| 欄位 | 必填 | 說明 |
|------|------|------|
| `name` | 建議填寫 | 唯一識別符；未填時 fallback 至目錄名 |
| `description` | **必填** | 無 description 則跳過（不 crash）|
| `tags` | 選填 | 字串列表或逗號分隔字串皆可 |

### Frontmatter 解析規則

- 採 lenient validation：格式錯誤的 YAML 跳過，不阻擋其他技能匯入
- `name` 含 `-` 或 `_` 均可，`load_skill()` 呼叫時兩者皆能命中
- Description 中的 `:` 可能導致 YAML 解析失敗，建議加引號包覆

---

## Auto-Import 流程

```
讀取 SKILL.md
    ↓
_parse_skill_frontmatter()
    ├─ 解析成功 → (name, description, tags)
    └─ 失敗 → 跳過（logger.debug）

檢查 ProceduralMemory 是否已有同名技能
    ├─ 不存在 → 建立 SkillGenome（confidence=0.8）
    ├─ 存在 + mtime 或 body 有變 → 更新（version+1，保留 confidence/usage_count）
    └─ 存在 + 未變 → 跳過

加入 SkillCatalogEntry → Tier 1 披露
```

### 更新判斷邏輯

```python
needs_update = (
    existing is None
    or (existing.updated_at and existing.updated_at.timestamp() < file_mtime)
    or existing.body != raw
)
```

---

## 三層漸進式披露（Agent Skills Spec）

### Tier 1 — 系統提示（自動）

每次 session 啟動後，LLM 的 system prompt 包含：

```xml
<available_skills>
<skill>
  <name>loom-engineer</name>
  <description>Full implementation cycle from issue to PR.</description>
</skill>
</available_skills>

When a task matches a skill's description, call load_skill(name) to load
its full instructions before proceeding.
```

LLM 看到 skill 名稱和描述，知道何時該呼叫 `load_skill()`。

### Tier 2 — 按需載入（load_skill tool）

```
load_skill("loom-engineer")
↓
<skill_content name="loom-engineer">
<evolution_hints>                    ← 若 confidence < 0.6 且 usage >= 3
  ⚠ 考慮改進 step 3 的驗證邏輯
</evolution_hints>

# Loom Engineer
[完整 SKILL.md body，去除 frontmatter]

Skill directory: /path/to/skills/loom-engineer
<skill_resources>
  <file>scripts/check_pr.py</file>
  <file>references/coding-guide.md</file>
</skill_resources>
</skill_content>
```

**特性：**
- Session 級 dedup：同一 skill 在同一 session 只注入一次，第二次呼叫返回簡短提示
- Hyphen/underscore 正規化：`loom-engineer` 和 `loom_engineer` 皆可命中
- 找不到 skill 時，列出可用 skill 名稱作為提示

### Tier 3 — 附屬資源（Agent 按需讀取）

`<skill_resources>` 中列出的檔案需要 Agent 主動使用 `read_file` 讀取。路徑相對於 skill 目錄。

---

## Skill Self-Assessment 演化閉環

Auto-import 讓技能進入系統，Self-Assessment 讓技能的 confidence 真正反映使用品質：

```
load_skill() 呼叫
    ↓ record_activation(name, turn_index)
使用技能完成任務
    ↓ record_tool_usage()（每次工具呼叫計數）
TurnDone
    ↓ _trigger_skill_assessment()
        ↓ maybe_evaluate()（背景 task）
            ↓ LLM 自評（1–5 分）
            ↓ _parse_assessment()（三階段容錯解析）
            ↓ EMA 更新 confidence（alpha=0.15）
            ↓ outcome 寫入 SemanticMemory
```

Session 結束時，`SkillEvolutionHook` 自動分析低信心技能並寫入改進建議，下次 `load_skill()` 時以 `<evolution_hints>` 呈現。

---

## 從外部格式匯入（Hermes / OpenAI Tools）

舊版 `loom import` CLI 仍保留，支援外部格式的一次性匯入：

```bash
loom import skills.json                          # Hermes format
loom import tools.json --lens openai_tools
loom import skills.json --dry-run --min-confidence 0.7
```

外部匯入的技能存入 ProceduralMemory 後，若技能目錄下沒有對應的 SKILL.md，不會出現在 Tier 1 catalog（`<available_skills>`）。建議：從外部匯入後，在 skills 目錄建立對應的 SKILL.md，以獲得完整的三層披露支援。

---

## loom.toml 相關設定

```toml
[memory]
skill_deprecation_threshold = 0.3   # confidence 低於此值自動廢棄
```

skills 目錄路徑目前固定為 `<workspace>/skills/` 和 `~/.loom/skills/`，不需額外配置。

---

## 完整生命週期一覽

```
SKILL.md 建立
    ↓ session 啟動 auto-import
SkillGenome（SQLite ProceduralMemory）
    ↓ Tier 1 — <available_skills> XML
LLM 看到 name + description
    ↓ 判斷任務匹配 → load_skill()
Tier 2 — 完整 body + evolution hints
    ↓ Agent 按技能 workflow 執行任務
TurnDone → LLM 自評（1–5）
    ↓ EMA 更新 confidence
session stop → SkillEvolutionHook
    ↓ confidence < 0.6 → 生成改進建議
    ↓ 寫入 SemanticMemory
下次 load_skill() → <evolution_hints>
    ↓ 技能內容自我改進
    ↓ confidence 恢復或持續降低
confidence ≤ 0.3 → 廢棄（is_deprecated）
```
