# Reflection API

Reflection API 是 Loom 的「自我反思」機制。它在每個 session 結束時執行分析，生成摘要、工具報告和健康狀態報告，讓 agent 的經驗得以累積和改進。

---

## 什麼是 Reflection？

Reflection 不是簡單的「總結」。它是多維度的分析：

```
┌─────────────────────────────────────────────────────────────┐
│                     Reflection 分析維度                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐         │
│   │   Summary   │  │ Tool Report │  │   Health    │         │
│   │   會話摘要  │  │  工具報告   │  │   健康報告  │         │
│   └─────────────┘  └─────────────┘  └─────────────┘         │
│         │                │                │                 │
│         ▼                ▼                ▼                 │
│   Episodic Memory   Skill Genome    Notification          │
│   （寫入）           （更新 confidence）  （觸發警告）         │
│                                                             │
│   ┌─────────────┐  ┌─────────────┐                         │
│   │ Counter-    │  │ Self-      │                         │
│   │ factual     │  │ Reflection │                         │
│   │ 反事實反思  │  │ 自我反思   │                         │
│   └─────────────┘  └─────────────┘                         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 三種 Report

### 1. Session Summary（會話摘要）

**目的**：壓縮 session 的核心內容，供未來快速回顧。

```python
@dataclass
class SessionSummary:
    """Session 摘要"""
    
    session_id: str
    duration_seconds: int
    
    # 核心內容
    topics_discussed: list[str]      # 討論的主題
    decisions_made: list[str]        # 做出的決定
    tasks_completed: list[str]       # 完成的任務
    tasks_pending: list[str]          # 未完成的任務
    
    # 用戶偏好（如有觀察到）
    user_preferences: list[str]      # 觀察到的用戶偏好
    
    # 摘要文字（用於快速閱讀）
    summary_text: str
    
    # 原始 token 消耗
    tokens_used: int
    compression_ratio: float         # 摘要 / 原始 比例
```

**生成邏輯**：

```python
async def generate_summary(
    self,
    history: list[dict],
    memory: MemoryStore,
) -> SessionSummary:
    # 1. 從 history 提取事實
    topics = self._extract_topics(history)
    decisions = self._extract_decisions(history)
    
    # 2. 計算 token 消耗
    original_tokens = self._count_tokens(history)
    
    # 3. 生成摘要文字（呼叫 LLM）
    summary_text = await self._llm_summarize(history)
    
    # 4. 計算壓縮比
    summary_tokens = self._count_tokens(summary_text)
    compression_ratio = summary_tokens / original_tokens if original_tokens > 0 else 1.0
    
    return SessionSummary(
        session_id=self.session_id,
        duration_seconds=self._get_duration(),
        topics_discussed=topics,
        decisions_made=decisions,
        tasks_completed=self._extract_completed_tasks(history),
        tasks_pending=self._extract_pending_tasks(history),
        user_preferences=self._infer_preferences(history),
        summary_text=summary_text,
        tokens_used=original_tokens,
        compression_ratio=compression_ratio,
    )
```

**寫入 Episodic Memory**：

```python
async def persist_summary(self, summary: SessionSummary):
    await self.memory.write_episode(
        key=f"session_{summary.session_id}",
        value=summary.summary_text,
        metadata={
            "type": "session_summary",
            "topics": summary.topics_discussed,
            "decisions": summary.decisions_made,
            "tokens_used": summary.tokens_used,
            "compression_ratio": summary.compression_ratio,
        }
    )
```

---

### 2. Tool Report（工具報告）

**目的**：追蹤每個工具的使用情況，更新 Skill Genome 的 confidence。

```python
@dataclass
class ToolReport:
    """工具使用報告"""
    
    tool_name: str
    call_count: int                  # 呼叫次數
    success_count: int               # 成功次數
    failure_count: int               # 失敗次數
    average_latency_ms: float        # 平均延遲
    total_latency_ms: float          # 總延遲
    
    # 錯誤分析
    error_types: dict[str, int]      # 各類錯誤的次數
    last_error: str | None           # 最後一次錯誤
    
    # 趨勢（相對於上次 session）
    success_rate_delta: float         # 成功率變化
    call_count_delta: int            # 呼叫次數變化
