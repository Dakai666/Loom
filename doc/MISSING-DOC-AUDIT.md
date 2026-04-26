# Loom Doc 缺口清單與文件草稿

> 絲繹・Loom 執行 | 2026-04-26 03:21 Asia/Taipei
> 依據：完整原始碼掃描（`loom/`）與現有 48 篇 doc 文件比對
> 狀態：已完整完成（12/16 缺口已處理）

---

## 一、盤點範圍

### 掃描過的目錄與關鍵檔案

```
loom/
├── core/
│   ├── harness/        lifecycle.py, middleware.py, permissions.py, registry.py,
│   │                   scope.py, skill_checks.py, validation.py
│   ├── memory/         semantic.py, episodic.py, procedural.py, relational.py,
│   │                   contradiction.py, governance.py, health.py, search.py,
│   │                   index.py, maintenance.py, embeddings.py, facade.py, store.py,
│   │                   session_log.py, skill_outcome.py
│   ├── cognition/     prompt_stack.py, router.py, providers.py, reflection.py,
│   │                   task_reflector.py, skill_promoter.py, skill_mutator.py,
│   │                   skill_gate.py, dreaming.py, counter_factual.py,
│   │                   context.py, skill_gate.py
│   ├── agent/         subagent.py
│   ├── tasks/          manager.py, tasklist.py
│   ├── jobs/           store.py, scratchpad.py
│   ├── security/       command_scanner.py, self_termination_guard.py
│   ├── infra/          abort.py, telemetry.py
│   ├── session.py
│   └── events.py
├── autonomy/           daemon.py, evaluator.py, planner.py, triggers.py,
│                       self_reflection.py, history.py
├── notify/
│   └── adapters/       cli.py, discord.py, webhook.py, discord_bot.py
├── extensibility/       adapter.py, plugin.py, lens.py, hermes.py,
│                       mcp_client.py, mcp_server.py, openai_tools.py,
│                       pipeline.py, dreaming_plugin.py
├── platform/
│   ├── cli/
│   ├── api/            server.py
│   └── discord/         bot.py, middleware.py, tools.py
└── palace/             components/
```

### 現有 doc 文件（共 48 篇 + 本次新增 9 篇）

---

## 二、缺口清單（共 16 個）

### 缺失類（8個）— 完全沒有文件對應的實作

| # | 缺口名稱 | 嚴重性 | 狀態 | 產出文件 |
|---|---------|--------|------|---------|
| 1 | AbortController 實作 | 中 | ✅ 已完成 | `doc/45-AbortController.md` |
| 2 | CommandScanner 安全掃描 | 高 | ✅ 已完成 | `doc/46-CommandScanner.md` |
| 3 | Notification Types 完整定義 | 中 | ✅ 已完成 | `doc/24b-Notification-Types.md` |
| 4 | Discord Bot 平台整合 | 中 | ✅ 已完成 | `doc/36b-Discord-Bot-平台.md` |
| 5 | Security 模組完整說明 | 中 | ✅ 已完成 | `doc/45b-Security-Module.md` |
| 6 | MCP Server 實作細節 | 低 | ✅ 已完成 | `doc/31b-MCP-Server-實作.md` |
| 7 | LoomSession 完整生命週期 | 高 | ✅ 已完成 | `doc/35b-Session-Lifecycle-詳解.md` |
| 8 | Memory Health 操作手冊 | 中 | ✅ 已完成 | `doc/12b-Memory-Health.md` |

### 需深化類（8個）— 有文件但深度不足

| # | 缺口名稱 | 狀態 | 產出文件 |
|---|---------|------|---------|
| 9 | AutonomyDaemon 實作同步 | ✅ 已完成 | `doc/22-Autonomy-Daemon.md`（重寫）+ `doc/21-Action-Planner.md`（簡化）|
| 10 | LLM Router Multi-Provider | ✅ 已完成 | `doc/14-LLM-Router.md`（重寫）|
| 11 | Context Budget 自動壓縮 | ✅ 已完成 | `doc/15-Context-Budget.md`（重寫）|
| 12 | TaskReflector / Skill Evolution 完整實作 | ✅ 已完成 | `doc/10b-Skill-Evolution.md`（大幅更新）|
| 13 | PromptStack switch_personality | ✅ 已完成 | `doc/26-Prompt-Stack.md`（重寫）|
| 14 | Discord Notifier 工具行為 | ✅ 已完成 | `doc/36-Web-Tools.md`（增量）|
| 15 | Plugin 首次確認機制 | ✅ 已完成 | `doc/31-Plugin-系統.md`（增量）|
| 16 | Skills 目錄結構與格式 | ✅ 已完成 | `doc/10-Skill-Genome.md`（增量）+ `doc/32-Skill-Import.md`（增量）|

