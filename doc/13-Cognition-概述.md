# Cognition Layer 概述

Cognition Layer（認知層）是 Loom 框架的「思考引擎」。它負責模型路由、Token 配額管理、與 Session 運行時的自我反思。

```
┌─────────────────────────────────────────────────────────────┐
│                    Loom 架構圖                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────┐     ┌─────────────┐     ┌─────────────┐ │
│   │   Harness   │────▶│  Cognition  │◀────│   Memory    │ │
│   │   Layer     │     │   Layer     │     │   Layer     │ │
│   └─────────────┘     └─────────────┘     └─────────────┘ │
│         │                   │                   │          │
│         │                   ▼                   │          │
│         │            ┌─────────────┐            │          │
│         └───────────▶│  Prompt     │◀───────────┘          │
│                      │  Stack      │                       │
│                      └─────────────┘                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 三個核心模組

| 模組 | 職責 | 核心概念 |
|------|------|----------|
| **LLM Router** | 決定每個請求發往哪個模型 | 前綴路由、多 provider 支援 |
| **Context Budget** | 管理 Token 配額與自動壓縮 | 配額桶、壓縮觸發線 |
| **Reflection API** | session 結束時的自我反思 | 摘要生成、健康報告、Anti-pattern 分析 |

---

## LLM Router

### 問題：為什麼需要路由？

同一個 agent 可能需要：
- 快速回覆 → 用 MiniMax-M2.7
- 複雜推理 → 用 o4-mini
- 工具調用 → 用 GPT-4o（工具支援好）

LLM Router 讓 Loom 根據請求的「意圖前綴」自動選擇最適合的模型。

### 前綴路由範例

```
user: "幫我解釋這個錯誤"     → /general   → MiniMax-M2.7
user: "分析這段代碼邏輯"     → /reasoning → o4-mini
user: "規劃這個功能的實現"    → /reasoning → o4-mini
user: "幫我查詢數據庫"       → /tools     → GPT-4o
user: "翻譯這段文字"         → /general   → MiniMax-M2.7
```

### 多 Provider 支援

Loom 不綁定單一 API 供應商。LLM Router 支援：

| Provider | 用途 | 配置 key |
|----------|------|----------|
| MiniMax | 日常對話、性價比 | `MINIMAX_API_KEY` |
| Anthropic | 複雜推理、工具調用 | `ANTHROPIC_API_KEY` |
| OpenAI | 工具調用、相容性 | `OPENAI_API_KEY` |
| Azure OpenAI | 企業部署 | `AZURE_OPENAI_*`（Phase X）|

詳見 [14-LLM-Router.md](14-LLM-Router.md)。

---

## Context Budget

### 問題：Context Window 是有限資源

每個模型都有 context window 限制：

| 模型 | Context Window |
|------|----------------|
| MiniMax-M2.7 | 32K tokens |
| GPT-4o | 128K tokens |
| o4-mini | 64K tokens |

當對話越來越長，context 會接近上限。Context Budget 負責：

1. **配額追蹤** — 每個 session 有獨立的 token 配額
2. **壓縮觸發** — 當配額即將耗盡時，自動執行摘要
3. **優先級管理** — system prompt > tool result > history

詳見 [15-Context-Budget.md](15-Context-Budget.md)。

### 壓縮觸發時機

```python
# 觸發壓縮的時機
if budget.used_ratio > 0.8:     # 已使用 80%
    await budget.trigger_compression()
elif budget.remaining < 2000:     # 剩餘少於 2000 tokens
    await budget.trigger_compression()
```

---

## Reflection API

### 問題：每次 session 都是獨立的？

如果每個 session 都從零開始，agent 無法：
- 記住上次做了什麼決定
- 改進自己的工具使用策略
- 追蹤 skill 的準確度變化

Reflection API 在 session 結束時執行「自我反思」，生成：

| 產出 | 用途 |
|------|------|
| **Session Summary** | 壓縮後寫入 Episodic Memory |
| **Tool Report** | 每個工具的成功率寫入 Skill Genome |
| **Health Report** | Skill confidence 趨勢、需關注的問題 |
| **Counter-factual** | Anti-pattern 分析寫入 Semantic + Relational Memory |

詳見 [16-Reflection-API.md](16-Reflection-API.md)。

### Counter-factual Reflection（v0.2.5.1）

當工具執行失敗且有 SkillGenome 記錄時，觸發反事實反思：

```
execution_error 發生
    ↓
LLM 問：「什麼 pattern 導致失敗？下次應避免什麼？」
    ↓
寫入 SemanticMemory → skill:<name>:anti_pattern:<timestamp>
寫入 RelationalMemory → (loom-self, should_avoid:<tool_name>, <行為>)
```

Session 開始時，MemoryIndex 讀取 `should_avoid` 三元組，agent 在進入對話前就知道自己踩過的坑。

### 觸發時機

Reflection 可手動觸發或自動觸發：

```bash
# 手動觸發
loom reflect

# 自動觸發（loom.toml 設定）
[autonomy]
auto_reflect_on_exit = true
```

---

## 協作流程

```
┌──────────────────────────────────────────────────────────┐
│                   一次完整對話流程                         │
├──────────────────────────────────────────────────────────┤
│                                                          │
│  1. Session 開始                                         │
│     ├── MemoryIndex.build() → 注入上下文（含 Anti-pattern）│
│     └── ContextBudget.init() → 初始化配額                │
│                                                          │
│  2. 用戶訊息進入                                          │
│     ├── LLM Router.choose() → 選擇模型                   │
│     ├── ContextBudget.track() → 計入 token               │
│     └── PromptStack.build() → 組裝 prompt                │
│                                                          │
│  3. LLM 回應                                             │
│     ├── Tool calls → Harness Layer 執行                  │
│     └── 結果寫入 Memory                                   │
│                                                          │
│  4. Session 結束                                          │
│     └── Reflection API.run() → 生成 summary/report       │
│        ├── Tool Report → Skill Genome confidence 更新     │
│        ├── Counter-factual → Anti-pattern 寫入            │
│        └── SelfReflection → loom-self 三元組              │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

---

## 與其他 Layer 的關係

```
Memory Layer                    Cognition Layer
┌────────────┐                 ┌────────────────────────┐
│ Semantic   │──▶ 提供事實 ───▶│ Context Budget         │
│ Memory     │                 │ （知道有多少事實可用）    │
└────────────┘                 └────────────────────────┘
                                         │
┌────────────┐                 ┌────────▼────────┐
│ Episodic   │──▶ 提供歷史 ───▶│ Reflection API  │
│ Memory     │                 │ （壓縮進 summary）│
└────────────┘                 └─────────────────┘
                                         │
┌────────────┐                 ┌────────▼────────┐
│ Skill      │◀── 寫入 confidence◀─│ Tool Report     │
│ Genome     │                 │                 │
└────────────┘                 └─────────────────┘
                                         │
┌────────────┐                 ┌────────▼────────┐
│Relational  │◀── Anti-pattern ◀─│ Counter-factual │
│            │◀── loom-self  ◀─│ Self-Reflection │
└────────────┘                 └─────────────────┘
```

---

## 總結

Cognition Layer 是 Loom 的「智慧中樞」：

| 模組 | 解決的問題 |
|------|-----------|
| LLM Router | 讓對的請求去對的模型 |
| Context Budget | 避免 context 溢出、保持回應品質 |
| Reflection API | 讓每次 session 的經驗能累積 |
| Counter-factual Reflection | 從失敗中學習 Anti-pattern |
