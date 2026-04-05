# ConfirmFlow

ConfirmFlow 是 Loom 的「用戶確認流程」。當 Autonomy Engine 需要用戶授權才能執行高風險操作時，會觸發 ConfirmFlow。

---

## 使用場景

```
┌─────────────────────────────────────────────────────────────┐
│                    ConfirmFlow 觸發情境                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Autonomy Engine                                           │
│       │                                                     │
│       ▼                                                     │
│   Decision.CONFIRM                                          │
│       │                                                     │
│       ▼                                                     │
│   ConfirmFlow.run() ──▶ 等待用戶回覆                         │
│       │                                                     │
│       ├──▶ APPROVED  ──▶ 執行操作                           │
│       ├──▶ DENIED   ──▶ 取消操作                            │
│       └──▶ TIMEOUT  ──▶ 超時處理（預設 DENIED）              │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 核心流程

```python
# loom/core/notification/confirm_flow.py
class ConfirmFlow:
    """確認流程"""
    
    def __init__(
        self,
        prompt: str,
        timeout: float = 300.0,  # 5 分鐘超時
        default_action: ConfirmResult = ConfirmResult.DENIED,
    ):
        self.prompt = prompt
        self.timeout = timeout
        self.default_action = default_action
    
    async def run(self) -> ConfirmResult:
        """
        執行確認流程
        
        Returns:
            ConfirmResult: APPROVED / DENIED / TIMEOUT
        """
        
        # 1. 發送確認通知
        notification = await self._send_confirmation_request()
        
        # 2. 等待用戶回覆
        try:
            result = await self._wait_for_response(
                notification.id,
                timeout=self.timeout
            )
            return result
        
        except asyncio.TimeoutError:
            # 3. 超時處理
            return await self._handle_timeout()
    
    async def _send_confirmation_request(self) -> Notification:
        """發送確認請求"""
        return await notification_router.send(
            NotificationType.CONFIRM,
            self.prompt,
            source="autonomy.confirm_flow",
            timeout=self.timeout,
        )
    
    async def _wait_for_response(
        self,
        notification_id: str,
        timeout: float,
    ) -> ConfirmResult:
        """等待用戶回覆"""
        
        start_time = asyncio.get_event_loop().time()
        
        while True:
            # 檢查是否已收到回覆
            notification = await notification_store.get(notification_id)
            
            if notification and notification.response:
                return notification.response
            
            # 檢查超時
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                raise asyncio.TimeoutError()
            
            # 短暫休眠後重試
            await asyncio.sleep(0.5)
```

---

## ConfirmResult 枚舉

```python
# loom/core/notification/types.py
class ConfirmResult(Enum):
    """確認結果"""
    APPROVED = "approved"    # 用戶批准
    DENIED = "denied"       # 用戶拒絕
    TIMEOUT = "timeout"      # 超時（預設當作 DENIED）
```

---

## 回覆方式

### CLI 回覆

```python
# loom/core/notification/adapters/confirm_cli.py
class ConfirmCLIAdapter:
    """CLI 確認介面卡"""
    
    def __init__(self, input_stream: TextIO = sys.stdin):
        self.input_stream = input_stream
    
    async def request_confirmation(self, prompt: str) -> ConfirmResult:
        """在 CLI 請求用戶確認"""
        
        print(f"\n⚠️  需要確認：{prompt}")
        print("回覆 [a]pprove / [d]eny / [t]imeout (300s): ", end="", flush=True)
        
        # 非同步讀取輸入
        loop = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            None,
            lambda: self.input_stream.readline().strip().lower()
        )
        
        if reply in ("a", "approve", "y", "yes"):
            return ConfirmResult.APPROVED
        elif reply in ("t", "timeout"):
            return ConfirmResult.TIMEOUT
        else:
            return ConfirmResult.DENIED
```

### TUI 回覆

```python
# loom/core/notification/adapters/confirm_tui.py
class ConfirmTUIAdapter:
    """TUI 確認介面卡"""
    
    def __init__(self, app: "LoomTUI"):
        self.app = app
    
    async def request_confirmation(self, prompt: str) -> ConfirmResult:
        """在 TUI 請求用戶確認"""
        
        # TUI 模式使用按鈕
        return await self.app.show_confirm_dialog(prompt)
