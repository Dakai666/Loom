# PR-D 實作草稿 — Linear Stream + Live Footer + Solidification

本文件是 `doc/49-CLI-Refresh-設計.md` PR-D 的詳細實作藍圖。是 **整個 CLI Refresh milestone 工作量最大的一刀**——核心是把 PR-A 的「每輪 `prompt_async` + 短暫 widget Application」模型砍掉，換成「單一 persistent Application + 固定 bottom 區（input + footer）」。前 PR 為了快速落地引入的支點全部要拆。

---

## 1. 目標回顧

把 CLI 從「文字列印 + 偶爾彈出 widget」變成「**底部恆定區（input + footer）+ 上方自然 scrollback**」的混合形態。具體想要的觀感：

- 輸入區永遠黏在底部，多行輸入自然展開（重啟 multiline）
- 底部 1 行 footer 顯示當下狀態（token budget / active envelope / grant TTL / compaction spinner）
- Streaming 輸出在 input 區之上自然滾動，不被洗稿
- Confirm / HITL 廣告**就地展開取代 input 區**，決策後立即收回——不留 scrollback
- Loom Agent 文本左緣帶 `Loom ▎` 引線，跨行延續
- envelope 完成後三階淡出（active 黃 → committed 綠 → frozen 灰）
- 並行 envelope 群組化呈現（`2× ▸ run_bash`）

---

## 2. 架構轉換：per-iteration → persistent

### 現況（PR-A 引入的支點）

```python
# main.py _chat()
with patch_stdout(raw=True):
    while True:
        text = await prompt_session.prompt_async(...)  # 每輪新 Application
        # ...
        # confirm 時：_run_interactive(coro) 暫停 input_loop，啟動新 widget Application

# 維護 3 個 events 協調 stdin
confirm_active = asyncio.Event()
confirm_done = asyncio.Event()
input_released = asyncio.Event()
```

問題：
- 每次 `prompt_async` 起新 Application，confirm widget 也起新 Application，兩者搶 stdin → 需要 3-event 協議
- `patch_stdout` 把 stdout 改線，對 Rich `\r\033[K` 等 ANSI 互動微妙
- 沒有「常駐底部區」概念，footer 無處掛

### 目標（PR-D 的單一 Application 模型）

```python
# main.py _chat()
app = build_loom_application(session, ...)
await app.run_async()
```

整個 chat 期間**只有一個 Application**。Layout 大致：

```
┌──────────────────────────────────────────────┐
│                                              │
│  (terminal scrollback — output flows here)   │  ← 真實終端 scrollback
│  Loom ▎ context 0.3% · persona: ...          │     output 用 app.print_text()
│  Loom ▎ 你好我是 Loom，今天想做什麼？        │     往這裡寫，自然滾動
│  ⚙ harness › auth denied: ...                │
│  ▸ run_bash · 1.2s ✓                         │
│                                              │
├──────────────────────────────────────────────┤  ← Application 底部區開始
│ you › 寫個小說                              │  ← input area (multiline)
│      .........                               │     或 confirm/HITL 區
├──────────────────────────────────────────────┤
│ 🔑 L2·0:43  ⚡ tok 67%  ▸ run_bash · 1.2s   │  ← footer (height=1)
└──────────────────────────────────────────────┘
```

input area 跟 footer 是 Application 的 layout（恆定，prompt_toolkit 渲染在終端底部）。上方輸出走 scrollback——終端處理捲動。

`patch_stdout` 不再需要，因為 Application 只負責底部區，上方 scrollback 是普通 terminal output。

3-event 協議全部刪除——只有一個 Application，不存在 stdin 搶奪。

---

## 3. Layout 結構

