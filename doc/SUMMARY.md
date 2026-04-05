# Loom 文檔總索引

> 這是 Loom 框架的完整文檔索引。所有功能說明都會收錄於 `/doc` 目錄下。

---

## 📚 文檔分類

### 0. 總覽與概念
| 文件 | 說明 |
|------|------|
| [00-總覽.md](00-總覽.md) | Loom 是什麼？定位、核心價值、與其他框架的差異 |
| [01-名詞解釋.md](01-名詞解釋.md) | Trust Level、Middleware、Memory Type、Personality 等核心術語 |

### 1. 架構導讀
| 文件 | 說明 |
|------|------|
| [02-系統架構.md](02-系統架構.md) | 七層架構圖與每層職責說明 |
| [03-目錄結構.md](03-目錄結構.md) | `loom/` 各模組的職責對照表 |

### 2. Harness Layer（工具生命週期管理）
| 文件 | 說明 |
|------|------|
| [04-Harness-概述.md](04-Harness-概述.md) | Middleware Pipeline 設計理念 |
| [05-Trust-Level.md](05-Trust-Level.md) | SAFE / GUARDED / CRITICAL 三級權限 (2026.4.5已修訂)|
| [06-Middleware-詳解.md](06-Middleware-詳解.md) | Log / Trace / BlastRadius 三種 Middleware |
| [07-Tool-Registry.md](07-Tool-Registry.md) | 工具定義格式與雙 provider schema 輸出 |

### 3. Memory Layer（記憶系統）
| 文件 | 說明 |
|------|------|
| [08-Memory-概述.md](08-Memory-概述.md) | 四種記憶類型與 SQLite WAL 後端 |
| [09-四種記憶詳解.md](09-四種記憶詳解.md) | Episodic / Semantic / Procedural / Relational |
| [10-Skill-Genome.md](10-Skill-Genome.md) | EMA confidence 機制與自動廢棄 |
| [11-Memory-Search.md](11-Memory-Search.md) | BM25 搜尋與未來的 Embedding 升級計畫 |
| [12-Memory-Index.md](12-Memory-Index.md) | 輕量目錄結構，讓 agent 按需召回 |

### 4. Cognition Layer（思考與推理）
| 文件 | 說明 |
|------|------|
| [13-Cognition-概述.md](13-Cognition-概述.md) | LLM Router / Context Budget / Reflection API 總覽 |
| [14-LLM-Router.md](14-LLM-Router.md) | 模型前綴路由與 Multi-Provider 支援 |
| [15-Context-Budget.md](15-Context-Budget.md) | Token 配額管理與自動壓縮機制 |
| [16-Reflection-API.md](16-Reflection-API.md) | Session 摘要、工具成功率、Skill 健康報告 |

### 5. Task Engine（任務引擎）
| 文件 | 說明 |
|------|------|
| [17-Task-Engine.md](17-Task-Engine.md) | DAG 圖與 Kahn's Topological Sort |
| [18-Task-Scheduler.md](18-Task-Scheduler.md) | asyncio.gather 並行執行與 stop_on_failure |

### 6. Autonomy Engine（自主行動）
| 文件 | 說明 |
|------|------|
| [19-Autonomy-概述.md](19-Autonomy-概述.md) | 觸發器類型與決策管道 |
| [20-觸發器詳解.md](20-觸發器詳解.md) | CronTrigger / EventTrigger / ConditionTrigger |
| [21-Action-Planner.md](21-Action-Planner.md) | Trust Level → Decision 映射邏輯 |
| [22-Autonomy-Daemon.md](22-Autonomy-Daemon.md) | 常駐程式與 loom.toml 設定 |

### 7. Notification Layer（通知系統）
| 文件 | 說明 |
|------|------|
| [23-Notification-概述.md](23-Notification-概述.md) | NotificationRouter 與五種通知類型 |
| [24-Notifier-適配器.md](24-Notifier-適配器.md) | CLI / Webhook / Telegram / Discord |
| [25-ConfirmFlow.md](25-ConfirmFlow.md) | 確認流程、超時降級、APPROVED/DENIED/TIMEOUT |

### 8. Prompt Stack（三層提示詞）
| 文件 | 說明 |
|------|------|
| [26-Prompt-Stack.md](26-Prompt-Stack.md) | SOUL → Agent → Personality 三層結構 |
| [27-SOUL-設計.md](27-SOUL-設計.md) | SOUL.md 的設計理念與內容說明 |
| [28-Personalities.md](28-Personalities.md) | 內建人格（Adversarial / Architect / Minimalist / Operator / Researcher / Barista）|

### 9. Extensibility（擴充系統）
| 文件 | 說明 |
|------|------|
| [29-Extensibility-概述.md](29-Extensibility-概述.md) | Lens 系統與 Plugin 架構 |
| [30-Lens-系統.md](30-Lens-系統.md) | BaseLens / HermesLens / OpenAIToolsLens |
| [31-Plugin-系統.md](31-Plugin-系統.md) | LoomPlugin 抽象、PluginRegistry、安裝流程 |
| [32-Skill-Import.md](32-Skill-Import.md) | 技能匯入 Pipeline（審查 → 去重 → confidence gate）|

### 10. Platform（CLI 與 TUI）
| 文件 | 說明 |
|------|------|
| [33-CLI-命令.md](33-CLI-命令.md) | loom chat / memory / reflect / autonomy 指令 |
| [34-TUI-使用指南.md](34-TUI-使用指南.md) | Textual TUI 介面操作說明 |
| [35-Session-管理.md](35-Session-管理.md) | --resume / --session / sessions list/show/rm |
| [36-Web-Tools.md](36-Web-Tools.md) | fetch_url / web_search 工具說明 |

### 11. 設定與配置
| 文件 | 說明 |
|------|------|
| [37-loom-toml-參考.md](37-loom-toml-參考.md) | 所有設定項目詳解(2026.4.5已修訂) |
| [38-環境變數.md](38-環境變數.md) | .env 支援的變數清單 |

### 12. 開發者指南
| 文件 | 說明 |
|------|------|
| [39-新增工具.md](39-新增工具.md) | 如何在 loom 中註冊新工具 |
| [40-新增Notifier.md](40-新增Notifier.md) | 如何實作新的通知適配器 |
| [41-新增人格.md](41-新增人格.md) | 如何建立新 personality markdown |
| [42-測試指南.md](42-測試指南.md) | pytest 執行方式與測試覆蓋 |

### 附錄
| 文件 | 說明 |
|------|------|
| [99-功能人工確認清單.md](99-功能人工確認清單.md) | 透過對話逐一驗證架構行為與設計一致性 |

---

## ✅ 文件撰寫狀態

全部 43 個文件已完成！

| 區塊 | 完成數 |
|------|--------|
| 0. 總覽與概念 | 2/2 |
| 1. 架構導讀 | 2/2 |
| 2. Harness Layer | 4/4 |
| 3. Memory Layer | 5/5 |
| 4. Cognition Layer | 4/4 |
| 5. Task Engine | 2/2 |
| 6. Autonomy Engine | 4/4 |
| 7. Notification Layer | 3/3 |
| 8. Prompt Stack | 3/3 |
| 9. Extensibility | 4/4 |
| 10. Platform | 4/4 |
| 11. 設定與配置 | 2/2 |
| 12. 開發者指南 | 4/4 |
| 附錄 | 1/1 |

---

> 📚 Loom 文檔編寫完成！
