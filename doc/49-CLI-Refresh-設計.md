# CLI Refresh 設計

本文件記錄 **Loom CLI 介面重構** 的動機、設計哲學、訊息分流策略與 PR 拆分計畫。

替代目標：完成後將取代 `34-TUI-使用指南.md`（屆時 Dashboard 模式淘汰，本文成為 CLI 互動的權威說明）。

關聯：原始討論起點為 issue #160（已關閉，改開新 issue 追蹤本計畫）。

---

## 動機

原 issue #160 起意為「修 TUI 的 markdown 顯示」。深入後重新定義為更根本的問題：

- 現行 `ExecutionDashboard` 全螢幕固定窗格，限制歷史滾動，視覺資訊過載
- CLI 端與 TUI 端風格分裂，CLI 缺乏羊皮卷美學、缺乏結構化訊息分層
- prompt_toolkit 級別的現代輸入體驗缺席（無多行擴張、無 slash 補全、無 abort-on-submit）
- Harness 訊息與絲絲對話視覺混雜，使用者難以分辨「誰在說話」

核心心法：**「底層架構越複雜（Harness, Memory, Jobs），前端介面就要裝得越輕鬆。」**

---

## 設計哲學

### 1. 漸進式揭露 (Progressive Disclosure)

捨棄全螢幕 Dashboard，擁抱 **Linear Stream + 1 行 Live Footer** 的混合形態：

- **歷史軸**：append-only linear stream，自然滾動，留底
- **當下軸**：底部 1–2 行 `rich.live` footer，只顯示 active envelope / token budget / grant TTL
- **完成即蒸發**：turn 結束 footer 收回，歷史區留下 solidified envelope panel

### 2. 狀態固化 (Solidification)

執行中的視覺元件是動態的（spinner、閃爍、活躍色）；一旦 `COMMITTED` 或 `ABORTED`，**3 秒後降一階變灰**，自然把視覺重心推給最新內容。

### 3. 並行不展平

Loom 有 Claude Code 沒有的 parallel envelope dispatch（見 `_build_envelope_view()`，#128 之後更明確）。純線性流會逼出兩個壞選擇：強制序列化（騙人）、交錯插入（撕裂）。

正解：**parallel block 用 collapsible group panel 收住**，期間多個 spinner 同時轉，全部 COMMITTED 後 collapse 成 `✓ 3 tools · 4.2s`，可展開。

### 4. 優雅的人類搶佔 (Graceful Interruption)

「打斷」是正常互動，不是錯誤：

- **送出即剎車**：使用者按 Enter 的瞬間，若有 turn 在跑，先 `session.cancel()` 再 queue 新訊息
- **視覺反饋**：運作中的元件**不直接消失**，轉為 `⏸ ABORTED` 灰色標籤
- **Context 接力**：下個 turn 開頭注入 system note `[使用者打斷上輪並接續]`，確保絲絲認知連貫
- 底層已有 `call.abort_signal: asyncio.Event`、`session.cancel()`、`/stop`、Ctrl+C、Escape——只缺「Enter 送出 = 自動剎車」這條邊

---

## Harness 訊息三頻道分流

### 分流判準

```
                  能做事 ──┐
                            ▼
               ┌─ 不知道會出事 ──→ 跳出 (modal)
時間敏感 ──────┤
               └─ 知道也沒差 ──→ 底層 (footer)
                            ▲
                  能感知 ──┘

非時間敏感 ─────────────→ 流式 (inline，留底)
```

### 完整訊息來源對照表（13 類）

| # | 來源 | 範例 | 頻道 |
|---|---|---|---|
| 1 | EnvelopeStarted/Updated/Completed | tool 執行框 | 流式（panel 主體）+ 底層（執行中摘要） |
| 2a | `_notify_lifecycle` 綠燈 | `pre-authorized`, `exec_auto`, `scope-allow` | **底層**（閃 0.3s 不留底） |
| 2b | `_notify_lifecycle` 紅燈 | `user denied`, `circuit breaker tripped` | 流式（forensics 留底） |
| 3 | Confirm prompt | 工具授權互動 | **跳出**（modal） |
| 4 | Scope grant 狀態 | TTL 倒數、過期清掃 | 底層（最快過期那個） |
| 5 | Compaction | `Compacting context (87% used)…` | 底層（執行中）+ 流式（完成摘要） |
| 6 | History sanitize | orphaned tool_calls 修復 | 流式（**只在真的修了才喊**） |
| 7 | Session resume / diagnostic | `Resuming session abc123` | 流式 |
| 8 | Model / Personality 切換 | `Model switched to: ...` | 流式 |
| 9 | Token budget | input_tokens / context % | 底層（**>60% 才浮出**） |
| 10 | NotificationRouter | autonomy daemon、external trigger | 流式 |
| 11 | MemoryGovernor | governed_upsert | 流式（**只顯示 reject，accept 沉默**） |
| 12 | Reasoning chain | `/reasoning` 回看 | 使用者主動呼叫 |
| 13 | Error / fatal | API 2013、provider 拒絕 | 流式（一般）/ 跳出（fatal recovery） |