```python
from prompt_toolkit.application import Application
from prompt_toolkit.layout import Layout, HSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition

# 1. Mode flag — controls which "bottom area" widget shows
class CLIMode:
    INPUT = "input"          # 一般狀態：使用者輸入
    CONFIRM = "confirm"      # 工具確認 widget
    PAUSE = "pause"          # HITL pause widget
    PAUSE_REDIRECT = "redirect"  # HITL redirect text input

current_mode = ["input"]  # mutable so closures see updates

# 2. Buffers / state
input_buffer = Buffer(multiline=True, ...)
redirect_buffer = Buffer(multiline=False, ...)

# 3. Bottom-area conditional containers
def is_mode(m: str):
    return Condition(lambda: current_mode[0] == m)

input_window = ConditionalContainer(
    Window(content=BufferControl(buffer=input_buffer),
           height=Dimension(min=1, max=10),
           wrap_lines=True),
    filter=is_mode(CLIMode.INPUT),
)

confirm_window = ConditionalContainer(
    Window(content=FormattedTextControl(render_confirm),
           height=Dimension(min=1, max=12)),
    filter=is_mode(CLIMode.CONFIRM),
)

pause_window = ConditionalContainer(
    Window(content=FormattedTextControl(render_pause),
           height=Dimension(min=1, max=8)),
    filter=is_mode(CLIMode.PAUSE),
)

redirect_window = ConditionalContainer(
    Window(content=BufferControl(buffer=redirect_buffer), height=1),
    filter=is_mode(CLIMode.PAUSE_REDIRECT),
)

# 4. Footer — always visible, height=1
footer_window = Window(
    height=1,
    content=FormattedTextControl(render_footer),
    style="class:footer",
)

# 5. Layout assembled
layout = Layout(HSplit([
    input_window,
    confirm_window,
    pause_window,
    redirect_window,
    footer_window,
]))

app = Application(
    layout=layout,
    key_bindings=key_bindings,  # mode-aware bindings
    style=LOOM_STYLE,
    full_screen=False,           # ← key: render only bottom, scrollback above
    mouse_support=False,
)
```

### Mode 切換語意

mode 是個簡單字串旗標（包在 list 裡讓 closures 看見變更）。切換時：

```python
def enter_confirm_mode(scope_info, options):
    confirm_state.scope = scope_info
    confirm_state.options = options
    current_mode[0] = CLIMode.CONFIRM
    app.invalidate()  # trigger redraw
    # await confirm_decision_future
    # ...
    current_mode[0] = CLIMode.INPUT
    app.invalidate()
```

`ConditionalContainer.filter` 收到變更後自動只 render 對應 window。**Confirm 結束時其畫面區域被 input_window 取代——zero scrollback 殘留**。這就是「用過即焚」的天然實現。

---

## 4. 輸出走向：`console.print` → `app.print_text`

prompt_toolkit `Application` 有個關鍵 API：

```python
from prompt_toolkit.application import in_terminal

async def print_above(formatted):
    async with in_terminal():
        # 暫時釋放底部區的繪製，把 formatted 印到 scrollback
        sys.stdout.write(formatted)
```

或用同步版本：

```python
app.print_text(formatted_text)  # 自動 above-the-bottom-area
```

Streaming output 這樣寫：

```python
async for event in session.stream_turn(text):
    if isinstance(event, TextChunk):
        # 不再用 console.print(end='')
        rendered = render_loom_agent_chunk(event.text)
        app.print_text(rendered)
    elif isinstance(event, ToolBegin):
        app.print_text(render_tool_begin(event))
        footer_state.active_envelopes.append(...)
        app.invalidate()
    # ...
```

對 Rich 物件（Panel, Text）：用 `Console.capture()` 把 Rich 渲染成 ANSI 字串再丟進 `app.print_text`：

```python
from rich.console import Console
_render_console = Console(theme=LOOM_THEME, force_terminal=True)

def rich_to_ansi(renderable) -> str:
    with _render_console.capture() as cap:
        _render_console.print(renderable)
    return cap.get()

app.print_text(rich_to_ansi(panel))
```

這樣 Rich 的所有 markup / Panel / Rule 都能繼續用，只是渲染輸出走 prompt_toolkit 的 above-area。

---

## 5. 修 streaming 行首截斷老 bug

PR-A 階段確認的 bug：CJK soft-wrap 時 `clear_line()` 用 `\r\033[K` 清錯視覺行，下個 chunk 從錯位置覆寫。

