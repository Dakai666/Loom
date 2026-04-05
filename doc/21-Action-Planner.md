# Action Planner

Action Planner 是 Autonomy Engine 的「決策執行層」。它將 Decision Pipeline 的決策轉換為具體的 Action 序列，並管理 Action 的執行。

---

## 決策到行動的映射

```
┌─────────────────────────────────────────────────────────────┐
│                    Decision → Action                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Decision.APPROVE ──▶ ActionPlan (行動序列)                │
│   Decision.DENY ────▶ (不行動，記錄日誌)                    │
│   Decision.CONFIRM ──▶ Action.REQUEST_CONFIRMATION         │
│   Decision.DEFER ───▶ Action.SCHEDULE_RETRY                 │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Action 結構

```python
# loom/core/autonomy/actions.py
class ActionType(Enum):
    """行動類型"""
    EXECUTE_TASK = "execute_task"
    EXECUTE_TOOL = "execute_tool"
    REQUEST_CONFIRMATION = "request_confirmation"
    SCHEDULE_RETRY = "schedule_retry"
    SEND_NOTIFICATION = "send_notification"
    WRITE_MEMORY = "write_memory"
    ABORT = "abort"

@dataclass
class Action:
    """行動單元"""
    type: ActionType
    params: dict = field(default_factory=dict)
    
    # 執行控制
    timeout: float = 300.0
    retry_count: int = 0
    max_retries: int = 3
    
    # 條件
    condition: str | None = None  # 執行條件（可選）

@dataclass
class ActionPlan:
    """行動計劃（一系列 Action）"""
    actions: list[Action]
    metadata: dict = field(default_factory=dict)
    
    @property
    def is_empty(self) -> bool:
        return len(self.actions) == 0
```

---

## ActionPlanner 核心

```python
# loom/core/autonomy/planner.py
class ActionPlanner:
    """將決策轉換為行動"""
    
    def __init__(
        self,
        task_scheduler: TaskScheduler,
        memory: MemoryStore,
        notification_router: NotificationRouter,
        tool_registry: ToolRegistry,
    ):
        self.task_scheduler = task_scheduler
        self.memory = memory
        self.notification_router = notification_router
        self.tool_registry = tool_registry
    
    def plan(
        self,
        decision: Decision,
        trigger: Trigger,
        context: dict,
    ) -> ActionPlan:
        """
        根據決策生成行動計劃
        """
        
        if decision == Decision.DENY:
            return self._plan_deny(trigger, context)
        
        elif decision == Decision.CONFIRM:
            return self._plan_confirm(trigger, context)
        
        elif decision == Decision.APPROVE:
            return self._plan_approve(trigger, context)
        
        elif decision == Decision.DEFER:
            return self._plan_defer(trigger, context)
        
        return ActionPlan(actions=[])
```

---

## Trust Level → Decision 映射

### 映射表

```python
# loom/core/autonomy/trust_mapper.py
class TrustLevelMapper:
    """Trust Level 到 Decision 的映射"""
    
    @staticmethod
    def map(
        trigger: Trigger,
        context: dict,
        trust_level: TrustLevel,
    ) -> Decision:
        """
        根據 Trust Level 決定決策
        """
        
        # 1. 檢查觸發器本身的風險
        if trigger.risk_level == RiskLevel.CRITICAL:
            if trust_level == TrustLevel.SAFE:
                return Decision.DENY
            elif trust_level == TrustLevel.GUARDED:
                return Decision.CONFIRM
        
        # 2. 檢查是否需要寫入
        if trigger.requires_write:
            if trust_level == TrustLevel.SAFE:
                return Decision.CONFIRM
        
        # 3. 檢查自訂規則
        risk_score = TrustLevelMapper._calculate_risk_score(trigger, context)
        
        if risk_score > 0.8:
            return Decision.CONFIRM
        elif risk_score > 0.5:
            return Decision.APPROVE  # 允許執行但記錄
        else:
            return Decision.APPROVE
    
    @staticmethod
    def _calculate_risk_score(trigger: Trigger, context: dict) -> float:
        """計算風險分數（0-1）"""
        score = 0.0
        
        # 基礎風險（來自觸發器類型）
        score += {
            RiskLevel.LOW: 0.1,
            RiskLevel.MEDIUM: 0.3,
            RiskLevel.HIGH: 0.6,
            RiskLevel.CRITICAL: 0.9,
        }[trigger.risk_level]
        
        # 寫入操作加成
        if trigger.requires_write:
            score += 0.2
        
        # 金額/資料量加成（如果上下文包含）
        if context.get("amount", 0) > 10000:
            score += 0.2
        
        return min(score, 1.0)