```

**生成邏輯**：

```python
async def generate_tool_report(
    self,
    tool_calls: list[ToolCallRecord],
) -> dict[str, ToolReport]:
    reports = {}
    
    # 按工具分組
    grouped = defaultdict(list)
    for call in tool_calls:
        grouped[call.tool_name].append(call)
    
    for tool_name, calls in grouped.items():
        success = [c for c in calls if c.success]
        failure = [c for c in calls if not c.success]
        
        errors = {}
        for c in failure:
            errors[c.error_type] = errors.get(c.error_type, 0) + 1
        
        # 讀取歷史數據計算趨勢
        previous = await self._get_tool_history(tool_name)
        
        report = ToolReport(
            tool_name=tool_name,
            call_count=len(calls),
            success_count=len(success),
            failure_count=len(failure),
            average_latency_ms=sum(c.latency_ms for c in calls) / len(calls),
            total_latency_ms=sum(c.latency_ms for c in calls),
            error_types=errors,
            last_error=failure[-1].error_message if failure else None,
            success_rate_delta=self._calc_delta(
                len(success) / len(calls),
                previous.success_rate if previous else 0
            ),
            call_count_delta=len(calls) - (previous.call_count if previous else 0),
        )
        
        reports[tool_name] = report
    
    return reports
```

**寫入 Skill Genome**：

```python
async def update_skill_genomes(self, reports: dict[str, ToolReport]):
    for tool_name, report in reports.items():
        # 讀取現有 genome
        genome = await self.memory.get_skill_genome(tool_name)
        
        if genome:
            # EMA 更新 confidence
            new_confidence = self._ema_update(
                genome.confidence,
                1.0 if report.success_count > 0 else 0.0,
                alpha=0.3
            )
            
            await genome.update(
                confidence=new_confidence,
                call_count=genome.call_count + report.call_count,
                last_used=datetime.now(),
            )
        else:
            # 新工具，建立新 genome
            await self.memory.create_skill_genome(
                key=tool_name,
                value=f"Tool: {tool_name}",
                confidence=0.5,  # 初始 confidence
            )
```

---

### 3. Health Report（健康報告）

**目的**：識別需要關注的問題（如低 confidence skills、異常的對話長度等）。

```python
@dataclass
class HealthReport:
    """健康狀態報告"""
    
    # 記憶健康
    low_confidence_skills: list[SkillAlert]
    stale_episodes: list[str]           # 30+ 天未更新的 episodes
    empty_semantic: bool                # 是否有空的 semantic memory
    
    # 對話健康
    session_too_short: bool             # session < 2 分鐘
    session_too_long: bool             # session > 60 分鐘
    high_token_usage: bool             # token 使用率 > 90%
    
    # 工具健康
    degraded_tools: list[str]          # 成功率 < 50% 的工具
    slow_tools: list[str]              # 平均延遲 > 10s 的工具
    
    # 建議
    recommendations: list[str]
    
    # 總評
    overall_status: Literal["healthy", "warning", "critical"]
```

**生成邏輯**：

```python
async def generate_health_report(
    self,
    memory: MemoryStore,
    budget: ContextBudget,
    tool_reports: dict[str, ToolReport],
) -> HealthReport:
    alerts = []
    recommendations = []
    
    # 1. 檢查低 confidence skills
    low_conf = await memory.get_skills_where("confidence < 0.3")
    for skill in low_conf:
        alerts.append(SkillAlert(
            skill_key=skill.key,
            confidence=skill.confidence,
            last_used=skill.last_used,
            suggestion="考慮重新訓練或停用"
        ))
        recommendations.append(
            f"Skill '{skill.key}' confidence 過低 ({skill.confidence:.2f})，"
            f"建議審視或停用"
        )
    
    # 2. 檢查陳舊的 episodes
    stale = await memory.get_episodes_where(
        "last_accessed < datetime('now', '-30 days')"
    )
    if stale:
        recommendations.append(
            f"{len(stale)} 個 episodes 超過 30 天未訪問，"
            f"考慮清理或重新整合"
        )
    
    # 3. 檢查 token 使用
    if budget.used_ratio > 0.9:
        recommendations.append(
            f"Token 使用率過高 ({budget.used_ratio:.1%})，"
            f"考慮增加 session 頻率或壓縮歷史"
        )
    
    # 4. 檢查工具健康
    degraded = [
        tool for tool, report in tool_reports.items()
        if report.failure_count / report.call_count > 0.5
    ]
    slow = [
        tool for tool, report in tool_reports.items()
        if report.average_latency_ms > 10_000
    ]
    
    # 5. 總評
    if alerts or degraded or budget.used_ratio > 0.95:
        status = "critical"
    elif recommendations or slow:
        status = "warning"
    else:
        status = "healthy"
    
    return HealthReport(
        low_confidence_skills=alerts,
        stale_episodes=[e.key for e in stale],
        empty_semantic=False,
        session_too_short=self._duration < 120,
        session_too_long=self._duration > 3600,
        high_token_usage=budget.used_ratio > 0.9,
        degraded_tools=degraded,
        slow_tools=slow,
        recommendations=recommendations,
        overall_status=status,
    )