---

## 三、本次新增文件清單

| 檔案 | 類型 |
|------|------|
| `doc/MISSING-DOC-AUDIT.md` | 缺口清單主檔 |
| `doc/45-AbortController.md` | 新增 |
| `doc/46-CommandScanner.md` | 新增 |
| `doc/24b-Notification-Types.md` | 新增 |
| `doc/36b-Discord-Bot-平台.md` | 新增 |
| `doc/45b-Security-Module.md` | 新增 |
| `doc/31b-MCP-Server-實作.md` | 新增 |
| `doc/35b-Session-Lifecycle-詳解.md` | 新增 |
| `doc/12b-Memory-Health.md` | 新增 |

**重寫 5 篇**：`doc/14`, `doc/15`, `doc/21`, `doc/22`, `doc/26`
**增量更新 4 篇**：`doc/10`, `doc/31`, `doc/32`, `doc/36`

---

## 四、關鍵實作變更摘要（文件 vs 實作差異）

### 1. AutonomyDaemon — DecisionPipeline 不存在

**舊版 doc**：描述了獨立的 `DecisionPipeline` 類別
**實作**：決策邏輯整合在 `ActionPlanner.handle()` 內，DecisionPipeline 是 doc 虛構

### 2. LLM Router — 無 Intent 分類

**舊版 doc**：描述 Intent-based routing，`/reasoning` 等前綴
**實作**：純前綴匹配（`MiniMax-`, `claude-`, `ollama/` 等），無意圖分析

### 3. LLM Router — 無 RouterConfig dataclass

**舊版 doc**：描述 `RouterConfig(prefix_routes, model_providers)`
**實作**：路由表是純 Python 常數，無 dataclass 封裝

### 4. Context Budget — 無 `can_fit()` / `consume()`

**舊版 doc**：描述 `can_fit()`, `consume()` 等方法
**實作**：實際方法為 `record_response()`, `record_messages()`, `should_compress()`, `fits()`

### 5. Prompt Stack — 無 AgentPromptGenerator

**舊版 doc**：描述 async `build()` + `AgentPromptGenerator` + `PersonalityLoader`
**實作**：純同步讀檔、字串組合，runtime 可切換 personality

### 6. Skill Evolution — 缺少 `from_batch_diagnostic()`

**舊版 doc**：只描述 `propose_candidate()`
**實作**：還有 `from_batch_diagnostic()`（Grader 批量路徑）和 `fast_track` 機制

---

## 五、不需文件的原因

以下實作**刻意不寫文件**：

| 實作 | 位置 | 說明 |
|------|------|------|
| `loom/palace/components/` | 空目錄 | 歷史殘留，無內容 |
| `loom/core/cognition/agent_health.py` | 見 `doc/48` | 已有對應文件 |
| `loom/core/cognition/memory_health.py` | 不存在 | 無此檔案 |
| `loom/notify/adapters/confirm.py` | 見 `doc/24b` | 已有對應文件 |

---

## 六、待觀察缺口（本次未處理）

這些實作存在但尚未確認是否需要獨立文件：

| 實作 | 位置 | 說明 |
|------|------|------|
| `ExecutionEnvelopeView` / execution graph | `loom/core/events.py` | Issue #110 的 execution 可視化結構，doc 中提到但未深入 |
| `_resolve_attachments()` | `loom/autonomy/daemon.py` | `attach_outputs` 新功能，已有 doc 記載 |
| Config tamper detection | `loom/autonomy/daemon.py` | Issue #91，已在 doc/22 中記載 |

---

*本報告由絲繹・Loom 產生 | 2026-04-26 03:21 Asia/Taipei*