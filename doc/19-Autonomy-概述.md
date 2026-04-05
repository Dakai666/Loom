# Autonomy Engine 概述

Autonomy Engine 是 Loom 的「自主行動引擎」。它讓 Loom 不只是回應用戶請求，還能根據觸發條件自動執行任務。

---

## 核心問題

傳統的 AI Agent 是「被動」的——用戶問什麼，它回答什麼。

Loom 的 Autonomy Engine 讓它「主動」——在滿足特定條件時自動行動：

| 模式 | 觸發者 | 例子 |
|------|--------|------|
| **被動模式** | 用戶請求 | 「幫我查天氣」 |
| **主動模式** | 觸發條件 | 「每天早上 8 點自動查天氣並通知我」 |

---

## 三大觸發器

```
┌─────────────────────────────────────────────────────────────┐
│                    Autonomy Engine                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│   │  CronTrigger │  │ EventTrigger │  │ConditionTrigger│   │
│   │  定時觸�發   │  │  事件觸發    │  │  條件觸發   │    │
│   └──────────────┘  └──────────────┘  └──────────────┘    │
│          │                 │                  │             │
│          └─────────────────┼──────────────────┘             │
│                            │                                │
│                            ▼                                │
│                   ┌──────────────┐                         │
│                   │DecisionPipeline│                        │
│                   │   決策管道    │                         │
│                   └──────────────┘                         │
│                            │                                │
│                            ▼                                │
│                   ┌──────────────┐                         │
│                   │  ActionPlanner │                        │
│                   │   行動規劃    │                         │
│                   └──────────────┘                         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

詳見 [20-觸發器詳解.md](20-觸發器詳解.md)。

---

## Decision Pipeline

### 決策流程

當觸發器觸發時，Decision Pipeline 決定「是否要行動」以及「如何行動」：

```python
# loom/core/autonomy/decision.py
class DecisionPipeline:
    """決策管道"""
    
    def __init__(
        self,
        trust_level: TrustLevel,
        memory: MemoryStore,
        notification_router: NotificationRouter,
    ):
        self.trust_level = trust_level
        self.memory = memory
        self.notification_router = notification_router
    
    async def decide(
        self,
        trigger: Trigger,
        context: dict,
    ) -> Decision:
        """
        根據觸發器和上下文做出決策
        
        Returns:
            Decision: APPROVE / DENY / CONFIRM / DEFER
        """
        
        # 1. 評估信任級別
        trust_decision = await self._evaluate_trust(trigger, context)
        
        if trust_decision == TrustDecision.BLOCK:
            return Decision.DENY
        
        # 2. 評估風險
        risk = await self._evaluate_risk(trigger, context)
        
        if risk > self._risk_threshold:
            return Decision.CONFIRM  # 需要確認
        
        # 3. 評估時機
        if not await self._is_appropriate_time(trigger):
            return Decision.DEFER  # 推遲
        
        # 4. 做出決定
        return Decision.APPROVE
```

### Trust Level 映射

```python
def _evaluate_trust(
    self,
    trigger: Trigger,
    context: dict,
) -> TrustDecision:
    """根據 Trust Level 決定是否允許"""
    
    if self.trust_level == TrustLevel.SAFE:
        # SAFE: 只允許純讀取操作
        if trigger.requires_write:
            return TrustDecision.BLOCK
        return TrustDecision.ALLOW
    
    elif self.trust_level == TrustLevel.GUARDED:
        # GUARDED: 允許讀取 + 安全的寫入
        if trigger.risk_level > RiskLevel.LOW:
            return TrustDecision.BLOCK
        return TrustDecision.ALLOW
    
    elif self.trust_level == TrustLevel.CRITICAL:
        # CRITICAL: 允許所有操作（由用戶監督）
        return TrustDecision.ALLOW
```

詳見 [21-Action-Planner.md](21-Action-Planner.md)。

---

## Action Planner

### 將 Decision 轉換為 Action

```python
# loom/core/autonomy/planner.py
class ActionPlanner:
    """將決策轉換為可執行的行動"""
    
    def plan(
        self,
        decision: Decision,
        trigger: Trigger,
        context: dict,
    ) -> list[Action] | None:
        """
        根據決策生成行動序列
        """
        
        if decision == Decision.DENY:
            return None  # 不行動
        
        elif decision == Decision.CONFIRM:
            # 需要用戶確認
            return [Action(
                type=ActionType.REQUEST_CONFIRMATION,
                prompt=self._build_confirm_prompt(trigger, context),
            )]
        
        elif decision == Decision.APPROVE:
            # 直接執行
            return self._build_action_sequence(trigger, context)
        
        elif decision == Decision.DEFER:
            # 稍後重試
            return [Action(
                type=ActionType.SCHEDULE_RETRY,
                delay=self._calculate_delay(trigger),
            )]
