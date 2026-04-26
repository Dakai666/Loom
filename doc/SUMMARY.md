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
| [03-目錄結構.md](03-目錄結構.md) | `loom/` 各模組職責對照表，含 `skills/` 與 `infra/` |

### 2. Harness Layer（工具生命週期管理）
| 文件 | 說明 |
|------|------|
| [04-Harness-概述.md](04-Harness-概述.md) | Middleware Pipeline 設計 + SchemaValidationMiddleware + AbortController |
| [05-Trust-Level.md](05-Trust-Level.md) | SAFE / GUARDED / CRITICAL 三級權限 |
| [06-Middleware-詳解.md](06-Middleware-詳解.md) | Log / Trace / BlastRadius / SchemaValidation / LifecycleMiddleware / LifecycleGateMiddleware |
| [06b-Action-Lifecycle.md](06b-Action-Lifecycle.md) | Control-first 狀態機詳解：兩層架構、各狀態場景、precondition 失敗情況、abort racing、post-validator + rollback |
| [07-Tool-Registry.md](07-Tool-Registry.md) | 工具定義格式與雙 provider schema 輸出 |

### 3. Memory Layer（記憶系統）
| 文件 | 說明 |
|------|------|
| [08-Memory-概述.md](08-Memory-概述.md) | 四種記憶類型、SQLite FTS5、Anti-pattern、Session Log 結構化、Governance 概覽 |
| [08b-Memory-Governance.md](08b-Memory-Governance.md) | Trust Tier 分級、矛盾偵測 + 自動解決、Admission Gate、Decay Cycle |
| [09-四種記憶詳解.md](09-四種記憶詳解.md) | Episodic / Semantic（含 Trust Tier + Anti-pattern）/ Procedural / Relational |
| [10-Skill-Genome.md](10-Skill-Genome.md) | EMA confidence 機制與自動廢棄（含 SKILL.md frontmatter schema）|
| [10b-Skill-Evolution.md](10b-Skill-Evolution.md) | Issue #120：TaskReflector → SkillMutator → SkillPromoter + SkillGate 的三段式進化生命週期 |
| [11-Memory-Search.md](11-Memory-Search.md) | Phase 5 向量相似度 → FTS5 BM25 → recency 三層混合搜尋 |
| [12-Memory-Index.md](12-Memory-Index.md) | 輕量目錄，含 Anti-pattern count 與 Self-Portrait |
| **[12b-Memory-Health.md](12b-Memory-Health.md)** | MemoryHealthTracker 操作手冊（Issue #133）|

### 4. Cognition Layer（思考與推理）
| 文件 | 說明 |
|------|------|
| [13-Cognition-概述.md](13-Cognition-概述.md) | LLM Router / Context Budget / Reflection API / Counter-factual Reflection |
| [14-LLM-Router.md](14-LLM-Router.md) | 模型前綴路由與 Multi-Provider 支援 |
| [15-Context-Budget.md](15-Context-Budget.md) | Token 配額管理與自動壓縮機制 |
| [16-Reflection-API.md](16-Reflection-API.md) | Session 摘要、工具報告、Counter-factual / Self-Reflection |

### 5. Task Engine（任務引擎）
| 文件 | 說明 |
|------|------|
| [17-Task-Engine.md](17-Task-Engine.md) | TaskList 認知外骨骼（平坦清單 + 自驅動 + pre-final-response self-check）|
| [18-Task-Scheduler.md](18-Task-Scheduler.md) | Async Jobs：JobStore + Scratchpad，IO 層並行與 turn-boundary 事件注入 |

### 6. Autonomy Engine（自主行動）
| 文件 | 說明 |
|------|------|
| [19-Autonomy-概述.md](19-Autonomy-概述.md) | 觸發器、決策管道、DreamingPlugin、TaskReflector 行為反思、Counter-factual |
| [20-觸發器詳解.md](20-觸發器詳解.md) | CronTrigger / EventTrigger / ConditionTrigger |
| [21-Action-Planner.md](21-Action-Planner.md) | ActionDecision（EXECUTE/NOTIFY/HOLD/SKIP）映射邏輯 |
| [22-Autonomy-Daemon.md](22-Autonomy-Daemon.md) | 常駐程式、統一管線（origin-aware）、scope_grants、attach_outputs、Issue #91 tamper detection |