PR-D 改寫 streaming 後**這個 bug 自然消失**，因為：
- 不再用 `\r\033[K` 清行（沒有 spinner cursor 要清——spinner 改進 footer）
- TextChunk 直接 `app.print_text` 累積，prompt_toolkit 自己處理斷行
- Streaming cursor 也由 footer 區內的小指示器處理（不再印在 inline）

---

## 6. Footer 渲染（簡單 ANSI 實作）

`render_footer` 是個 callable，每次 `app.invalidate()` 重新算：

```python
def render_footer() -> FormattedText:
    parts: list[tuple[str, str]] = []

    # Grant TTL — 最快過期那個
    grant = footer_state.next_grant_expiry
    if grant:
        parts.append(("class:footer.grant", f"🔑 {grant.label}·{format_ttl(grant)}"))
        parts.append(("", "  "))

    # Token budget — >60% 才浮出
    pct = footer_state.token_pct
    if pct > 60:
        token = "footer.budget.warn" if pct > 80 else "footer.budget.high"
        parts.append((f"class:{token}", f"⚡ tok {pct:.0f}%"))
        parts.append(("", "  "))

    # Active envelope — 多並行寫數量
    envs = footer_state.active_envelopes
    if envs:
        if len(envs) == 1:
            parts.append(("class:footer.envelope", f"▸ {envs[0].name} · {envs[0].elapsed:.1f}s"))
        else:
            latest = envs[-1]
            parts.append(("class:footer.envelope",
                         f"{len(envs)}× ▸ {latest.name} · {latest.elapsed:.1f}s"))

    # Compaction spinner
    if footer_state.compacting:
        parts.append(("class:footer.compaction", f"⚡ 壓縮中… {footer_state.compaction_pct:.0f}%"))

    return FormattedText(parts)
```

footer state 是個簡單 dataclass，session/event handler 修改後呼叫 `app.invalidate()`。**沒用 `Live`**——用 prompt_toolkit 自己的 invalidate 機制就夠。

Grant TTL 倒數**只在 turn 邊界刷新**（決議 #3）：每個 turn 開始時 `app.invalidate()` 一次重算，而不是每秒 redraw。

Compaction spinner 在 `_smart_compact` 開頭 set `footer_state.compacting = True`，`app.invalidate()`；完成後 clear + invalidate + inline 印摘要。

---

## 7. 並行 envelope group panel + 三階淡出

EnvelopeStarted/Updated/Completed 事件同上 `app.print_text`，但兩個新增行為：

### 三階淡出

每個 envelope panel 列印時帶 `created_at`。一個背景 task 每 1s 檢查：completed >3s 的 envelope 「重印一次」用 frozen 樣式覆蓋舊位置——但 scrollback 改寫困難（已經滾過的內容回不去）。

**現實妥協**：只有「**最後一個還在底部可見的 envelope**」才有三階淡出（用 `app.print_text` 重印 + cursor 上移）。已捲過的歷史就讓它保留 committed 樣式。這比 PR-D 設計的「全歷史 frozen」務實——對使用者實際感知差異不大。

### 並行 group panel

並行 envelope 在 `_build_envelope_view()` 已經有 levels >0 概念。當 view.levels > 1 時，把 envelopes 收進一個 group panel 渲染：

```
╭─ parallel · 3 tools ──────────────────╮
│ ▸ run_bash · 0.5s                     │
│ ▸ web_search · 1.2s                   │
│ ▸ minimax:text_to_image · 2.1s        │
╰────────────────────────────────────────╯
```

全部 COMMITTED 後 collapse 成一行：

```
✓ 3 tools · 4.2s   [parallel]
```

實作：監聽 `EnvelopeStarted` 事件，若 view 顯示 multi-level 就先 buffer 起 envelope events，等所有 sibling completed 後再一次印一個 group panel。

---

## 8. Confirm widget 用過即焚

新需求（user 今天加的）：`Tool requires confirmation` 整塊（含 scope panel + 選項 widget）在 user 確認/拒絕後從 scrollback 消失。

