# Loom Agent 能力評級系統

> 目的：用 RPG 角色卡與技能樹的形式，管理 Loom 這個 agent runtime 的能力成熟度、短板、主線任務與升級條件。  
> 性質：規劃與治理文件，不是前端介面規格。  
> 建立日期：2026-05-06 Asia/Taipei

---

## 1. 設計動機

Loom 的能力已經橫跨 Harness、Memory、Cognition、Autonomy、Platform、Extensibility、Security。當系統越長越大，單純用 issue list 或 changelog 很難直觀看出：

- 哪些能力已經接近 operator-grade。
- 哪些能力看起來有功能，但其實缺測試、缺沙盒、缺 replay。
- 下一個 release 應該補短板，還是推主線。
- 某個能力的提升是否真的被證據支撐。

本文件提出一套 **RPG-style Capability Sheet**：把 Loom 當成一個正在成長的 agent 角色，為每個能力標記等級、分數、證據、升級條件與 debuff。

核心原則：

> 數值不是裝飾。每個評級都必須能回扣到文件、測試、程式符號、真實使用案例或可重播 trace。

---

## 2. 評級總覽

### 2.1 六維主屬性

Loom 的整體能力先用六個 RPG 主屬性描述：

| 屬性 | 代表意義 | 主要對應模組 |
|------|----------|--------------|
| STR 行動力 | 工具執行、寫檔、shell、MCP、subagent、任務落地能力 | Harness / Tools / Task Engine / MCP |
| DEX 控制力 | lifecycle、abort、pause、rollback、scope grants、permission precision | Harness / Permissions / Lifecycle |
| CON 穩定性 | 測試、錯誤恢復、長任務、session resume、並行穩定性 | Tests / Session / Jobs / Telemetry |
| INT 推理力 | model routing、context budget、judge、reflection、prompt stack | Cognition |
| WIS 記憶力 | semantic、episodic、procedural、relational、governance、dreaming | Memory |
| CHA 協作力 | CLI/TUI/Discord、確認流程、可解釋輸出、operator UX | Platform / Notify |

建議每月或每個 minor release 重新評估一次。

### 2.2 成熟度等級

每個 capability 使用 Level 0-5：

| Level | 名稱 | 定義 |
|-------|------|------|
| 0 | Missing | 尚未存在，或只有概念文字 |
| 1 | Prototype | 有初步實作，但邊界薄、測試少、失敗模式未完整處理 |
| 2 | Usable | 可日常使用，有基本測試與文件 |
| 3 | Reliable | 有 regression tests、錯誤恢復、清楚 contract |
| 4 | Operator-grade | 可觀測、可審計、可回滾、可長時間運行 |
| 5 | Self-improving | 能從 replay/eval/telemetry 中自動改善，且改善可被證明 |

### 2.3 三分制

每個能力不只給單一分數，而是拆成三個維度：

| 分數 | 問題 |
|------|------|
| Power | 這個能力的功能覆蓋與設計上限有多高？ |
| Reliability | 在壓力、錯誤、邊界情境下是否可靠？ |
| Evidence | 是否有測試、文件、trace、eval、真實使用案例支撐？ |

總分可用簡單平均，但 review 時應優先看最低分。最低分通常就是短板。

---

## 3. Overall Character Sheet

> 下表是初始評估模板。實際數值應在 release review 時依據測試、文件與實作更新。

| 主屬性 | 初始等級 | Power | Reliability | Evidence | 判斷 |
|--------|----------|-------|-------------|----------|------|
| STR 行動力 | B+ | 84 | 74 | 76 | 工具與 subagent 能力強，但 sandbox 會限制自治上限 |
| DEX 控制力 | A- | 88 | 80 | 82 | lifecycle、scope、rollback 是 Loom 目前最強護城河 |
| CON 穩定性 | B | 76 | 70 | 78 | 測試量可觀，但長任務 replay / stress eval 還可加強 |
| INT 推理力 | B | 78 | 68 | 70 | router、judge、reflection 已具雛形，仍需 eval 閉環 |
| WIS 記憶力 | A- | 90 | 74 | 80 | memory governance 很前沿，演化治理仍需證明 |
| CHA 協作力 | B+ | 82 | 74 | 76 | CLI/TUI/Discord control surface 已成熟，但仍需統一 event contract |

