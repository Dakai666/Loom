# Autonomy Daemon

Autonomy Daemon 是 Loom 的後台常駐服務。它監控觸發器條件、執行決策、並在必要時調度行動。

---

## Daemon 角色

```
┌─────────────────────────────────────────────────────────────┐
│                    Autonomy Daemon                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐  │
│   │  Trigger    │────▶│  Decision   │────▶│   Action    │  │
│   │  Monitor     │     │  Pipeline   │     │   Planner   │  │
│   └─────────────┘     └─────────────┘     └─────────────┘  │
│          │                                       │          │
│          │              ┌─────────────┐          │          │
│          └─────────────▶│   Task      │◀─────────┘          │
│                         │  Scheduler  │                     │
│                         └─────────────┘                     │
│                                │                            │
│                                ▼                            │
│                         ┌─────────────┐                     │
│                         │ Notification│                     │
│                         │   Router    │                     │
│                         └─────────────┘                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Daemon 結構

```python
# loom/core/autonomy/daemon.py
class AutonomyDaemon:
    """Autonomy 常駐程式"""
    
    def __init__(
        self,
        config: AutonomyConfig,
        trigger_registry: TriggerRegistry,
        decision_pipeline: DecisionPipeline,
        action_planner: ActionPlanner,
        task_scheduler: TaskScheduler,
        notification_router: NotificationRouter,
    ):
        self.config = config
        self.trigger_registry = trigger_registry
        self.decision_pipeline = decision_pipeline
        self.action_planner = action_planner
        self.task_scheduler = task_scheduler
        self.notification_router = notification_router
        
        self._running = False
        self._task: asyncio.Task | None = None
        
        # 統計
        self._stats = DaemonStats()
```

---

## 主循環

### 啟動與停止

```python
# loom/core/autonomy/daemon.py
class AutonomyDaemon:
    async def start(self):
        """啟動 daemon"""
        if self._running:
            logger.warning("Daemon already running")
            return
        
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        
        logger.info("Autonomy Daemon started")
        
        # 發送啟動通知
        await self.notification_router.send(
            NotificationType.INFO,
            "Autonomy Daemon started"
        )
    
    async def stop(self):
        """停止 daemon"""
        if not self._running:
            return
        
        self._running = False
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        
        logger.info("Autonomy Daemon stopped")
        
        # 發送停止通知
        await self.notification_router.send(
            NotificationType.INFO,
            "Autonomy Daemon stopped"
        )
```

### 主循環

```python
async def _run_loop(self):
    """主事件循環"""
    
    while self._running:
        try:
            # 1. 檢查所有啟用的觸發器
            triggered = await self._check_triggers()
            
            # 2. 處理觸發的事件
            for trigger in triggered:
                await self._handle_trigger(trigger)
            
            # 3. 統計報告
            self._stats.record_cycle(len(triggered))
            
            # 4. 休眠
            await asyncio.sleep(self.config.check_interval)
        
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Daemon loop error: {e}")
            self._stats.increment("errors")
            await asyncio.sleep(5)  # 錯誤後短暫休眠
```

---

## 觸發器檢查

### 檢查邏輯

```python
async def _check_triggers(self) -> list[Trigger]:
    """檢查所有觸發器，返回已觸發的列表"""
    
    enabled_triggers = self.trigger_registry.list_enabled()
    triggered = []
    
    for trigger in enabled_triggers:
        try:
            if await trigger.should_fire():
                triggered.append(trigger)
                logger.info(f"Trigger fired: {trigger.id}")
        
        except Exception as e:
            logger.error(f"Error checking trigger {trigger.id}: {e}")
            self._stats.increment("trigger_errors")
    
    return triggered
```

### 觸發器去重

為避免同一觸發器在短時間內重複觸發（如 cron 精確到分鐘但檢查更頻繁）：

```python
class TriggerCooldown:
    """觸發器冷卻機制"""
    
    def __init__(self):
        self._last_fired: dict[str, datetime] = {}
        self._cooldown_seconds: dict[str, float] = {}
    
    def should_fire(self, trigger_id: str, min_interval: float = 60) -> bool:
        """檢查是否在冷卻中"""
        last = self._last_fired.get(trigger_id)
        
        if last is None:
            return True
        
        elapsed = (datetime.now() - last).total_seconds()
        return elapsed >= min_interval
    
    def record_fire(self, trigger_id: str):
        """記錄觸發時間"""
        self._last_fired[trigger_id] = datetime.now()
```

---

## 觸發處理

### 處理流程

```python
async def _handle_trigger(self, trigger: Trigger):
    """處理已觸發的觸發器"""
    
    start_time = datetime.now()
    
    try:
        # 1. 獲取上下文
        context = await trigger.get_context()
        
        # 2. 決策
        decision = await self.decision_pipeline.decide(trigger, context)
        
        # 3. 生成行動計劃
        plan = self.action_planner.plan(decision, trigger, context)
        
        # 4. 執行計劃
        if not plan.is_empty:
            await self._execute_plan(plan, trigger, context)
        
        # 5. 記錄統計
        self._stats.record_execution(
            trigger_id=trigger.id,
            decision=decision,
            duration=(datetime.now() - start_time).total_seconds(),
        )
    
    except Exception as e:
        logger.error(f"Error handling trigger {trigger.id}: {e}")
        self._stats.increment("execution_errors")
        
        # 發送錯誤通知
        await self.notification_router.send(
            NotificationType.ERROR,
            f"Trigger {trigger.id} execution failed: {e}"
        )