PR-D 的 layout 模型**天然滿足這需求**：

```python
async def _confirm_tool(call) -> ConfirmDecision:
    confirm_state.call = call
    confirm_state.scope = format_scope_panel(call)
    confirm_state.options = build_confirm_options(call)
    confirm_state.future = asyncio.Future()
    
    current_mode[0] = CLIMode.CONFIRM
    app.invalidate()
    
    try:
        return await confirm_state.future
    finally:
        confirm_state.call = None
        current_mode[0] = CLIMode.INPUT
        app.invalidate()
```

`render_confirm` 的輸出寫在 `confirm_window` 裡（layout 的一部分，**不是** scrollback）。當 mode 切回 INPUT，`confirm_window` 變不可見，input_window 接管那塊區域——**confirm 從未進過 scrollback，直接消失**。

scope panel 也包進 `render_confirm` 的 FormattedText 裡，**不再單獨 console.print**。這是 PR-C 的 `console.print(Panel(...))` 改點：scope panel 的存在本來就只在「決策中」，沒理由佔 scrollback。

ToolBegin / ToolEnd 自然會記下「這個 tool 跑了/被拒絕了」，所以決策結果**不會被遺失**——只是中間的 UI 過渡不留底。

---

## 9. HITL Pause 同此原理

HITL pause 也是 mode 切換：`CLIMode.PAUSE` 顯示 3 選項；選 redirect 後切到 `CLIMode.PAUSE_REDIRECT` 用 redirect_buffer 收文字；都決策完切回 INPUT。整個過程不留 scrollback。

---

## 10. Per-line `Loom ▎` 左邊引線

streaming text 跨行時要每行帶 guide。在 `render_loom_agent_chunk` 處理：

```python
def render_loom_agent_chunk(text: str) -> str:
    # split on newlines, prefix each non-first line with guide
    lines = text.split("\n")
    if len(lines) == 1:
        return text  # no newlines, no per-line guide needed for this chunk
    guide = "[loom.agent.guide]Loom ▎[/loom.agent.guide]  "
    return "\n".join(
        line if i == 0 else f"{guide}{line}"
        for i, line in enumerate(lines)
    )
```

注意：streaming 是 chunk-by-chunk 的，連續 chunk 可能斷在行中。需要在 render 層維護「上一個 chunk 是否以 newline 結尾」狀態，決定本 chunk 第一行要不要加 guide。

---

## 11. 要刪的舊 code

PR-D 刪除：

| 檔案 | 行數 | 內容 |
|---|---|---|
| `loom/platform/cli/main.py` | ~80 行 | `_INTERRUPT_PREFIX`、`confirm_active`/`confirm_done`/`input_released` events、`_run_interactive`、`patch_stdout(raw=True)` 整段 wrapper |
| `loom/platform/cli/main.py` | ~30 行 | `_run_streaming_turn` 的 spinner_task / `_print_spinner` / `_spin_loop`（spinner 改進 footer） |
| `loom/platform/cli/main.py` | ~10 行 | `clear_line()` 呼叫（不再需要） |
| `loom/platform/cli/ui.py` | `clear_line()`, `set_show_cursor`, `streaming_cursor`, `clear_line_escape` 整組 helper | 不再需要 |
| `loom/core/session.py` | `_confirm_tool_cli` 內 `console.print(Panel(...))` + `_run_interactive` routing | 改成 future-based mode 切換 |

新增：

| 檔案 | 內容 |
|---|---|
| `loom/platform/cli/app.py` | 新檔——`build_loom_application()` factory、CLIMode、FooterState、各 render_* 函式 |
| 改寫 `_chat` | 變成「build app + run_async」薄包裝 |

---

## 12. Commit 拆分

PR-D 是大刀，建議拆 4 個 commit：