### 初始角色定位

```text
Class: Harness Weaver
Subclass: Memory-native Operator Agent
Build Style: Control-first / Audit-heavy / Long-lived
Current Weakness: Sandbox wall, unified event ledger, evolution eval
Current Strength: Action lifecycle, memory governance, scope-aware permission
```

---

## 4. Capability Radar

| Capability | Level | Power | Reliability | Evidence | 短評 |
|------------|-------|-------|-------------|----------|------|
| Action Lifecycle | 4 | 92 | 84 | 86 | Loom 的核心優勢，已接近 operator-grade |
| Scope-aware Permission | 4 | 88 | 80 | 82 | 已有 scope grants 與 TTL，應持續擴到 capability policy |
| OS Sandbox | 1 | 38 | 30 | 44 | 有 scanner 與 cwd 約束，但尚未是真正安全邊界 |
| Command Scanner | 3 | 70 | 72 | 76 | 很適合當 tripwire，但不可誤認為 sandbox |
| Memory Governance | 4 | 92 | 76 | 82 | trust tier、contradiction、decay 很強，需 eval 證明品質 |
| MemoryPulse (Hook G/A) | 2 | 82 | 72 | 70 | 顧問漏列（2026-05-06 補）；Hook G preheat + Hook A contradiction notice 已實作 v0.3.6.0 |
| Skill Evolution | 2 | 80 | 65 | 64 | 想像力高，shadow/fast-track/Grader 機制已到位，replay eval 仍是短板 |
| LLM-as-Judge (Verdict) | 2 | 80 | 70 | 72 | 顧問漏列（2026-05-06 補）；#196 Phase 2 verdict system 已實作，verdict 可 replay |
| SubAgent | 3 | 78 | 68 | 72 | bounded child agent 設計健康，尚未進入 agent society |
| Autonomy Daemon | 2 | 74 | 65 | 66 | trigger/action 已可用，trigger registry 本身成熟，dry-run simulator 仍不足 |
| MaintenanceLoop (daemon-cron) | 2 | 68 | 72 | 74 | 顧問漏列（2026-05-06 補）；v0.3.6.0 daemon-cron decay runner，run() throttle 已實作 |
| Event Stream | 2 | 84 | 60 | 68 | events 已多，但還沒有統一 AgentLedger |
| TUI / Discord Control Surface | 3 | 82 | 72 | 76 | 已能承載 envelope 與 confirm，需共享同一 rendering contract |
| MCP Extensibility | 2 | 76 | 62 | 66 | 方向好，但外部 tool 的安全 policy 要更硬 |
| Documentation System | 3 | 86 | 66 | 78 | 文件豐富，但需要 doc drift CI |

---

## 5. Skill Trees

### 5.1 Harness Tree

```text
Harness
├─ Tool Registry
├─ Schema Validation
├─ Trust Level
├─ Scope-aware Permission
├─ Action Lifecycle
├─ Post-validator / Rollback
├─ JIT Retrieval
└─ Capability Firewall  [future]
```

| Skill | Current | Target | 升級條件 |
|-------|---------|--------|----------|
| Action Lifecycle | L4 | L5 | lifecycle trace 可 replay；semantic failure 進入 eval corpus |
| Scope-aware Permission | L4 | L5 | policy-as-code；跨 platform grants 完全一致 |
| Post-validator / Rollback | L3 | L4 | 每個 mutating tool 都有 verifier + rollback contract test |
| JIT Retrieval | L3 | L4 | scratchpad refs 可追蹤來源、可被 replay、可檢查 masking |
| Capability Firewall | L0 | L3 | 每個 tool 宣告 fs/network/env/process capability |

### 5.2 Memory Tree

```text
Memory
├─ Episodic
├─ Semantic
├─ Procedural
├─ Relational
├─ Search
├─ Governance
├─ Pulse
├─ Dreaming
└─ Belief Git  [future]
```