```

---

## Counter-factual Reflection（v0.2.5.1）

當工具執行失敗且該工具有 SkillGenome 記錄時，觸發反事實反思（非同步 fire-and-forget）：

```
execution_error 發生
    ↓
LLM 提問：「什麼 pattern 導致了這個失敗？下次應避免什麼？」
    ↓
寫入 SemanticMemory：  key = "skill:<name>:anti_pattern:<timestamp>"
                       value = "[Anti-pattern] <LLM 分析內容>"
                       source = "reflection"
寫入 RelationalMemory：(skill:<name>, has_anti_pattern, <分析>)
                       (loom-self, should_avoid:<tool_name>, <行為>)
```

**觸發條件：**
- `execution_error` 類型的失敗（工具執行時拋出異常）
- 該工具有 SkillGenome 記錄

**寫入位置：**
- SemanticMemory — `skill:<name>:anti_pattern:<timestamp>` key
- RelationalMemory — `(skill:<name>, has_anti_pattern, …)` 和 `(loom-self, should_avoid:<tool_name>, …)` 三元組

**Session 開始時：**
`MemoryIndex` 讀取 `should_avoid` 三元組，agent 在每次對話開始就知道自己過去踩過的坑。

> 反思失敗完全 non-fatal，不會阻斷任何流程。

---

## Self-Reflection（v0.2.5.3）

詳見 [19-Autonomy-概述.md](19-Autonomy-概述.md) 和 [22-Autonomy-Daemon.md](22-Autonomy-Daemon.md)。

由 `TaskReflector`（Issue #120 PR 1）在每次結構化診斷後自動呼叫 `run_self_reflection`，產出 `loom-self` 三元組。

> **變更**：`SelfReflectionPlugin` 與 `reflect_self` 工具已於 Issue #120 PR 1 移除，`run_self_reflection` 核心邏輯保留於 `loom/autonomy/self_reflection.py`。

---

## Reflection Pipeline

### 觸發時機

```python
# loom/core/cognition/reflection.py
class ReflectionPipeline:
    def __init__(
        self,
        memory: MemoryStore,
        llm: LLMProvider,
        budget: ContextBudget,
    ):
        self.memory = memory
        self.llm = llm
        self.budget = budget
    
    async def run(
        self,
        history: list[dict],
        tool_calls: list[ToolCallRecord],
        session_id: str,
    ) -> ReflectionResult:
        # 1. 生成 Session Summary
        summary = await self._generate_summary(history, session_id)
        
        # 2. 生成 Tool Reports
        tool_reports = await self._generate_tool_reports(tool_calls)
        
        # 3. 生成 Health Report
        health = await self._generate_health_report(tool_reports)
        
        # 4. 寫入 Memory
        await self._persist(summary, tool_reports, health)
        
        # 5. 發送通知（如有必要）
        if health.overall_status != "healthy":
            await self._notify(health)
        
        return ReflectionResult(
            summary=summary,
            tool_reports=tool_reports,
            health=health,
        )
```

### 手動觸發

```bash
# CLI 觸發
loom reflect

# 指定 session
loom reflect --session abc123

# 輸出格式
loom reflect --format json
```

---

## 與其他模組的整合

```
┌─────────────────────────────────────────────────────────────┐
│                    Reflection Pipeline                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Input                                                     │
│   ├── history (from Session)                               │
│   ├── tool_calls (from Harness)                            │
│   └── memory (from Memory Layer)                          │
│                                                             │
│   ├──▶ Summary Generator                                   │
│   │      └──▶ Episodic Memory (write)                     │
│   │                                                           │
│   ├──▶ Tool Report Generator                               │
│   │      └──▶ Skill Genome (update confidence)             │
│   │                                                           │
│   ├──▶ Health Report Generator                             │
│   │      └──▶ Notification (if critical)                   │
│   │                                                           │
│   ├──▶ Counter-factual Reflection                          │
│   │      ├──▶ SemanticMemory (anti_pattern keys)           │
│   │      └──▶ RelationalMemory (should_avoid triples)     │
│   │                                                           │
│   └──▶ Self-Reflection (via TaskReflector post-hook)      │
│          └──▶ RelationalMemory (loom-self triples)          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 總結

Reflection API 讓 Loom 每次 session 後都「反省」：

| Report | 產出 | 寫入 |
|--------|------|------|
| Session Summary | 對話壓縮摘要 | Episodic Memory |
| Tool Report | 每個工具的成功率 | Skill Genome |
| Health Report | 問題預警 | Notification |
| Counter-factual | Anti-pattern 分析 | Semantic + Relational Memory |
| Self-Reflection | loom-self 行為觀察 | Relational Memory |

這確保了 Loom 的「經驗」得以累積，而不是每次 session 都是獨立的。
