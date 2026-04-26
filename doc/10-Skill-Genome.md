# SKILL.md 格式規範（增量更新）

> 對 [doc/10-Skill-Genome.md](doc/10-Skill-Genome.md) 的增量更新，補充完整 frontmatter schema 與 SKILL.md 目錄結構。

---

## SKILL.md Frontmatter Schema

每個 SKILL.md 檔案頂部有 YAML frontmatter（`---` 包圍）：

```yaml
---
name: systematic_code_analyst
description: "系統化程式碼分析技能..."
tags: [core, planning, tracking]
precondition_checks:
  - ref: checks.reject_write_operations
    applies_to: [write_file]
    description: "分析技能為唯讀模式，禁止寫入任何檔案"
maturity_tag: mature  #  optional
---
```

### 欄位說明

| 欄位 | 類型 | 說明 |
|------|------|------|
| `name` | str | 技能名稱（通常與目錄名相同）|
| `description` | str | 一句話觸發描述 |
| `tags` | list[str] | 技能分類標籤（無需嚴格管理）|
| `precondition_checks` | list[dict] | **Issue #64**：技能聲明的執行前置條件 |
| `maturity_tag` | str | 成熟度標籤（`mature` / `needs_improvement` / null）|

### precondition_checks 格式（Issue #64 Phase B）

```yaml
precondition_checks:
  - ref: checks.fn_name        # 解析為 checks 模組中的函數
    applies_to: [tool_name]   # 套用到哪些工具（可多個）
    description: "為何這個檢查必要"
```

`applies_to` 內的工具，在 `load_skill()` 時會動態將 `precondition_checks` 附加到對應的 `ToolDefinition`。

---

## 完整 Skills 目錄結構

```
skills/
├── task_list/
│   └── SKILL.md            ← 純 frontmatter + Markdown 內容
├── systematic_code_analyst/
│   └── SKILL.md            ← 含 precondition_checks
├── loom_engineer/
├── meta-skill-engineer/
├── memory_hygiene/
├── async_jobs/
├── audio_transcriber/
├── deep_researcher/
├── github_cli/
├── opencli/
├── pdf/
├── pet-cat/
├── remotion/
├── security_assessment/
├── silky_minimax_draw/
├── silky_tts/
├── sisi_mood_tarot/
│   └── SKILL.md            ← 含 mood_frontmatter，觸發 Mood Tarot
└── suno_music_creator/
```

技能本身是**純文字**：frontmatter 結構化 + Markdown 內容。沒有附屬 Python 檔案必要。

---

## SkillGenome 與 SKILL.md 的對照

| SkillGenome 欄位 | 來源 |
|----------------|------|
| `name` | frontmatter `name` |
| `body` | 整份 SKILL.md（frontmatter + Markdown）|
| `version` | SkillGenome 自行管理（promote/rollback 時遞增）|
| `confidence` | EMA 追蹤（`SkillOutcomeTracker` quality-gradient）|
| `usage_count` | SkillOutcomeTracker 記錄 |
| `deprecation_threshold` | 固定 0.3 |
| `tags` | frontmatter `tags` |
| `precondition_check_refs` | frontmatter `precondition_checks` |
| `maturity_tag` | frontmatter `maturity_tag` |

`body` 包含完整的 frontmatter，這樣 LLM rewrite（SkillMutator）時保有完整上下文。

---

## SkillOutcomeTracker（EMA Confidence 追蹤）

舊版 `SkillGenome.record_outcome()` 已 deprecated。現實使用 `SkillOutcomeTracker` 的 quality-gradient 機制：

```python
@dataclass
class SkillOutcome:
    skill_name: str
    task_type: str
    quality_score: float   # 1.0–5.0
    outcome: str           # "success" / "partial" / "failure"
    timestamp: datetime
```

每次 `load_skill()` 時，Session 掛鉤 `TaskReflector` 的診斷結果寫入 `skill_outcomes` 表。

`SkillGenome.confidence` 由 `SkillOutcomeTracker` 根據 quality_score 的 EMA 計算，SkillGenome 本身不直接計算。

---

*增量更新 | 2026-04-26 03:21 Asia/Taipei*
