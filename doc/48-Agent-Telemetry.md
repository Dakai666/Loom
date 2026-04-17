# Agent Telemetry 自我觀測層

> Issue #142 — 讓 agent 能看見自己。

## 目標與設計原則

Loom 有完整的 memory health（#133）觀測記憶子系統，但 agent 本身的行為沒有鏡子可照。#142 補上這一層：

- **pull-based 優先**：預設不往 context 裡塞狀態。agent 要看時主動呼叫 `agent_health`。
- **push 只在異常時**：某維度真的越界才在 turn 邊界注入一行警示，避免變成噪音。
- **hot-path 零 I/O**：計數器都在記憶體，`persist_interval` 決定批次落盤節奏；`stop()` 最終 flush。
- **char-weighted token 歸因**：不引入 tokenizer 依賴，用字元數按比例分攤 `input_tokens`（±15% 典型誤差，總數仍是真值）。

## 三個維度（v1）

| 維度 | 觀測什麼 | 異常條件 |
|------|---------|----------|
| `tool_call` | 每工具成功/失敗/延遲 | 失敗率 > 30% 且樣本 ≥ 5 |
| `context_layout` | 上下文層的 token 佔比（SOUL / Agent / messages） | 無觸發邏輯，供查詢 |
| `memory_compression` | episodic → semantic 的 fact-yield 比率 | 近 10 次 yield < 20% 且 runs ≥ 3 |

維度集合以 `loom.toml [telemetry].dimensions` 覆寫，省略時用 `DEFAULT_DIMENSIONS`。

## 持久化

單表 `agent_telemetry`：

```sql
CREATE TABLE agent_telemetry (
    dimension   TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    payload     TEXT NOT NULL,   -- JSON snapshot
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (dimension, session_id)
);
```

一個 session 在每個維度最多佔一列，跨 session 查詢用 `json_extract`。

## 生命週期

```text
session.start()   → AgentTelemetryTracker + ensure_table()
each LLM response → context_layout.update_total(input_tokens)
each tool_end     → tool_call.record(...)
compress_session  → memory_compression.record(entries, facts)
batch boundary    → maybe_flush()（達到 persist_interval 才寫 DB）
turn boundary     → anomaly_report() 有內容就注入 <system-reminder>
session.stop()    → final flush()
```

## `agent_health` 工具

| 參數 | 效果 |
|------|------|
| _(none)_ | 完整 detail report（三個維度） |
| `minimal=true` | 一行摘要 |
| `dimension="tool_call"` | 單一維度的 detail |

工具自動在尾端附上 `anomaly_report()` 的內容（若有），agent 不用再打第二次。

## 配置

```toml
[telemetry]
enabled          = true
persist_interval = 100            # 每 N 個 hot-path 事件落盤一次
retention_days   = 30             # 保留給未來 decay 使用
# dimensions = ["tool_call", "context_layout", "memory_compression"]
```

停用：`enabled = false`。tracker 不會建立、工具也不會註冊。

## 新增維度

繼承 `DimensionTracker` 實作 `snapshot` / `render_summary` / `render_detail`，可選擇覆寫 `has_anomaly` / `describe_anomaly` / `load_from`。然後在 `_build_dimension()` 加一個分支。

## 與 `memory_health` 的差別

| | `memory_health` (#133) | `agent_telemetry` (#142) |
|---|---|---|
| 觀察對象 | 記憶子系統的 I/O 成敗 | agent 自己的行為 |
| 資料粒度 | 按 operation + 成敗 | 按 dimension，結構可變 |
| 跨 session | 有 prior session 回報 | 留空白（未來考慮） |
| Push 時機 | session 啟動注入 prior issues | turn 邊界注入異常 |

兩者並行存在、互補。