| Skill | Current | Target | 升級條件 |
|-------|---------|--------|----------|
| Semantic Governance | L4 | L5 | contradiction resolution 有 eval set；confidence decay 可解釋 |
| Procedural Skill Genome | L3 | L5 | promotion 由 replay metric 驅動 |
| Relational Memory | L3 | L4 | triples 有 provenance、trust、decay、diff |
| Dreaming | L2 | L4 | dream 產物可審查、可撤銷、可衡量 usefulness |
| Belief Git | L0 | L3 | memory branch/diff/rollback；see also doc/50 §大膽押注: Belief Git |

### 5.3 Cognition Tree

```text
Cognition
├─ LLM Router
├─ Context Budget
├─ Prompt Stack
├─ Reflection
├─ Judge
├─ Skill Mutator
└─ Predictive Context  [future]
```

| Skill | Current | Target | 升級條件 |
|-------|---------|--------|----------|
| LLM Router | L3 | L4 | provider capability matrix；模型降級策略可測 |
| Context Budget | L3 | L4 | compression quality eval；large-turn stress tests |
| Judge | L2 | L4 | false positive / false negative corpus；verdict replay |
| Reflection | L3 | L5 | 反思結果能改善後續任務分數 |
| Predictive Context | L1 | L3 | prefetch 命中率、噪音率、token ROI 可觀測 |

### 5.4 Autonomy Tree

```text
Autonomy
├─ Cron Trigger
├─ Event Trigger
├─ Condition Trigger
├─ Action Planner
├─ Notification
├─ Dry-run Simulator  [future]
└─ Consensus Gate     [future]
```

| Skill | Current | Target | 升級條件 |
|-------|---------|--------|----------|
| Trigger Runtime | L3 | L4 | missed-fire / duplicate-fire tests；time-zone replay |
| Action Planner | L2 | L4 | dry-run plan preview；risk scoring；policy integration |
| Autonomy Safety | L2 | L4 | sandbox backend + explicit egress policy |
| Dry-run Simulator | L0 | L3 | cron/event 可模擬，不產生副作用 |
| Consensus Gate | L0 | L3 | high-risk autonomy 可要求 critic/reviewer agent 簽核 |

### 5.5 Platform Tree

```text
Platform
├─ CLI
├─ TUI
├─ Discord
├─ REST API
├─ Notification
└─ Unified Projection  [future]
```

| Skill | Current | Target | 升級條件 |
|-------|---------|--------|----------|
| CLI Runtime | L3 | L4 | stream/pause/abort/resume 有 replay tests |
| TUI Control Surface | L3 | L4 | 完全從 event schema rendering |
| Discord Surface | L3 | L4 | confirm parity、event parity、long output policy 穩定 |
| REST API | L1 | L3 | auth、schema、job state、memory endpoints 明確 |
| Unified Projection | L0 | L4 | CLI/TUI/Discord/API 都讀同一份 AgentLedger |

### 5.6 Security Tree

```text
Security
├─ Trust Level
├─ Scope Grants
├─ Command Scanner
├─ Self-termination Guard
├─ Secret Redaction
├─ Sandbox Backend  [future]
└─ Policy-as-code   [future]
```

| Skill | Current | Target | 升級條件 |
|-------|---------|--------|----------|
| Trust + Scope | L4 | L5 | policy-as-code；least privilege defaults |
| Command Scanner | L3 | L3 | 保持定位為 tripwire，不升格為安全邊界 |
| Secret Redaction | L3 | L4 | tool output、session log、scratchpad 都有一致 redaction |
| Sandbox Backend | L0-L1 | L4 | container/OS isolation；fs/network/env policy |
| Policy-as-code | L0 | L3 | `loom.toml` 可宣告 capability firewall |

---

## 6. Buffs & Debuffs

### Current Buffs

| Buff | 效果 |
|------|------|
| Harness-first Architecture | +20 control reliability |
| Scope-aware Permission | +15 operator trust |
| Memory Governance | +18 long-term coherence |
| Action Lifecycle State Machine | +18 auditability |
| Architecture Guard Tests | +10 maintainability |
| Multi-platform Control Surface | +12 operator reach |