### 7. Notification Layer（通知系統）
| 文件 | 說明 |
|------|------|
| [23-Notification-概述.md](23-Notification-概述.md) | NotificationRouter 與五種通知類型 |
| [24-Notifier-適配器.md](24-Notifier-適配器.md) | CLI / Webhook / Discord Notifier |
| **[24b-Notification-Types.md](24b-Notification-Types.md)** | Notification / ConfirmResult dataclass 完整定義、Discord thread 路由 |
| [25-ConfirmFlow.md](25-ConfirmFlow.md) | 確認流程、超時降級、APPROVED/DENIED/TIMEOUT |

### 8. Prompt Stack（三層提示詞）
| 文件 | 說明 |
|------|------|
| [26-Prompt-Stack.md](26-Prompt-Stack.md) | SOUL → Agent → Personality 三層結構（含 runtime switch_personality）|
| [27-SOUL-設計.md](27-SOUL-設計.md) | SOUL.md 的設計理念與內容說明 |
| [28-Personalities.md](28-Personalities.md) | 內建人格（Adversarial / Architect / Minimalist / Operator / Researcher / Barista / Moon Mood）|

### 9. Extensibility（擴充系統）
| 文件 | 說明 |
|------|------|
| [29-Extensibility-概述.md](29-Extensibility-概述.md) | Lens / Plugin / Skill Import / MCP 整合 / skills/ 命名 |
| [30-Lens-系統.md](30-Lens-系統.md) | BaseLens / HermesLens / OpenAIToolsLens |
| [31-Plugin-系統.md](31-Plugin-系統.md) | LoomPlugin 抽象、PluginRegistry、首次確認機制（RelationalMemory）|
| **[31b-MCP-Server-實作.md](31b-MCP-Server-實作.md)** | MCP Server / Client 雙向實作詳解、tool 前綴、env 擴展 |
| [32-Skill-Import.md](32-Skill-Import.md) | 技能匯入 Pipeline（三層漸進式披露、SkillOutcomeTracker）|

### 10. Platform（CLI 與 TUI）
| 文件 | 說明 |
|------|------|
| [33-CLI-命令.md](33-CLI-命令.md) | loom chat / memory / reflect / autonomy / mcp / discord 指令 |
| [34-TUI-使用指南.md](34-TUI-使用指南.md) | Textual TUI（HITL /pause+stop、Command Palette、Swarm Dashboard）|
| [35-Session-管理.md](35-Session-管理.md) | --resume / --session / sessions list/show/rm |
| [36-Web-Tools.md](36-Web-Tools.md) | fetch_url / web_search（含 post-verifier）+ send_discord_embed 完整 schema |
| **[36b-Discord-Bot-平台.md](36b-Discord-Bot-平台.md)** | LoomDiscordBot 完整實作、Streaming 策略、TaskWriteDiscordReminderMiddleware |
| **[35b-Session-Lifecycle-詳解.md](35b-Session-Lifecycle-詳解.md)** | LoomSession 建構 → start() → stream_turn() → stop() 完整生命週期 |

### 11. 設定與配置
| 文件 | 說明 |
|------|------|
| [37-loom-toml-參考.md](37-loom-toml-參考.md) | 所有設定項目（含 MCP servers、predictive pre-fetcher、AbortController）|
| [38-環境變數.md](38-環境變數.md) | .env 支援的變數（含 DISCORD_BOT_TOKEN、MCP 相關）|