| Commit | 主題 | 風險 | 依賴 |
|---|---|---|---|
| **D1** | Application skeleton：persistent app + input/footer layout，實作最簡 footer（只顯示 token budget），刪 patch_stdout / 三事件 | **高** | 無 |
| **D2** | Streaming output 改走 `app.print_text`；rich_to_ansi helper；修 CJK 截斷 bug 自然消失 | 中 | D1 |
| **D3** | Confirm + HITL mode 切換（用過即焚）；scope panel 收進 confirm_window | 中 | D1, D2 |
| **D4** | Per-line `Loom ▎` 引線；多行 input 重啟；Footer 補完（envelope summary、grant TTL、compaction spinner、三階淡出、parallel group panel） | 中 | D1, D2, D3 |

D1 是最高風險——架構翻新，要小心測 abort-on-submit、slash commands、長 streaming、多輪正常運作。

---

## 13. 風險與對策

### 風險 1 — `Application.run_async` 跟 streaming 互動

streaming turn 跑的時候 Application 是 running 狀態，`app.print_text` 應該能用。但如果 streaming 異常退出，要確保 app 還活著。

**對策**：streaming task 包在 `try / except`，異常時 `harness.inline(error)` + 不退出 app。

### 風險 2 — `console.capture()` 跟 force_terminal

Rich 的 force_terminal=True 模式渲染 ANSI 顏色字串。但 Application 的 colour mode 可能跟 Rich 預期不同——比如 truecolor vs 256-color。

**對策**：實測，必要時把 `_render_console` 設成跟 Application 同 colour mode（透過 `color_system="truecolor"` 等明確指定）。

### 風險 3 — Mode 切換時的 race

streaming turn 中突然 confirm 觸發，layout 切到 confirm，input_buffer 內容怎麼辦？

**對策**：input_buffer 是純 buffer，mode 切走時不會丟內容，切回來內容還在。確認測試覆蓋。

### 風險 4 — Confirm 期間 streaming 還在跑（被切到背景）

streaming turn 會印中間結果到 scrollback。confirm widget 在底部 layout。兩者互不干擾 — `app.print_text` 自動 above-bottom-area。

**對策**：理論上 ok，但要實測。如果視覺上不舒服（streaming 還在輸出時下面卡個 confirm widget），可以考慮 streaming 暫停直到 confirm 結束。

### 風險 5 — 三階淡出沒法 retroactively 改 scrollback

scrollback 已經滾過去的 envelope panel 沒法重塗。

**對策（已收進 §7）**：只對「最後 N 個還在底部可見」的 envelope 做淡出。用 cursor up 重印實作。歷史就讓它停在 committed 樣式。

### 風險 6 — Discord / TUI 端不受影響

PR-D 只動 CLI 端（`loom/platform/cli/`）。Discord 端有自己的 renderer（不受影響）；TUI 端走 Textual（更不受影響）。要確認沒有跨模組 import 遺漏。

**對策**：grep 確認 PR-D 動的檔案沒被 Discord/TUI 引用。

---

## 14. 已知 bug 一併解掉

PR-A 留底（doc/49 已知 bug 段）：

- ✅ Streaming 行首截斷（CJK soft-wrap）→ 改寫 streaming 自然消失
- ✅ Multiline input 暫不啟用 → multiline=True 重啟
- ✅ 三事件 stdin coordination 拿掉
- ✅ `_run_interactive` wrapper 拿掉
- ✅ `patch_stdout(raw=True)` 不再需要

---

## 15. 已決議

從 doc/49 + #236 + 本 PR 設計過程累積：

- [x] Footer 簡單 ANSI（不上 Live）
- [x] 多並行 envelope footer 顯示數量 `2× ▸ run_bash`
- [x] Grant TTL turn 邊界刷新（不每秒 redraw）
- [x] Compaction spinner 必要
- [x] Confirm widget 用過即焚（含 scope panel）
- [x] 綠燈訊息維持沉默；不 blanket flash
- [x] **三階淡出**只對最新 1 個底部可見的 envelope，更早的就讓它停在 committed 樣式（2026-04-29）
- [x] **Streaming 中觸發 confirm 預設暫停 streaming**，實測若視覺上沒問題再放寬（2026-04-29）
- [x] **Mode 切換動畫硬切，不 fade**（2026-04-29）。對齊主流終端 CLI（Claude Code / Codex / Gemini / charm 系列），premium 感由排版/節奏/色彩處理而非動畫
- [x] **Loom 視覺識別差異化**（2026-04-29）：靠羊皮卷 palette + `Loom ▎` + `⚙ harness ›` + `you ›` 琥珀 prompt 為主軸；D4 再加 footer 永久 `▎ Loom` 角標、首次啟動迷你 ASCII signature、footer 左下 model + persona
- [x] **啟動瞬間整合**（2026-04-29）：捨棄現有的 `render_header` + `MemoryIndex.render()` 雙 Panel，改成 3 行迷你 signature 帶數字（skills / facts / mcp / episodes 一行帶過）。MemoryIndex 完整資料繼續餵 LLM system prompt 不變