### Current Debuffs

| Debuff | 影響 |
|--------|------|
| No Hard Sandbox Wall | -30 autonomy safety |
| LoomSession Gravity Well | -18 modularity |
| Doc / Runtime Drift Risk | -12 onboarding clarity |
| Skill Evolution Eval Gap | -15 self-improvement trust |
| Event Stream Fragmentation | -16 replayability |
| External Tool Policy Gap | -14 MCP / web safety |

### Debuff Removal Priority

| Priority | Debuff | 建議主線 |
|----------|--------|----------|
| P0 | No Hard Sandbox Wall | Quest A: Build the Sandbox Wall |
| P0 | Event Stream Fragmentation | Quest B: Unify the Event River |
| P1 | LoomSession Gravity Well | Quest C: Split the Runtime Core |
| P1 | Skill Evolution Eval Gap | Quest D: Build the Evolution Arena |
| P2 | Doc / Runtime Drift Risk | Quest E: Make Docs Testable |

---

## 7. Main Quests

### Quest A: Build the Sandbox Wall

**Goal**：把 Loom 從 policy/tripwire 級安全，推進到真正 isolation-backed execution。

Reward：

- OS Sandbox: D -> B+
- Autonomy Safety: C -> B
- Enterprise Readiness: C -> B-

Exit Criteria：

- `ExecutionBackend` abstraction exists.
- `run_bash` supports local backend and at least one isolated backend.
- Filesystem writes are limited by explicit mounts or allowlist.
- Network egress can deny by default.
- Environment secrets are projected explicitly, not inherited implicitly.
- Regression tests cover path escape, env exfiltration, network denial, process cancellation.

### Quest B: Unify the Event River

**Goal**：把 ActionRecord、ExecutionEnvelope、SessionLog、TaskList、MemoryWrite、JudgeVerdict、Artifact 都放進同一條語義時間線。

Reward：

- Observability: B -> A
- Replayability: D -> B+
- Platform Consistency: B- -> A-

Exit Criteria：

- 定義 `AgentLedger` event schema。
- 每個 turn 可輸出完整 ledger。
- CLI/TUI/Discord/API 都能從 ledger projection rendering。
- Memory compression 可從 ledger 提取 facts。
- 至少一個 historical session 可被 replay。

### Quest C: Split the Runtime Core

**Goal**：降低 `LoomSession` 的重力，讓新增能力不必全部塞進同一個中樞。

Reward：

- Maintainability: B- -> A-
- Extensibility: B -> A-
- Testability: B -> A-

Exit Criteria：

- 抽出 `ToolSurface` 負責工具註冊。
- 抽出 `MemoryRuntime` 負責 memory facade / pulse / compression。
- 抽出 `ControlRuntime` 負責 confirmation / grants / pause / abort。
- 抽出 `EventRuntime` 負責 normalized events。
- `loom.core.session` 不再 lazy import platform tool factories。

### Quest D: Build the Evolution Arena

**Goal**：讓 skill mutation、reflection、dreaming 不只是會產生內容，而是能證明「變好了」。

Reward：

- Skill Evolution: C+ -> A-
- Memory Trust: B -> A
- Self-improvement Credibility: C -> A-

Exit Criteria：

- 建立 replay corpus：失敗 trace、成功 trace、edge cases。
- Skill candidate promotion 需要 metric 改善。
- Dreaming triples 有 usefulness / contradiction / staleness 評估。
- Judge verdict 有 false positive / false negative corpus。
- 每次 promoted skill 都能連到 evidence。

### Quest E: Make Docs Testable

**Goal**：讓 Loom 的豐富文件不變成平行宇宙。

Reward：

- Documentation Reliability: B- -> A
- Onboarding Clarity: B -> A
- Release Confidence: B -> A-

Exit Criteria：

- Doc 中的 `loom.xxx` import path 可被自動檢查。
- Doc 中聲稱「已實作」的功能要能連到測試或 symbol。
- README version history、`pyproject.toml` version、`doc/SUMMARY.md` 狀態有 drift check。
- Release 前自動產生 doc drift report。