```

---

## 行動執行

```python
async def _execute_plan(
    self,
    plan: ActionPlan,
    trigger: Trigger,
    context: dict,
):
    """執行行動計劃"""
    
    action_executor = ActionExecutor(
        task_scheduler=self.task_scheduler,
        memory=self.memory,
        notification_router=self.notification_router,
        tool_registry=self.tool_registry,
    )
    
    result = await action_executor.execute(plan)
    
    if result.overall_success:
        logger.info(f"Action plan executed successfully for {trigger.id}")
    else:
        logger.warning(f"Action plan partially failed for {trigger.id}")
        
        # 發送失敗通知
        await self.notification_router.send(
            NotificationType.WARNING,
            f"Action plan for {trigger.id} completed with errors"
        )
```

---

## loom.toml 配置

### 完整配置範例

```toml
[autonomy]

# 是否啟用 daemon
enabled = true

# 檢查間隔（秒）
check_interval = 60

# 信任級別
trust_level = "GUARDED"

# 關閉開啟時的通知
notify_on_start = true
notify_on_stop = true

# 觸發器設定
[autonomy.triggers]

# 定時任務
[[autonomy.triggers.cron]]
id = "daily_summary"
cron = "0 9 * * *"
action = "send_daily_summary"
enabled = true

# 事件觸發
[[autonomy.triggers.event]]
id = "test_on_save"
event = "file_modified"
filter = "**/test_*.py"
action = "run_tests"
enabled = false

# 條件觸發
[[autonomy.triggers.condition]]
id = "memory_warning"
conditions = [
    { type = "memory_low", threshold = 10, operator = "<" }
]
logic = "AND"
action = "notify_memory_low"
check_interval = 300
enabled = true

# 觸發器冷卻（避免重複觸發）
[autonomy.triggers.cooldown]
default_seconds = 60
# 特定觸發器可以覆蓋
"daily_summary" = 3600
```

---

## CLI 命令

```bash
# 啟動 daemon
loom autonomy start

# 停止 daemon
loom autonomy stop

# 查看狀態
loom autonomy status

# 查看觸發器列表
loom autonomy triggers list

# 啟用/停用觸發器
loom autonomy triggers enable daily_summary
loom autonomy triggers disable test_on_save

# 手動觸發觸發器
loom autonomy trigger fire daily_summary

# 查看執行日誌
loom autonomy logs --tail 100
loom autonomy logs --trigger daily_summary
```

---

## 統計與監控

### 統計資料

```python
# loom/core/autonomy/daemon.py
@dataclass
class DaemonStats:
    """Daemon 統計"""
    
    cycles: int = 0
    triggers_fired: int = 0
    actions_executed: int = 0
    errors: int = 0
    trigger_errors: int = 0
    execution_errors: int = 0
    
    last_cycle_at: datetime | None = None
    uptime_seconds: float = 0.0
    
    def record_cycle(self, triggers_fired: int):
        self.cycles += 1
        self.triggers_fired += triggers_fired
        self.last_cycle_at = datetime.now()
    
    def record_execution(
        self,
        trigger_id: str,
        decision: Decision,
        duration: float,
    ):
        self.actions_executed += 1
    
    @property
    def success_rate(self) -> float:
        total = self.errors + self.trigger_errors + self.execution_errors + self.actions_executed
        return self.actions_executed / total if total > 0 else 0.0
```

### 狀態報告

```bash
$ loom autonomy status

Autonomy Daemon Status
======================
Status:        Running
Uptime:        2h 34m
Last Cycle:    45 seconds ago

Statistics
----------
Cycles:           9,240
Triggers Fired:   127
Actions Executed: 124
Errors:           3
Success Rate:     97.7%

Trigger Breakdown
-----------------
daily_summary:     45 fires, 44 executed, 1 skipped
memory_warning:     2 fires, 2 executed, 0 skipped
```

---

## 開機啟動

### 系統服務（systemd）

```ini
# /etc/systemd/system/loom-autonomy.service
[Unit]
Description=Loom Autonomy Daemon
After=network.target

[Service]
Type=simple
User=loom
WorkingDirectory=/home/loom
ExecStart=/usr/bin/loom autonomy start
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# 啟用開機啟動
sudo systemctl enable loom-autonomy
sudo systemctl start loom-autonomy
```

---

## 總結

Autonomy Daemon 是 Loom 的「自動化引擎核心」：

| 功能 | 說明 |
|------|------|
| 主循環 | 定期檢查觸發器狀態 |
| 觸發器管理 | 維護啟用/停用狀態、冷卻機制 |
| 決策執行 | 觸發 → 決策 → 行動 → 執行 |
| 統計監控 | 追蹤執行次數、成功率和錯誤 |
| CLI 控制 | start/stop/status/logs 命令 |
| 系統整合 | 支援 systemd 開機啟動 |