```

### 詳細映射邏輯

| Trust Level | 低風險觸發器 | 中風險觸發器 | 高風險觸發器 | 寫入操作 |
|-------------|-------------|-------------|-------------|----------|
| SAFE | APPROVE | DENY | DENY | CONFIRM |
| GUARDED | APPROVE | APPROVE | CONFIRM | CONFIRM |
| CRITICAL | APPROVE | APPROVE | APPROVE | APPROVE |

---

## 行動計劃生成

### APPROVE → 執行行動

```python
def _plan_approve(self, trigger: Trigger, context: dict) -> ActionPlan:
    """當決策是 APPROVE 時"""
    
    actions = []
    
    # 1. 根據觸發器的 action 配置生成行動
    action_name = context.get("action", "default_action")
    
    if action_name == "send_notification":
        actions.append(Action(
            type=ActionType.SEND_NOTIFICATION,
            params={
                "message": self._build_notification_message(trigger, context),
                "channel": context.get("channel", "default"),
            }
        ))
    
    elif action_name == "run_tests":
        actions.append(Action(
            type=ActionType.EXECUTE_TASK,
            params={
                "task": "run_unit_tests",
                "args": context.get("test_args", {}),
            }
        ))
    
    elif action_name == "fetch_data":
        actions.append(Action(
            type=ActionType.EXECUTE_TOOL,
            params={
                "tool": "web_search",
                "args": {"query": context.get("query", "")},
            }
        ))
        actions.append(Action(
            type=ActionType.WRITE_MEMORY,
            params={
                "memory_type": "semantic",
                "key": f"fetched_{datetime.now().isoformat()}",
                "value": "{{previous_result}}",  # 引用前一個 action 的結果
            }
        ))
    
    # 預設行動：執行與觸發器關聯的任務
    else:
        actions.append(Action(
            type=ActionType.EXECUTE_TASK,
            params={"trigger_id": trigger.id}
        ))
    
    # 2. 總是最後發送通知（結果通知）
    actions.append(Action(
        type=ActionType.SEND_NOTIFICATION,
        params={
            "message": f"Action completed: {action_name}",
            "channel": "log",
        }
    ))
    
    return ActionPlan(
        actions=actions,
        metadata={"decision": "APPROVE", "trigger_id": trigger.id}
    )
```

### CONFIRM → 請求確認

```python
def _plan_confirm(self, trigger: Trigger, context: dict) -> ActionPlan:
    """當決策是 CONFIRM 時，需要用戶確認"""
    
    return ActionPlan(
        actions=[
            Action(
                type=ActionType.REQUEST_CONFIRMATION,
                params={
                    "prompt": self._build_confirm_prompt(trigger, context),
                    "timeout": 300,  # 5 分鐘超時
                    "on_approve": "execute_primary_action",
                    "on_deny": "abort",
                }
            )
        ],
        metadata={
            "decision": "CONFIRM",
            "trigger_id": trigger.id,
            "requires_user_input": True,
        }
    )

def _build_confirm_prompt(self, trigger: Trigger, context: dict) -> str:
    """構建確認提示"""
    
    risk_desc = {
        RiskLevel.LOW: "低風險",
        RiskLevel.MEDIUM: "中等風險",
        RiskLevel.HIGH: "高風險",
        RiskLevel.CRITICAL: "極高風險",
    }[trigger.risk_level]
    
    return f"""
您有一個待確認的自動化行動：

**觸發器**: {trigger.id}
**風險等級**: {risk_desc}
**行動**: {context.get("action", "unknown")}

**上下文**:
{self._format_context(context)}

是否允許執行？
"""
```

### DENY → 記錄日誌

```python
def _plan_deny(self, trigger: Trigger, context: dict) -> ActionPlan:
    """當決策是 DENY 時"""
    
    return ActionPlan(
        actions=[
            Action(
                type=ActionType.SEND_NOTIFICATION,
                params={
                    "message": f"Action denied: {trigger.id}",
                    "channel": "log",
                }
            )
        ],
        metadata={
            "decision": "DENY",
            "trigger_id": trigger.id,
            "reason": "Trust level restriction",
        }
    )
