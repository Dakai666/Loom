# Skill Import（增量更新）

> 對 [doc/32-Skill-Import.md](doc/32-Skill-Import.md) 的增量更新，補充 SkillOutcomeTracker。

---

## SkillOutcomeTracker — 實際的 EMA 追蹤

`SkillEvolutionHook` 並非獨立模組。`TaskReflector` 的 `TaskDiagnostic` 是實際的自評觸發點，寫入 `skill_outcomes` 表：

```python
@dataclass
class SkillOutcome:
    skill_name: str
    task_type: str
    quality_score: float    # 1.0–5.0
    outcome: str            # "success" / "partial" / "failure"
    timestamp: datetime
```

Session 結束時，outcomes 由 `SkillOutcomeTracker` 匯聚，計算 EMA confidence。

---

*增量更新 | 2026-04-26 03:21 Asia/Taipei*