- ✅ **三階淡出對 scrollback 範圍**：只對最新 1 個還在底部可見的 envelope，更早的就讓它停在 committed 樣式
- ✅ **Streaming 中觸發 confirm**：預設**暫停 streaming**，實測若視覺上沒問題再放寬
- ✅ **Mode 切換動畫**：硬切（無動畫）。對齊主流終端 CLI（Claude Code / Codex / Gemini / charm 系列）的選擇，premium 感由排版/節奏/色彩處理，不是動畫
- ✅ **Loom 視覺識別差異化**：靠羊皮卷 palette + `Loom ▎` + `⚙ harness ›` + `you ›` 琥珀 prompt 為主軸；D4 再加 footer 永久 `▎ Loom` 角標、首次啟動迷你 ASCII signature、footer 左下 model + persona

## 16. 未決問題

- **`force_terminal` colour mode**：實測時遇到 colour 不對再決定（PR-D2）
- **動畫補丁的觸發條件**：D4 完成後若實測視覺太突兀，加 50ms dim 過渡幀（follow-up）

---

## 17. Test plan 草稿

- [ ] 啟動 `loom chat`，input 區黏底，footer 出現
- [ ] 簡單對話：輸入 → 送出 → streaming 從上方出現 → 結束後 footer 收回 spinner
- [ ] Multiline 輸入：Alt+Enter 換行，Enter 送出
- [ ] 觸發 GUARDED 工具 → confirm widget 出現在底部 input 區位置 → 選擇後消失，scrollback 不留 panel
- [ ] HITL pause → 3 選項出現 → 選 redirect → 文字輸入 → 全部消失
- [ ] 長 CJK 段落 streaming：行首不再被截斷
- [ ] 並行工具：footer 顯示 `3× ▸ ...`，全完成後 group panel collapse 為單行
- [ ] `/compact` 觸發：footer 出現 `⚡ 壓縮中…`，完成後 inline 摘要
- [ ] Token budget 從 0 累積到 65%，footer 出現 `⚡ tok 65%`
- [ ] Grant 5min 後過期，下個 turn 開始時 footer TTL 數字正確
- [ ] Abort：streaming 中送新訊息 → 上一輪截斷標記 → 新訊息接續
- [ ] 多輪連續 abort 不卡死
- [ ] Discord / TUI 端不受影響（smoke test）

---

## 18. D4 視覺對齊草圖（mockups）

D4 是視覺打磨的最後一刀。下面是預期的各狀態 ASCII 草圖，作為 user 與實作對齊的參照。**真實渲染會帶羊皮卷色（奶油/琥珀/赭石），這裡為純文字示意**。

### 18.1 Idle（剛進 chat 或 turn 結束）

```
─────────────────── above is terminal scrollback ──────────────────

    Loom ▎ context 0.0% · persona: tarot
    Loom ▎ 你好，今天想做什麼？

    ╭───────────────────────────────────────────────────────────╮
    │ you › ▏                                                    │  ← input area
    ╰───────────────────────────────────────────────────────────╯
     tarot · MiniMax-M2.7                                ▎ Loom    ← footer
```

footer 兩端對稱：左下 `model · persona`（dim），右下 `▎ Loom` brand marker（dim 琥珀）。中間 idle 時留白。

### 18.2 Streaming 中（單一工具進行）