### 三條設計原則

1. **綠燈不出聲，紅燈才講話** — `pre-authorized` / `exec_auto` / `governor accept` 不佔流式空間，降為 footer 閃光
2. **Sanitize 必須可見** — history 修復是 invariant 級事件；靜默修復會讓使用者懷疑絲絲怪怪的，但只在「真的修了」才喊
3. **流式訊息一律帶 `⚙ harness ›` 署名** — 與絲絲文本視覺切開；modal 跳出用 `[!]` 框，自然有阻斷感不需署名

---

## 視覺族群（絲絲 vs Harness vs Tool）

| 族群 | 來源 | 視覺 | 範例 |
|---|---|---|---|
| **絲絲** | assistant text / think | 無框、奶油 `#e0cfa0`、左緣 `絲 ▎` 絲綢色引線 | 對話、思考、回答 |
| **Harness** | envelope state、grant、sanitize、token | 暗背景 `#242018` 整段反白、前綴 `⚙ harness ›` 琥珀金 | `⚙ harness › grant L2 expires in 0:43` |
| **Tool** | ExecutionEnvelope | rounded panel，標題色隨狀態 | 工具執行框 |

**絕不混血**：Harness 不能用奶油色文字假裝是絲絲在說話。

---

## 色彩系統

沿用 TUI 既有羊皮卷 palette（`loom/platform/cli/tui/app.py:7-15`），CLI 端建立 `rich.theme.Theme`：

```python
LOOM_THEME = Theme({
    "loom.text":    "#e0cfa0",  # 奶油（主文字）
    "loom.muted":   "#8a7a5e",  # 米褐（dim）
    "loom.accent":  "#c8a464",  # 琥珀金（重點）
    "loom.success": "#7a9e78",  # 鼠尾草（成功）
    "loom.warning": "#c8924a",  # 赭石（警告）
    "loom.error":   "#b87060",  # 赤陶（錯誤）
    "loom.border":  "#4a4038",  # 邊框
    "loom.harness.bg": "#242018",  # harness 訊息底色
})
```

狀態三階淡出：active → committed → frozen（3 秒後降一階）。

---

## 互動：Confirm Prompt

純 y/n/c 過時。既然 PR-A 引入 prompt_toolkit，arrow + enter 是天然來的：

```
⚠  絲絲想執行 run_bash                      ⚙ L2 · 信任度 GUARDED
   rm -rf ./build/dist

 ▸ 允許這次
   允許並記住 5 分鐘 (升 grant)
   允許並記住到 session 結束
   拒絕
   查看完整參數…

   ↑↓ 選擇  ⏎ 確認  esc 取消  · y/n/c 仍可直接按
```

關鍵：

- **方向鍵預設**，但 `y/n/c` 鍵盤快捷保留（肌肉記憶 + 低頻寬場景）
- **選項動態生成**：根據當前 trust level 與既有 grant 過濾
- **「查看完整參數」**：tool args 太長時不直接展開塞 prompt 區
- **rich.live 暫停**：選擇期間 footer/spinner 凍結，靠 `patch_stdout` 處理重繪

實作層面這是 PR-A 的 sub-mode，不額外開 PR。

---

## prompt_toolkit 輸入層

```
LoomPrompt (prompt_toolkit Application)
├─ multiline=True, height=Dynamic(1..10)        # 隨內容擴張，> 10 行才滾動
├─ patch_stdout()                                # 背景輸出不洗稿
├─ KeyBindings:
│   ├─ Enter (no shift)  → submit + 若 turn 在跑：先 cancel 再 queue
│   ├─ Shift+Enter       → 插入換行
│   ├─ Esc / Ctrl+C      → 純 cancel，不送
│   └─ Ctrl+D (空 buffer) → 退出
├─ Completer：slash 指令 fuzzy auto-complete
├─ History：~/.loom/cli_history（FileHistory）
└─ AutoSuggestFromHistory                        # fish-style 灰字補完
```

