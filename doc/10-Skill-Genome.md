# SKILL.md 格式規範

> v0.3.6.0+ 對應：`loom_engineer` + `systematic_code_analyst` → `code_weaver` 合併（Issue #225）

---

## SKILL.md Frontmatter Schema

每個 SKILL.md 檔案頂部有 YAML frontmatter（`---` 包圍）：

```yaml
---
name: code_weaver
description: "系統化程式碼分析、工程實作、PR 審查與資安審查..."
tags: [core, code, review, security]
precondition_checks:
  - ref: checks.reject_write_operations
    applies_to: [write_file]
    description: "分析技能為唯讀模式，禁止寫入任何檔案"
maturity_tag: mature  # optional
---
```

### 欄位說明

| 欄位 | 類型 | 說明 |
|------|------|------|
| `name` | str | 技能名稱（與目錄名相同）|
| `description` | str | 一句話觸發描述 |
| `tags` | list[str] | 技能分類標籤 |
| `precondition_checks` | list[dict] | Issue #64：技能聲明的執行前置條件 |
| `maturity_tag` | str | 成熟度標籤（`mature` / `needs_improvement` / null）|

### precondition_checks 格式（Issue #64 Phase B）

```yaml
precondition_checks:
  - ref: checks.fn_name        # 解析為 checks 模組中的函數
    applies_to: [tool_name]   # 套用到哪些工具（可多個）
    description: "為何這個檢查必要"
```

---

## Skills 目錄結構

```
skills/
├── code_weaver/              ← loom_engineer + systematic_code_analyst 合併（v0.3.6.0）
├── task_list/
├── memory_hygiene/
├── meta-skill-engineer/
├── async_jobs/
├── audio_transcriber/
├── deep_researcher/
├── github_cli/               ← deprecated，見 opencli
├── opencli/
├── pdf/
├── pet-cat/
├── remotion/
├── security_assessment/
├── silky_chatgpt_draw/       ← 新增（GPT-Image-1 web 繪圖）
├── silky_hyperframes/        ← 新增（HTML + GSAP 影片創作）
├── silky_minimax_draw/       ← 新增（MiniMax 雲端圖像）
├── silky_tts/                ← 新增（TTS 語音合成）
├── sisi_mood_tarot/          ← 含 mood_frontmatter，觸發 Mood Tarot
├── suno_music_creator/
├── news-aggregator/          ← 新增
└── skill_novel_writer.md     ← 單檔（非目錄）
```

> `loom_engineer` 與 `systematic_code_analyst` 已於 v0.3.6.0 合併為 `code_weaver`（PR #225）。

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

*v0.3.6.2 | 2026-05-06*
