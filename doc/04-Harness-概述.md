# Harness Layer 概述

> **「工具是公民，不是附屬品。」**

Harness Layer 是 Loom 的骨幹。所有工具的呼叫——無論來自 Agent、Task Engine、還是 Autonomy Engine——都必須經過 Harness 的 Middleware Pipeline。

---

## 設計動機

傳統 agent 框架將工具視為「功能」：定義函數 → 直接呼叫 → 完成。

Loom 的觀點不同：**工具是有生命週期的對象**，從「被定義」到「被執行」到「被審計」，每一個環節都需要被管理。

Middleware Pipeline 就是這個管理機制的實現。

---

## 與 Claude Code 的差異

Claude Code 只有兩個 hooks：
```
PreToolUse → 工具執行 → PostToolUse
```

Loom 改為全管道 Middleware Chain：
```
Middleware_1 → Middleware_2 → ... → Middleware_N → 實際工具執行
```

每個 Middleware 都是一個裝飾器，可以在工具執行前後附加任意邏輯，而且可以堆疊、組合、替換。

---

## Pipeline 流程

```
┌─────────────────────────────────────────────────────────────┐
│  Agent 發起工具呼叫                                          │
│  harness.execute(name, arguments, session_state)            │
└─────────────────────────────────────────────────────────────┘
                           ↓
              ┌─────────────────────────────┐
              │  MiddlewarePipeline.run()   │
              └─────────────────────────────┘
                           ↓
    ┌──────────┐    ┌──────────┐    ┌──────────────────┐
    │ Log      │ →  │ Trace    │ →  │ BlastRadius      │
    │Middleware │    │Middleware│    │ Middleware       │
    └──────────┘    └──────────┘    └──────────────────┘
                                               ↓
                          ┌──────────────────────────────────┐
                          │  BlastRadiusMiddleware.decide()  │
                          │                                    │
                          │  SAFE     → 放行（已預授權）      │
                          │  GUARDED  → 首次確認，session內免  │
                          │  CRITICAL → 每次都確認             │
                          └──────────────────────────────────┘
                                               ↓
                              ┌─────────────────────────────┐
                              │  ToolRegistry.execute()      │
                              │  實際工具函數執行             │
                              └─────────────────────────────┘
                                               ↓
                           ┌──────────────────────────────────┐
                           │  MiddlewarePipeline.on_result()  │
                           │  （回傳方向，清理 / 寫入等）      │
                           └──────────────────────────────────┘
```

---

## 已實作的 Middleware

| Middleware | 方向 | 職責 |
|------------|------|------|
| `LogMiddleware` | 雙向 | Rich 格式化輸出每次工具調用與結果到終端 |
| `TraceMiddleware` | 雙向 | 計時 + 每次 tool call/write 寫入 EpisodicMemory |
| `BlastRadiusMiddleware` | 雙向 | Trust Level 判斷 + 人類確認請求 |

---

## 與其他層的關係

```
Memory Layer ──提供──▶ TraceMiddleware（寫入 Episodic）
Cognition Layer ──提供──▶ 工具結果消費（Reflection API）
Autonomy Engine ──依賴──▶ Harness.execute()（執行決策）
Task Engine ──依賴──▶ Harness.execute()（並行工具執行）
Platform CLI ──依賴──▶ ToolRegistry（輸出可用工具列表）
```

---

## Trust Level 是什麼？

每一個工具都有一個 Trust Level，決定它需要多少人類審批：

| 等級 | 定義 | 行為 |
|------|------|------|
| **SAFE** | 唯讀、本地、可逆 | 自動執行，session 開始時預授權 |
| **GUARDED** | 寫入、網路、有副作用 | 首次需確認，session 內授權後免再詢問 |
| **CRITICAL** | 破壞性、跨系統、不可逆 | 每次強制確認，不可 session 授權 |

詳細說明：請參閱 [05-Trust-Level.md](05-Trust-Level.md)

---

## Middleware 擴展

Loom 的 Middleware 是可堆疊的。你可以在 Plugin 中實作新的 Middleware 並透過 `PluginRegistry.install_into()` 注入。

```python
class MyMiddleware(Middleware):
    name = "my_middleware"

    async def before(self, call: ToolCall, next):
        # 工具執行前的邏輯
        return await next(call)  # 呼叫下一個 middleware 或工具

    async def after(self, result: ToolResult, next):
        # 工具執行後的邏輯
        return await next(result)
```

---

## 什麼不該在 Harness 做

- **不應**在 Middleware 內直接執行耗時 I/O（如大量 DB 寫入）——交給 async task
- **不應**在 Middleware 內改變工具參數——使用 `before()` 修飾，不使用 `after()` 注入
- **不應**在 Middleware 內存取其他工具的內部狀態——每個 Middleware 應該無狀態或只讀 session state