```
─────────────────── scrollback flows here ──────────────────

    Loom ▎ context 0.3% · persona: tarot
    Loom ▎ 我來幫你查一下最新動態，等等。

    ▸ web_search · running

    ╭───────────────────────────────────────────────────────────╮
    │ you › 你也可以打字構思下一個訊息 ▏                         │  ← 可同時打字
    ╰───────────────────────────────────────────────────────────╯
     tarot · MiniMax-M2.7   ▸ web_search · 1.2s          ▎ Loom    ← footer
```

footer 中間多了 `▸ web_search · 1.2s` 即時顯示 active envelope。

### 18.3 多工並行

```
    Loom ▎ context 1.2%
    Loom ▎ 我同時跑幾個查詢，方便比對。

    ╭─ parallel · 3 tools ────────────────────────────────────╮
    │ ▸ web_search · 0.5s         (running)                   │
    │ ▸ minimax:text_to_image · 1.2s   (running)              │
    │ ▸ run_bash · 2.1s            (running)                  │
    ╰──────────────────────────────────────────────────────────╯

    ╭───────────────────────────────────────────────────────────╮
    │ you › ▏                                                    │
    ╰───────────────────────────────────────────────────────────╯
     tarot · MiniMax-M2.7   3× ▸ web_search · 0.5s       ▎ Loom    ← 數量寫出
```

完成後 group panel collapse 為一行：

```
    ✓ 3 tools · 4.2s   [parallel]
```

### 18.4 三階淡出（最新 1 個）

剛完成 envelope 樣式：

```
    ▸ write_file · 1.2s ✓        ← committed (鼠尾草綠)
```

3 秒後同一行就地重塗：

```
    ▸ write_file · 1.2s ✓        ← frozen (米褐 dim)
```

實作：保留 cursor 位置，cursor up + 重印同一行。只對「**最新一個還在底部可見**」的 envelope 生效。早於它的歷史就讓它定格在 committed 樣式（cursor 回不去）。

### 18.5 Confirm mode（用過即焚）

```
─────────────────── scrollback flows here ──────────────────

    Loom ▎ context 1.2%
    Loom ▎ 我來幫你寫一份檔案。

    ▸ write_file (awaiting confirmation)

    ╭─ ⚠ Loom 想執行 [write_file] · GUARDED ─────────────────╮
    │   path:write → tmp                                       │
    │   New resource type not previously authorized            │
    │                                                          │
    │  ▸ 允許這次  (y)                                         │
    │    允許並記住 30 分鐘 (lease)  (s)                       │
    │    允許並永久授權此 scope  (a)                           │
    │    拒絕  (n)                                             │
    │                                                          │
    │  ↑↓ 選擇  ⏎ 確認  esc 取消                               │
    ╰──────────────────────────────────────────────────────────╯
     tarot · MiniMax-M2.7   ▸ write_file (awaiting)      ▎ Loom    ← footer
```

選擇後，**整個 confirm 區塊原地消失**，input area 回來——scrollback 不留 panel。決策結果由後續的 ToolBegin/ToolEnd（或 ABORTED rule）負責記。

### 18.6 HITL Pause mode

```
    Loom ▎ context 2.3%
    Loom ▎ 已經跑完查資料的部分。

    ▸ web_search · 1.2s ✓
    ▸ minimax:text_to_image · 8.4s ✓

    ╭─ ⏸ Loom 已暫停，下一步？ ─────────────────────────────╮
    │  ▸ 繼續執行剩下的工具  (r)                             │
    │    導向新指令並繼續  (m)                               │
    │    取消這個 turn  (c)                                  │
    │                                                        │
    │  ↑↓ 選擇  ⏎ 確認  esc 取消                             │
    ╰────────────────────────────────────────────────────────╯
     tarot · MiniMax-M2.7   ⏸ HITL paused                ▎ Loom
```

選 `m` 後切到 redirect 模式：

```
    ╭───────────────────────────────────────────────────────────╮
    │ redirect › 跳過圖片，只給文字摘要 ▏                        │
    ╰───────────────────────────────────────────────────────────╯
```