### 12. 開發者指南
| 文件 | 說明 |
|------|------|
| [39-新增工具.md](39-新增工具.md) | 如何在 loom 中註冊新工具 |
| [40-新增Notifier.md](40-新增Notifier.md) | 如何實作新的通知適配器 |
| [41-新增人格.md](41-新增人格.md) | 如何建立新 personality markdown |
| [42-測試指南.md](42-測試指南.md) | pytest 執行方式與測試覆蓋 |
| [43-Harness-Execution-可視化規劃.md](43-Harness-Execution-可視化規劃.md) | TUI / Discord 的 execution graph、control surface 與 phased rollout 規劃 |
| [44-Scope-Aware-Permission-規劃.md](44-Scope-Aware-Permission-規劃.md) | Issue #45 的底層 permission substrate 規劃 |
| [45-AbortController.md](45-AbortController.md) | 標準取消訊號、memory-leak safety、asyncio.Event API |
| [46-CommandScanner.md](46-CommandScanner.md) | Shell 注入掃描、BLOCK/WARN pattern、Layering 策略 |
| **[45b-Security-Module.md](45b-Security-Module.md)** | Security 模組完整說明：CommandScanner + SelfTerminationGuard 分工 |

### 附錄
| 文件 | 說明 |
|------|------|
| [47-Legitimacy-Heuristics-規劃.md](47-Legitimacy-Heuristics-規劃.md) | Issue #47 的 Intent Declaration、軌跡守衛、污染熔斷設計 |
| [48-Agent-Telemetry.md](48-Agent-Telemetry.md) | Issue #142：三維度自我觀測、DimensionTracker、agent_health 工具 |
| [99-功能人工確認清單.md](99-功能人工確認清單.md) | 透過對話逐一驗證架構行為與設計一致性 |
| [MISSING-DOC-AUDIT.md](MISSING-DOC-AUDIT.md) | 本次 doc 缺口清單與版本比對記錄（2026-04-26）|

---

## 版本對照（v0.2.5.1 → v0.2.9.4 新功能）

| 版本 | 主要新功能 |
|------|-----------|
| v0.2.5.1 | AbortController、Counter-factual Reflection |
| v0.2.5.2 | SchemaValidationMiddleware、SQLite FTS5、Discord 多媒體 |
| v0.2.5.3 | Offline Dreaming、SelfReflectionPlugin（→ TaskReflector）、Session Log 結構化 |
| v0.2.6.0 | MCP 整合（Server + Client）、Predictive Memory Pre-fetcher |
| v0.2.6.1 | Plugin 架構修復、`skills/` 目錄命名 |
| v0.2.8.0 | Control-first Action Lifecycle（Issue #50）：`LifecycleMiddleware` + `LifecycleGateMiddleware`；`precondition_checks[]`；abort signal racing；移除 `/verbose` F3（Issue #63） |
| v0.2.9.0 | Advanced Memory Governance（Issue #43）：Trust Tier 信任分級（10 層）；`ContradictionDetector`（REPLACE/KEEP/SUPERSEDE）；Admission Gate；Decay Cycle |
| v0.2.9.4 | Unified Pipeline（Issues #83–#86）：`ToolCall.origin`；MCP/autonomy/sub-agent 全經同一 Pipeline；`allowed_tools` + `scope_grants` + `attach_outputs`；Config tamper detection（Issue #91） |

---

## ✅ 文件撰寫狀態

**57 個文件已完整！**

| 區塊 | 數量 |
|------|------|
| 0. 總覽與概念 | 2 |
| 1. 架構導讀 | 2 |
| 2. Harness Layer | 5 |
| 3. Memory Layer | 7（含 12b）|
| 4. Cognition Layer | 4 |
| 5. Task Engine | 2 |
| 6. Autonomy Engine | 4 |
| 7. Notification Layer | 4（含 24b）|
| 8. Prompt Stack | 3 |
| 9. Extensibility | 5（含 31b）|
| 10. Platform | 6（含 35b、36b）|
| 11. 設定與配置 | 2 |
| 12. 開發者指南 | 8（含 45、45b、46）|
| 附錄 | 3（含 MISSING-DOC-AUDIT）|

---

> 📚 Loom 文檔 v0.2.9.4 同步完成！| 2026-04-26 03:21 Asia/Taipei