```

---

## Autonomy Daemon

### 常駐程式

Autonomy Daemon 是 Loom 的後台服務，負責監控觸發條件：

```python
# loom/core/autonomy/daemon.py
class AutonomyDaemon:
    """Autonomy 常駐程式"""
    
    def __init__(
        self,
        triggers: list[Trigger],
        decision_pipeline: DecisionPipeline,
        action_planner: ActionPlanner,
        scheduler: TaskScheduler,
    ):
        self.triggers = triggers
        self.decision_pipeline = decision_pipeline
        self.action_planner = action_planner
        self.scheduler = scheduler
        self._running = False
    
    async def start(self):
        """啟動 daemon"""
        self._running = True
        
        while self._running:
            # 1. 檢查所有觸發器
            for trigger in self.triggers:
                if await trigger.should_fire():
                    await self._handle_trigger(trigger)
            
            # 2. 休眠一段時間
            await asyncio.sleep(60)  # 每分鐘檢查一次
    
    async def _handle_trigger(self, trigger: Trigger):
        """處理觸發事件"""
        
        # 獲取上下文
        context = await trigger.get_context()
        
        # 決策
        decision = await self.decision_pipeline.decide(trigger, context)
        
        if decision == Decision.DENY:
            logger.info(f"Trigger {trigger.id} denied")
            return
        
        if decision == Decision.CONFIRM:
            # 發送確認請求
            actions = self.action_planner.plan(decision, trigger, context)
            for action in actions:
                await self._execute_action(action)
            return
        
        # 執行
        actions = self.action_planner.plan(decision, trigger, context)
        if actions:
            await self._execute_actions(actions)
    
    async def _execute_actions(self, actions: list[Action]):
        """執行行動"""
        for action in actions:
            await self._execute_action(action)
```

詳見 [22-Autonomy-Daemon.md](22-Autonomy-Daemon.md)。

---

## loom.toml 配置

```toml
[autonomy]

# 是否啟用 autonomy daemon
enabled = true

# 信任級別
trust_level = "GUARDED"  # SAFE / GUARDED / CRITICAL

# 決策管道設定
[autonomy.decision]
risk_threshold = 0.5     # 風險閾值
allow_defer = true      # 允許推遲
max_defer_count = 3      # 最多推遲次數

# 觸發器設定
[autonomy.triggers]

# 定時任務
[[autonomy.triggers.cron]]
id = "daily_standup"
cron = "0 9 * * *"      # 每天早上 9 點
action = "send_daily_summary"
enabled = true

# 事件觸發
[[autonomy.triggers.event]]
id = "on_file_change"
event = "file_modified"
filter = "*.py"
action = "run_tests"
enabled = false
```

---

## 觸發器與行動的組合

### 常見組合範例

| 觸發器 | 條件 | 行動 |
|--------|------|------|
| CronTrigger | 每小時 | 檢查系統狀態，發送通知 |
| EventTrigger | 檔案變更 | 執行測試 |
| EventTrigger | Git push | 部署到 staging |
| ConditionTrigger | 磁碟空間 < 10% | 發送警告 |
| ConditionTrigger | 用戶上線 | 打招呼 |

---

## 與其他模組的關係

```
┌─────────────────────────────────────────────────────────────┐
│                    Autonomy Engine                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Trigger ──▶ Decision Pipeline ──▶ Action Planner         │
│                        │                    │              │
│                        ▼                    ▼              │
│                  ┌────────────┐      ┌────────────┐        │
│                  │  Memory    │      │TaskScheduler│       │
│                  │  (讀取)    │      │  (執行)     │        │
│                  └────────────┘      └────────────┘        │
│                                             │               │
│                                             ▼               │
│                                     ┌────────────┐          │
│                                     │Notification│          │
│                                     │ (通知結果)  │          │
│                                     └────────────┘          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 安全考量

### 預設安全

Autonomy Engine 預設是保守的：

| 設定 | 預設值 | 理由 |
|------|--------|------|
| `trust_level` | `GUARDED` | 阻止高風險操作 |
| `risk_threshold` | `0.5` | 中等風險需要確認 |
| `enabled` | `true`（但需要明確配置觸發器） | 需要管理員明確啟用 |

### 用戶控制

用戶可以隨時：
- 調整 trust_level
- 停用特定觸發器
- 查看執行日誌
- 手動干預或取消行動

---

## 總結

Autonomy Engine 讓 Loom 從「被動回答」升級為「主動行動」：

| 元件 | 職責 |
|------|------|
| Trigger | 偵測條件是否滿足 |
| Decision Pipeline | 決定是否/如何行動 |
| Action Planner | 將決策轉換為具體行動 |
| Autonomy Daemon | 常駐監控觸發器狀態 |

透過 Autonomy Engine，Loom 可以成為真正的「數位助手」——不只是回答問題，而是主動幫用戶完成任務。