---

## 8. Shortboard Watchlist

每次 review 只追蹤不超過 10 個最短板，避免列表膨脹。

| Rank | Shortboard Item | Why It Matters | Target Quest |
|------|-----------------|----------------|--------------|
| 1 | OS-level sandbox | 自治能力的安全天花板 | Quest A |
| 2 | Unified AgentLedger | replay、UI、memory compression 的共同底座 | Quest B |
| 3 | LoomSession decomposition | 降低後續功能開發阻力 | Quest C |
| 4 | Skill evolution eval | 防止自我修改變成不可證明的漂移 | Quest D |
| 5 | Doc drift CI | 保護文件資產 | Quest E |
| 6 | Capability firewall | 比 TrustLevel 更細的 policy layer | Quest A |
| 7 | Autonomy dry-run simulator | 讓無人值守任務先演練 | Quest D |
| 8 | MCP external tool policy | 防止外部工具擴大攻擊面 | Quest A |
| 9 | Memory diff / rollback | 讓 belief state 可審計 | Quest D |
| 10 | Platform projection parity | CLI/TUI/Discord/API 語義一致 | Quest B |

---

## 9. Upgrade Rules

能力不能靠主觀感覺升級。建議規則如下：

### Level 1 -> Level 2

- 有可用 API 或 CLI path。
- 有至少一個單元測試。
- 有基本文件或 docstring。

### Level 2 -> Level 3

- 有 regression tests 覆蓋主要失敗模式。
- 錯誤有 structured failure type。
- 行為有清楚 contract。

### Level 3 -> Level 4

- 可觀測：事件、trace、telemetry 或 audit log。
- 可恢復：rollback、retry、resume 或 safe failure。
- 跨平台一致：CLI/TUI/Discord/API 不各自發明語義。
- 有 operator-facing 說明。

### Level 4 -> Level 5

- 有 replay 或 eval corpus。
- 能透過指標證明改善。
- 自動演化結果可審查、可撤銷。
- 失敗案例會回流成測試或技能修正。

---

## 10. Review Cadence

### 每週輕量檢查

- 更新 Shortboard Watchlist。
- 檢查是否新增 debuff。
- 檢查 P0 quest 是否被阻塞。

### 每個 minor release

- 重評 Capability Radar。
- 每個分數變動必須寫一句 evidence。
- 新增或移除 buff/debuff。
- 將重大失敗案例轉成 eval 或 regression test。

### 每個 milestone

- 重畫 Skill Trees。
- 檢查 Level 4/5 的能力是否真的有 replay/eval 支撐。
- 檢查主線任務是否仍符合 Loom 定位。

---

## 11. 建議文件產物

本評級系統可以衍生幾份更具體的管理文件：

| 文件 | 用途 |
|------|------|
| `CAPABILITY_SHEET.md` | 每次 release 更新的角色卡快照 |
| `CAPABILITY_CHANGELOG.md` | 能力升降級記錄 |
| `QUEST_BOARD.md` | 主線任務、exit criteria、進度 |
| `DEBUFF_REGISTER.md` | 已知架構扣分項與解除條件 |
| `EVIDENCE_INDEX.md` | capability -> tests/docs/symbols/traces 對照 |

不一定要一次建立。建議先讓本文件成為規格，之後在 release 節奏穩定後再拆出快照文件。

---

## 12. 最終建議

這套 RPG 能力評級的價值，不在於讓 Loom 看起來有趣，而在於把 roadmap 從「做更多功能」轉成「讓角色成長」。

一個好的能力表應該讓你一眼看出：

- 目前最強的武器是什麼。
- 哪個 debuff 正在限制上限。
- 下一個 quest 完成後，哪些屬性會升級。
- 哪些能力只是有 prototype，還沒有可靠性。
- 哪些自我演化結果真的被 evidence 支撐。

如果 Loom 未來要成為長期、可信、可自治的 agent runtime，這份 Capability Sheet 可以成為它的成長儀表板與工程羅盤。

