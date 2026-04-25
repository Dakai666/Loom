---
name: task_list
description: Multi-step todo tracking for the main agent. Use task_write when a goal has 3+ coordinated steps and forgetting one would be costly.
tags: [core, planning, tracking]
---

# task_write — Your Sticky-Note Board

`task_write` is a **cognitive checklist** for the main agent — a reminder of what's left, not a state container. You rewrite the whole list every time you want to update it.

## Mental Model

```
┌─────────────────────────────┐
│ [x] research                │  ← 已完成
│ [x] dim_a                   │
│ [→] draft                   │  ← 正在做
│ [ ] audit                   │  ← 還沒做
│ [ ] commit                  │
└─────────────────────────────┘
```

- 它**不會推著你前進**
- 它**讓你不會忘記做到哪裡**
- 每個 turn 開始前掃一眼，知道現在在哪
- 完成一項就**重寫整張清單**，把那一項的 status 改成 `completed`

## Single Tool

```python
task_write(todos=[
    {"id": "research", "content": "蒐集 X 領域論文", "status": "completed"},
    {"id": "draft",    "content": "撰寫初稿到 tmp/draft.md", "status": "in_progress"},
    {"id": "audit",    "content": "審稿", "status": "pending"},
    {"id": "commit",   "content": "寫入磁碟並更新記憶", "status": "pending"},
])
```

- **Replace 整張清單**：每次呼叫都傳完整意圖狀態，會覆蓋掉上一份
- **status enum**：`pending` / `in_progress` / `completed`（可省略，預設 `pending`）
- **沒有 result 欄位**：所有實際產出走 `write_file` 寫到磁碟
- **沒有 depends_on**：順序由你自己讀清單決定

## Discord 狀態面板（Issue #207）

每次 `task_write` 更新時，若 loom 運行在 Discord 模式下，會自動 post 一個 checkbox embed 到 Discord thread：

```
┌─────────────────────────────────────────────┐
│ 🔄 任務進度 — 2/5 完成                      │
├─────────────────────────────────────────────┤
│ ✅ scope   ✅ dim_a                          │
│ [→] draft   [ ] audit                       │
│ [ ] commit                                     │
│                                             │
│ 📝 觸發：task_write 更新 · 17:51 UTC         │
└─────────────────────────────────────────────┘
```

狀態映射：`completed` → ✅  / `in_progress` → [→]  / `pending` → [ ]

**開關**（loom.toml.example）：
```toml
[task_write]
discord_reminder = true   # true = 每次更新 post embed；false = off
```

預設開啟。在 CLI 模式（非 Discord）下，`discord_reminder` 設定不影響任何行為。

## 紀律：`in_progress` 要主動更新

**開始做一個節點前**，先 `task_write` 把它的 status 從 `pending` 改成 `in_progress`；**做完後**再次 `task_write` 改成 `completed`。框架不會自動推進——這份「自己決定走到哪」的責任感是這個設計的核心精神。

忘了更新也不會壞事，只是節點會留在 `pending`，self-check 會在 turn 結束前 nudge 你。但養成「開始前 → in_progress、做完 → completed」的習慣，看清單時才能立刻知道現在停在哪一步。

## When to use it

- 多步驟任務（3+ 協調步驟），忘了哪步代價很大
- 跨 turn 追蹤進度
- 研究 / 寫作 / 重構等需要多階段的工作

## When NOT to use it

- 單次就能做完的事
- 純對話 / 解釋
- 太瑣碎、追蹤反而加負擔

## 為什麼沒有 task_done

舊版有 `task_done(node_id, result=...)`。這個工具的「動詞」性質造成了**認知置換**：呼叫它感覺像是「向框架回報完成」，即使 result 是空的、檔案根本不存在，agent 心裡也會覺得「我推進了一步」。在長任務裡這個錯覺累積成 issue #205 觀察到的「全部 completed 但報告從未存在」。

`task_write` 是個編輯動作，不是回報動詞。你改的是自己桌上的便利貼，沒有對象、沒有觀眾、沒有儀式感——所以也沒有「報告了 = 做了」的幻覺。

## 真正的產出在哪

**所有產出都寫檔案**，TaskList 只記「我有沒有忘記做這步」：

```python
# ✅ 正確流程
write_file("tmp/draft.md", draft_content)        # 產出 → 磁碟
task_write(todos=[                               # 更新清單
    ...,
    {"id": "draft", "content": "...", "status": "completed"},
    ...,
])

# ❌ 錯誤
task_write(todos=[
    {"id": "draft", "content": "draft 內容: ...", "status": "completed"},
    # ← 試圖把產出塞進 content，這違反設計
])
```

跨步驟需要傳資料？用約定好的檔名：
```python
# step A 寫
write_file("tmp/research_summary.md", findings)

# step B 讀
content = read_file("tmp/research_summary.md")
```

## Pre-final-response self-check

Turn 結束前若你還有 `pending` / `in_progress` 的 todo，框架會注入 reminder 強迫再跑一輪。要結束的話兩條路：
- 把剩下的事做完
- 用 `task_write` 重寫清單，把放棄的項目 status 改成 `completed`，並在 content 註明放棄原因

不要靜默結束 turn 留著未完成的事。

## 範例：研究流程

```python
# 開始時
task_write(todos=[
    {"id": "scope",    "content": "與 user 確認研究範圍", "status": "in_progress"},
    {"id": "dim_a",    "content": "維度 A：寫到 tmp/dim_a.md", "status": "pending"},
    {"id": "dim_b",    "content": "維度 B：寫到 tmp/dim_b.md", "status": "pending"},
    {"id": "synth",    "content": "綜合 dim_a + dim_b → tmp/report.md", "status": "pending"},
    {"id": "commit",   "content": "報告寫入最終位置 + 更新記憶", "status": "pending"},
])

# scope 完成後
task_write(todos=[
    {"id": "scope",    "content": "...", "status": "completed"},
    {"id": "dim_a",    "content": "...", "status": "in_progress"},
    {"id": "dim_b",    "content": "...", "status": "pending"},
    {"id": "synth",    "content": "...", "status": "pending"},
    {"id": "commit",   "content": "...", "status": "pending"},
])

# ... 一直 replace 整張到全部 completed
```

清空清單：`task_write(todos=[])`.