```

### DEFER → 延後執行

```python
def _plan_defer(self, trigger: Trigger, context: dict) -> ActionPlan:
    """當決策是 DEFER 時"""
    
    # 計算延遲時間
    delay = self._calculate_defer_delay(trigger)
    
    return ActionPlan(
        actions=[
            Action(
                type=ActionType.SCHEDULE_RETRY,
                params={
                    "trigger_id": trigger.id,
                    "context": context,
                    "delay_seconds": delay,
                    "max_retries": 3,
                }
            )
        ],
        metadata={
            "decision": "DEFER",
            "trigger_id": trigger.id,
            "delay_seconds": delay,
        }
    )

def _calculate_defer_delay(self, trigger: Trigger) -> float:
    """計算延遲時間"""
    
    # 根據觸發器類型決定延遲
    if isinstance(trigger, CronTrigger):
        # Cron 觸發器延遲到下一個時間點
        return 3600  # 1 小時
    
    elif isinstance(trigger, ConditionTrigger):
        # 條件觸發器延遲到下次檢查
        return trigger.check_interval * 2
    
    else:
        return 300  # 預設 5 分鐘
```

---

## Action 執行器

```python
# loom/core/autonomy/action_executor.py
class ActionExecutor:
    """執行 Action"""
    
    def __init__(
        self,
        task_scheduler: TaskScheduler,
        memory: MemoryStore,
        notification_router: NotificationRouter,
        tool_registry: ToolRegistry,
    ):
        self.task_scheduler = task_scheduler
        self.memory = memory
        self.notification_router = notification_router
        self.tool_registry = tool_registry
    
    async def execute(self, plan: ActionPlan) -> ExecutionResult:
        """執行行動計劃"""
        
        results = []
        
        for action in plan.actions:
            result = await self._execute_action(action)
            results.append(result)
            
            # 如果 action 失敗且不可重試，停止執行
            if not result.success and action.retry_count >= action.max_retries:
                break
        
        return ExecutionResult(
            plan=plan,
            action_results=results,
            overall_success=all(r.success for r in results),
        )
    
    async def _execute_action(self, action: Action) -> ActionResult:
        """執行單個 Action"""
        
        try:
            if action.type == ActionType.EXECUTE_TASK:
                return await self._execute_task(action)
            
            elif action.type == ActionType.EXECUTE_TOOL:
                return await self._execute_tool(action)
            
            elif action.type == ActionType.REQUEST_CONFIRMATION:
                return await self._request_confirmation(action)
            
            elif action.type == ActionType.SEND_NOTIFICATION:
                return await self._send_notification(action)
            
            elif action.type == ActionType.WRITE_MEMORY:
                return await self._write_memory(action)
            
            elif action.type == ActionType.SCHEDULE_RETRY:
                return await self._schedule_retry(action)
            
            else:
                return ActionResult(success=False, error=f"Unknown action type: {action.type}")
        
        except Exception as e:
            return ActionResult(success=False, error=str(e))
```

---

## 與 ConfirmFlow 的整合

當 Action 是 `REQUEST_CONFIRMATION` 時，會觸發 ConfirmFlow：

```python
async def _request_confirmation(self, action: Action) -> ActionResult:
    """請求用戶確認"""
    
    from loom.core.notification.confirm_flow import ConfirmFlow
    
    flow = ConfirmFlow(
        prompt=action.params["prompt"],
        timeout=action.params.get("timeout", 300),
    )
    
    result = await flow.run()
    
    if result == ConfirmResult.APPROVED:
        # 用戶批准，執行主要行動
        if action.params.get("on_approve") == "execute_primary_action":
            # 這裡會遞迴執行主要行動
            return ActionResult(success=True, output="confirmed")
    
    return ActionResult(success=False, error="User denied or timeout")
```

詳見 [25-ConfirmFlow.md](25-ConfirmFlow.md)。

---

## loom.toml 配置

```toml
[autonomy.action_planner]

# 預設超時
default_timeout = 300

# 預設重試次數
default_max_retries = 3

# 風險閾值
risk_threshold_low = 0.2
risk_threshold_medium = 0.5
risk_threshold_high = 0.8

# 確認提示模板
[autonomy.action_planner.confirm_template]
title = "⚠️ 待確認的自動化行動"
footer = "回覆 APPROVE/DENY"
```

---

## 總結

Action Planner 是 Autonomy Engine 的「決策轉換器」：

| 功能 | 說明 |
|------|------|
| Decision → Action | 將決策轉換為具體行動序列 |
| Trust Level 映射 | 根據 trust_level 決定允許/拒絕/確認 |
| 行動生成 | 根據 trigger 和 context 生成對應的 Action |
| Action 執行 | 支援 task/tool/notification/memory 等行動類型 |
| ConfirmFlow 整合 | 高風險行動自動請求用戶確認 |
