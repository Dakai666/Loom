# TUI 使用指南

Loom TUI（Textual User Interface）是基於 Textual 8 框架的終端圖形化介面，採用 **Parchment 羊皮紙配色**，在深色終端上提供溫潤不刺眼的視覺體驗。

---

## 啟動 TUI

```bash
# 啟動 TUI（自動恢復最近一次 session）
loom chat --tui

# 啟動時指定模型
loom chat --tui --model claude-sonnet-4-6

# 啟動全新 session（不恢復）
loom chat --tui --no-resume

# 恢復指定 session
loom chat --tui --session <session_id>
```

---

## 整體佈局（75:25）

```
┌─ Header (3 rows) ──────────────────────────────────────────────────┐
│  Loom v0.2.3  │  ◌ Thinking...  │  claude-sonnet-4-6  ·  memory.db │
├────────────────────────────────────────┬───────────────────────────┤
│  Conversation Pane (75%)               │  Workspace Panel (25%)    │
│                                        │                           │
│  [User] 你好                           │  Art  Act  Bgt   F2       │
│                                        │  ─────────────────────    │
│  [Assistant]                           │  (當前 tab 內容)          │
│  ▸ thinking  (點擊展開推理鏈)          │                           │
│  The answer is...                      │                           │
│                                        │                           │
│  ◌ Thinking...                         │                           │
│  ⟳ read_file — "loom/core/..."         │                           │
│                                        │                           │
│  > 輸入訊息...（Tab 自動補全）         │                           │
├────────────────────────────────────────┴───────────────────────────┤
│  ctx ████████░░ 62%  |  48.2k in / 1.3k out  |  2.3s              │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Agent 狀態指示器

ToolBlock 與 Header 同步顯示 agent 當前動作：

| 狀態 | 顯示 | 觸發條件 |
|------|------|----------|
| IDLE | （不顯示）| 閒置 |
| THINKING | `◌ Thinking...`（三點動畫）| 每次 turn 開始、每個工具呼叫完成後 |
| RUNNING | `⟳ read_file — "path/to/file"` | 工具執行中 |
| DONE | `✓ Done`（1.5s 後消失）| turn 完成 |

---

## Workspace Panel 三個 Tab

按 **F2** 或點擊 tab 標籤切換（`Art` / `Act` / `Bgt`）。

### Tab 1：Artifacts（Art）

顯示本 session 所有 `write_file` 產出的檔案：

```
✚ loom/core/tasks/scheduler.py    created    2m ago
~ loom/core/harness/middleware.py  modified   5m ago
```

### Tab 2：Activity Log（Act）

本 session 所有工具呼叫的時間軸：

```
5 calls  ·  1.3s total
────────────────────────────────
✓ read_file      middleware.py   45ms
✓ list_dir       loom/core/      12ms
✗ run_bash       pytest         234ms
  └ Exit 1: 3 tests failed          ← 點擊展開/折疊錯誤
✓ run_bash       pytest -x     1.2s
⟳ read_file      tasks/...   running
```

### Tab 3：Budget（Bgt）

Context 使用量監控：

```
Context Usage
████████████░░░░░░░░  62%
62.0k / 100.0k tokens

⚡ Auto-compact at 80%
   18,000 tokens remaining (~3 turns)

🔴 Force-compress at 95%
   33,000 tokens remaining

This turn
   In    48.2k tokens
   Out    1.3k tokens
```

進度條依用量變色：`< 60%` 鼠尾草綠 → `60-80%` 赭石橙 → `80-95%` 暖橙 → `> 95%` 磚紅。

---

## 快捷鍵

| 按鍵 | 功能 |
|------|------|
| `Escape` | 中斷當前 agent 生成 |
| `F1` | Toggle verbose 工具輸出 |
| `F2` | Workspace tab 循環（Art → Act → Bgt） |
| `Ctrl+L` | 清除對話視圖（不清歷史） |
| `Ctrl+C` | 退出 TUI |
| `Tab` | 自動補全 slash command |
| `Y` / `N` | 工具確認對話框：允許 / 拒絕 |

---

## Slash 命令

在輸入框輸入 `/` 後按 Tab 可自動補全。

| 命令 | 功能 |
|------|------|
| `/think` | 開啟上一回覆的完整推理鏈（ThinkModal） |
| `/compact` | 手動壓縮較舊的對話上下文 |
| `/sessions` | 開啟 session 選擇器，切換至其他 session |
| `/new` | 結束當前 session，開始全新 session |
| `/personality <name>` | 切換認知人格（adversarial / minimalist / architect / researcher / operator） |
| `/personality off` | 移除當前人格設定 |
| `/verbose` | Toggle 工具輸出詳細度 |
| `/help` | 顯示所有命令與快捷鍵的 HelpModal |

---

## 推理鏈查看（ThinkModal）

當 assistant 的回覆含有 `<think>` 推理過程時，回覆上方會出現：

```
▸ thinking  (click to expand)
```

點擊可開啟 ThinkModal 檢視完整推理鏈，或輸入 `/think` 達到相同效果。

- **Escape** / **Enter** / **Close 按鈕** 關閉
- 文字框可上下捲動

---

## 工具確認對話框（ConfirmModal）

執行 GUARDED / CRITICAL 工具時，會彈出確認對話框：

```
╔══ GUARDED ════════════════╗
║  web_search               ║
║  query="latest news"      ║
║                           ║
║  [Allow]      [Deny]      ║
╚═══════════════════════════╝
```

| 動作 | 按鍵 |
|------|------|
| 允許 | `Y` 或點擊 Allow |
| 拒絕 | `N` 或點擊 Deny |
| 拒絕 | `Escape` |

GUARDED 授權後本 session 不再詢問；CRITICAL 每次都需確認。

---

## Parchment 配色

| 用途 | 色值 |
|------|------|
| Screen 背景 | `#1c1814` |
| Widget 表面 | `#242018` |
| 主文字 | `#e0cfa0`（米白） |
| 強調/焦點 | `#c8a464`（琥珀）|
| 成功 | `#7a9e78`（霧綠）|
| 警告/GUARDED | `#c8924a`（赭石）|
| 錯誤/CRITICAL | `#b87060`（磚紅）|

---

## 疑難排解

### 終端顏色顯示異常

TUI 使用 24-bit 真彩色。若終端不支援，請確認：

```bash
echo $COLORTERM   # 應為 truecolor 或 24bit
```

Windows Terminal / iTerm2 / Alacritty 均支援。

### 中文輸入問題

確認終端編碼為 UTF-8：

```bash
export LANG=en_US.UTF-8
loom chat --tui
```

### Textual 版本

TUI 需要 Textual 8.x：

```bash
pip show textual   # 確認版本 >= 8.0
```