文字輸入結束後一樣**整塊消失**，恢復 input mode。

### 18.7 Compaction in progress

`/compact` 觸發或自動觸發時：

```
    Loom ▎ context 87.3%
    Loom ▎ 等等，先讓我整理一下記憶，避免後面對話跑掉軌道。

    ⚙ harness › compacting context (87.3% used)…

    ╭───────────────────────────────────────────────────────────╮
    │ you › ▏                                                    │
    ╰───────────────────────────────────────────────────────────╯
     tarot · MiniMax-M2.7   ⚡ 壓縮中… 23%                ▎ Loom    ← footer 表示忙
```

完成後：

```
    ⚙ harness › 已壓縮 47 → 12 turns（保留 user 5 / system 3 · -4.8k tokens）
    Loom ▎ 好，可以接著聊。剛剛你問的是…
     tarot · MiniMax-M2.7                                ▎ Loom    ← spinner 退場
```

### 18.8 Token budget 浮出（>60%）

footer 中間自動出現琥珀色 `⚡ tok 67%`，破 80% 變赭石、破 95% 變赤陶：

```
     tarot · MiniMax-M2.7   ⚡ tok 67%   ▸ run_bash · 0.3s   ▎ Loom
     tarot · MiniMax-M2.7   ⚡ tok 84%   ▸ run_bash · 0.3s   ▎ Loom    ← 警告色
     tarot · MiniMax-M2.7   ⚡ tok 96%                       ▎ Loom    ← 危險色
```

### 18.9 Grant TTL（最快過期那個）

`/scope` 有 active grants 時，footer 左半段加上：

```
     tarot · MiniMax-M2.7   🔑 L2·0:43   ⚡ tok 67%         ▎ Loom
```

倒數只在 turn 邊界刷新（不每秒 redraw）。

### 18.10 啟動瞬間（迷你 signature + 整合資訊）

**現況（要改）**：`render_header(model, db)` 印一個 Panel，緊接著 `_memory_index.render()` 又印一個多行 Memory Index Panel（skills 數、semantic facts、relations、anti-patterns…）。一次啟動連印 2 個 Panel + 多行清單，視覺壅塞。

**PR-D 整合方向**：把 header + memory index counts 收成一個 3 行迷你 signature。MemoryIndex 完整資料**繼續餵 LLM 的 system prompt**（沒改），只是不再給使用者看完整清單——使用者要查時 `/memory list`、`/skills` 等命令還在。

```
$ loom chat

         ╱ Loom v0.3.4 · 'parchment'
    ───── 12 skills · 14k facts · 3 mcp · 47 episodes
         ╲ MiniMax-M2.7 · persona: tarot

    ╭───────────────────────────────────────────────────────────╮
    │ you › ▏                                                    │
    ╰───────────────────────────────────────────────────────────╯
     tarot · MiniMax-M2.7                                ▎ Loom
```

中間那行「stats line」根據 `MemoryIndex` 取數：
- `12 skills` — `index.skill_count`
- `14k facts` — `index.semantic_count`（人類友善縮寫，>1k 用 k，>1m 用 M）
- `3 mcp` — 從 `session._mcp_servers` 拿（待 D4 實作時 wire）
- `47 episodes` — `index.episode_sessions`

額外 fields 視情況選顯：`relational_count > 0` 時顯示 `· N relations`；`anti_pattern_count > 0` 時加 `· N anti-patterns`。沒有的 field 不佔位。

第一次 streaming 開始後 signature 自然被推上 scrollback，永遠不再出現——是「會話開始」的記號，不是常駐 chrome。

---

## 19. 出處與相依

- 設計母文件：`doc/49-CLI-Refresh-設計.md`
- 已合併：PR-A (#237) PR-B (#242) PR-C (#244)
- Tracking issue：#236
- 後續：PR-E (TaskList 浮動面板)
- 取代目標：`doc/34-TUI-使用指南.md`（PR-D 完成後廢除）
- 相關 follow-up issues：#238 #239 #240 #241