多行貼上天然處理；換行不誤觸送出；slash 指令補全沿用現有 dispatch table。

---

## TaskList 可視化

PR #206 收斂為單一 `task_write`，UI 只需監聽一個事件：

```
╭─ 📋 task list ───────────────────────────────╮
│ ✓ 看清楚 issue #160                          │
│ ✓ 評估 rich vs textual                       │
│ ▸ 設計 prompt_toolkit 輸入層      ← active  │
│ ○ 實作 abort-on-submit                       │
│ ○ harness 訊息視覺切分                       │
╰──────────────────────────────────────────────╯
```

- `rich.live` 原地重繪，每次 `task_write` 觸發重渲
- 浮動於 footer 上方，**不是歷史的一部分**（不會被滾動沖走）
- turn 結束若無變動 → fade 成灰 frozen 樣式
- 全部完成 → collapse 成一行 `✓ 5/5 done`，可展開

---

## PR 拆分與依賴

```
PR-A  輸入層改造：prompt_toolkit + multiline + abort-on-submit + arrow-key confirm
        deps: 無
        解鎖：所有後續工作的互動基礎

PR-B  Rich Theme + 語義 color token
        deps: 無（可與 A 並行）
        純 refactor

PR-C  Harness vs 絲絲 視覺切分（含三頻道分流）
        deps: B
        副作用：清理 _notify_lifecycle 綠燈訊息，降為 footer

PR-D  Linear stream + 1 行 live footer + Solidification + 並行 group panel
        deps: B, C
        替換現有 ExecutionDashboard 渲染

PR-E  TaskList live 浮動面板
        deps: D
        監聽 task_write，rich.live 重繪
```

順序心法：A 給互動底層，B 給視覺底層，C 釐清訊息族群，D 線性化，E 是 D 上的小工。

建議掛新 milestone「CLI Refresh」，與 Harness Hardening #1 並列。

---

## 已拍板決策

- [x] 採 Linear Stream + 1 行 Live Footer 混合，不沿用 Dashboard
- [x] 並行 envelope 用 collapsible group panel
- [x] 沿用 TUI 羊皮卷 palette，CLI 端轉為 `rich.theme.Theme`
- [x] 絲絲 / Harness / Tool 三族群視覺強切分
- [x] 綠燈訊息（`pre-authorized` / `exec_auto` / `scope-allow`）降為 footer 閃光，不留底
- [x] Token budget 採 C 方案：<60% 完全隱藏，>60% 才浮出（呼吸感優先）
- [x] Confirm prompt 用方向鍵 + enter，y/n/c 快捷保留
- [x] Scratchpad 概念暫不引入（草稿留給絲絲自己看，需要時直接問）
- [x] Sanitize 修復必須有可見訊息，但僅在真的修復時觸發

---

## 未決問題

- 「查看完整參數」展開後是 inline 還是另開 pager？需要實作時決定
- TaskList collapse 的快捷鍵綁定（避免與 vim-style nav 衝突）
- Discord 端是否同步引入這些族群分類？目前 Discord 已有部分分流（`ThinkCollapsed`、slash command 區隔），需要對齊
- footer 在窄終端（< 80 col）的退化策略

## 已知 bug（待 PR-D 一併處理）

- **Streaming 行首截斷**（A1 階段確認為老 bug，非 A1 引入）：
  CLI streaming 輸出長 CJK 段落或多項列表時，個別行的開頭會被吃掉（例如 `3. 數字...` 整段消失、`5. ...` 只剩尾段）。
  根因推測為 `clear_line()` 用 `\r\033[K` 清行，在 soft-wrap + CJK 字元寬度判定誤差時會清到錯的視覺行，下一個 chunk 從錯位置覆寫前內容。
  Discord 不受影響（沒有 ANSI raw clear）。TUI 不受影響（用 Textual 自己的渲染）。
  PR-D 重寫 stream renderer 時一併解決——避免在 A1 scope 修，會讓 PR 失焦。

- **Multiline 輸入暫不啟用**：
  `multiline=True` 與 Rich 的 `\r\033[K` clear_line 互動會放大上面那個截斷 bug。A1 階段已將 `multiline=False`，Alt+Enter 換行能力延到 PR-D 與 stream renderer 重寫一起做。

---

## 參考

- 原始討論：issue #160（已關）→ 新 issue（CLI Refresh tracking）
- 既有相關文件：`43-Harness-Execution-可視化規劃.md`、`45-AbortController.md`
- 將取代：`34-TUI-使用指南.md`（PR-D 完成後廢除）