```

### API/Webhook 回覆

```python
# loom/core/notification/adapters/confirm_webhook.py
class ConfirmWebhookAdapter:
    """Webhook 確認介面卡"""
    
    def __init__(self, router: NotificationRouter):
        self.router = router
    
    async def request_confirmation(self, prompt: str) -> ConfirmResult:
        """通過 Webhook 請求確認"""
        
        # 發送帶有回覆連結的通知
        confirmation_url = f"https://example.com/confirm/{generate_id()}"
        
        await self.router.send(
            NotificationType.CONFIRM,
            f"{prompt}\n\n點擊確認：{confirmation_url}",
            source="autonomy.confirm_flow",
        )
        
        # 等待 webhook 回調
        return await self._wait_for_callback(confirmation_url, timeout=300)
```

---

## 超時處理

### 超時配置

```python
# loom/core/notification/confirm_flow.py
class ConfirmFlow:
    # ... 
    
    async def _handle_timeout(self) -> ConfirmResult:
        """處理超時"""
        
        logger.warning(f"Confirmation timeout after {self.timeout}s")
        
        # 發送超時通知
        await notification_router.send(
            NotificationType.INFO,
            f"確認超時（{self.timeout}s），操作已取消",
            source="autonomy.confirm_flow",
        )
        
        # 根據 default_action 決定結果
        return self.default_action
```

### 超時時的預設行為

| 設定 | 行為 |
|------|------|
| `default_action = DENIED` | 超時視為拒絕（安全預設） |
| `default_action = APPROVED` | 超時視為批准（需要明確信任） |

---

## 降級機制

### 當所有渠道都無法聯繫用戶時

```python
# loom/core/notification/confirm_flow.py
class GracefulDegradation:
    """優雅降級"""
    
    async def run_with_fallback(
        self,
        primary_flow: ConfirmFlow,
        fallback_flow: ConfirmFlow,
    ) -> ConfirmResult:
        """
        嘗試主要流程，失敗時降級到備用流程
        """
        
        try:
            return await primary_flow.run()
        except AllChannelsUnavailableError:
            logger.warning("All confirmation channels unavailable, using fallback")
            return await fallback_flow.run()
```

### 降級範例

```
主要渠道：Telegram Bot
    │
    ├──▶ Telegram 在線 ──▶ 請求確認
    │
    └──▶ Telegram 離線 ──▶ 降級到 Webhook
                                │
                                └──▶ Webhook 可達 ──▶ 請求確認
                                │
                                └──▶ Webhook 也不可達 ──▶ 降級到 CLI
                                                                    │
                                                                    └──▶ 最後手段：超時拒絕
```

---

## 整合 NotificationRouter

```python
# loom/core/notification/router.py
class NotificationRouter:
    async def send_confirm(
        self,
        prompt: str,
        timeout: float = 300,
        **kwargs,
    ) -> ConfirmResult:
        """發送確認請求並等待回覆"""
        
        # 創建確認流程
        flow = ConfirmFlow(
            prompt=prompt,
            timeout=timeout,
        )
        
        return await flow.run()
```

---

## loom.toml 配置

```toml
[notification.confirm_flow]

# 預設超時（秒）
default_timeout = 300

# 超時預設行為
# "denied" = 安全的預設（推薦）
# "approved" = 需要明確信任
default_action = "denied"

# 嘗試所有渠道
try_all_channels = true

# 降級設定
[notification.confirm_flow.fallback]
# 當第一渠道不可用時的降級順序
channels = ["cli", "webhook"]

# CLI 設定（始終可用）
[notification.confirm_flow.cli]
enabled = true
always_available = true

# Webhook 設定（作為降級）
[notification.confirm_flow.webhook]
enabled = false
url = "${CONFIRM_WEBHOOK_URL}"
```

---

## 安全性考量

### 防止脅迫

```python
class AntiCoercion:
    """防脅迫機制"""
    
    # 短時間內大量確認請求可能是脅迫
    MAX_REQUESTS_PER_MINUTE = 5
    
    # 某些高風險操作需要多次確認
    HIGH_RISK_CONFIRMATIONS = 2
```

### 審計日誌

```python
async def log_confirmation(
    notification_id: str,
    prompt: str,
    result: ConfirmResult,
    source: str,
):
    """記錄確認請求到審計日誌"""
    
    await audit_log.write({
        "event": "confirmation_request",
        "notification_id": notification_id,
        "prompt": prompt,
        "result": result.value,
        "source": source,
        "timestamp": datetime.now().isoformat(),
    })
```

---

## 總結

ConfirmFlow 確保高風險操作必須經過用戶授權：

| 功能 | 說明 |
|------|------|
| 多渠道支援 | CLI / TUI / Webhook / Telegram |
| 超時處理 | 可配置超時和預設行為 |
| 降級機制 | 主要渠道不可用時自動降級 |
| 防脅迫 | 短時間限制、審計日誌 |
| 統一介面 | 透過 NotificationRouter 整合 |
