# PR-C 實作草稿 — Harness vs Loom Agent 視覺切分

本文件是 `doc/49-CLI-Refresh-設計.md` 中 **PR-C** 的詳細實作藍圖。完成後內容會折回 issue #236 PR-C 的 PR body 與相關 follow-up。

---

## 1. 目標回顧

把「Loom Agent 在說話」、「Harness 在報告」、「工具在執行」三類訊息**視覺強切分**，並用三條訊息頻道分流：

- **底層 (footer)**：閃 0.3s 不留底
- **流式 (inline)**：append-only 留歷史
- **跳出 (modal)**：阻斷式互動

PR-A/B 已建好基礎（prompt_toolkit producer/consumer + LOOM_THEME），PR-C 把分流邏輯與訊息族群 renderer 補齊。

---

## 2. 13 類訊息來源實際 emission point

從 PR-A 設計階段確認的 13 類，補上具體 file:line。

| # | 類別 | Emission point | 目前頻道 | PR-C 目標頻道 |
|---|---|---|---|---|
| 1 | EnvelopeStarted/Updated/Completed | `session.stream_turn()` yields events → `main.py:_run_streaming_turn` | inline (panel) | **inline** + **footer** 摘要 |
| 2a | Auth 綠燈（`pre-authorized` / `exec_auto` / `scope-allow`） | `middleware.py:573 _notify_lifecycle` 配合 `permissions.py:label/plain` 進 trace；目前**不直接 print**，只寫進 lifecycle ctx + middleware_trace | 流式（透過 trace）| **footer 閃 0.3s 不留底** |
| 2b | Auth 紅燈（`user denied` / `circuit-breaker tripped` / `scope-deny` / `unattended-deny`） | 同上但 `result=False` | 流式 | **流式留底**（forensics） |
| 3 | Confirm prompt | `session.py:_confirm_tool_cli` → `select_prompt` widget | modal | **modal**（已是） |
| 4 | Scope grant 狀態（active grants、TTL、過期清掃） | `main.py:/scope` slash command 列表；過期清掃在 `permissions.py` 內部，目前無顯示 | 流式（手動查）/ 沉默（清掃）| 底層（最快過期那個 TTL）+ 流式（手動查） |
| 5 | Compaction | `main.py:394 /compact` 手動觸發；`session.py:_smart_compact` 自動觸發；目前 `console.print("[dim]Compacting context (X% used)…[/dim]")` | 流式 | 底層（執行中 spinner）+ 流式（完成摘要） |
| 6 | History sanitize | `session.py:2539 _sanitize_history` — **完全沉默**，無 log 無 print | 沉默 | **流式 1 行**（但只在真的有修才喊） |
| 7 | Session resume / diagnostic | `main.py:124 console.print("[dim]Resuming session ...")`；`main.py:143 _cli_diagnostic` callback | 流式 | 流式（保留現狀）|
| 8 | Model / Personality 切換 | `main.py:_handle_slash` 各 `console.print` | 流式 | 流式（保留現狀）|
| 9 | Token budget | `main.py:status_bar()` 在 turn 結束印一次 | inline (turn 結束)| **底層常駐（>60% 才浮出）** |
| 10 | NotificationRouter | `notify/router.py` fan-out → `cli.py CLINotifier.send` 印 Panel | 流式（Panel）| 流式（族群明確化：autonomy / external trigger 視覺區分）|
| 11 | MemoryGovernor | `governance.py:139 governed_upsert` 回傳 `GovernedWriteResult`；**目前不打印**，只回傳結果給 caller | 沉默 | **流式（reject only，accept 沉默）** |
| 12 | Reasoning chain | `main.py:387 /think` 手動查 | 使用者主動 | 不變 |
| 13 | Error / fatal | `main.py:1322 except CancelledError`、`except Exception` 在 `_run_streaming_turn`；provider 錯誤從 cognition 層拋上來 | 流式 / modal（fatal recovery）| 流式（一般）/ modal（fatal） |

---

## 3. 新抽象：`HarnessChannel`

把分流邏輯集中在一個薄抽象，避免散落 `console.print` 的 ad-hoc 處理。

```python
# loom/platform/cli/harness_channel.py（新檔）

class HarnessChannel:
    """Harness 訊息分流器：footer / inline / modal 三條路。"""

    def __init__(self, console: Console, footer: "FooterController"):
        self._console = console
        self._footer = footer

    # 流式（留底）
    def inline(self, message: str, *, level: str = "info") -> None:
        """印一條帶 ⚙ harness › 署名的 inline 訊息。
        level: 'info' | 'success' | 'warning' | 'error'
        """
        ...

    # 底層（閃光，不留底）
    def flash(self, message: str, duration: float = 0.3) -> None:
        """在 footer 區閃一次，duration 後消失。"""
        self._footer.flash(message, duration)

    # 底層（持續顯示）
    def status(self, key: str, value: str | None) -> None:
        """設定/清除 footer 上的某個 status field。
        value=None 清除該 field。
        """
        self._footer.set_status(key, value)
```

**為什麼是 HarnessChannel 不是 LoomAgentChannel**：
Loom Agent 的訊息走 `_run_streaming_turn` 既有路徑（TextChunk / Markdown），不需要新抽象。Harness 訊息才有「該不該留底」的決策歧義，需要明確 API。

---

## 4. 新抽象：`FooterController`（PR-D 完整版的前哨）

PR-D 才會做完整的 1 行 live footer。但 PR-C 的「綠燈閃光」、「token budget 浮出」、「grant TTL 倒數」需要一個最小可用的 footer，才能把分流落地。

```python
# loom/platform/cli/footer.py（新檔）

class FooterController:
    """1 行 status footer 的最小實作。

    PR-C 階段：用 Rich Live 在 main_loop 底層維護一行可變狀態。
    PR-D 階段：合併進 persistent Application 的固定 bottom Window。
    """

    def __init__(self, console: Console):
        self._console = console
        self._status: dict[str, str] = {}
        self._flash_message: str | None = None
        self._live: Live | None = None

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def set_status(self, key: str, value: str | None) -> None: ...
    def flash(self, msg: str, duration: float) -> None: ...
    def _render(self) -> Text: ...
```

**status 欄位順序**（從左到右）：

```
🔑 L2·0:43   ⚡ tok 67%   ▸ run_bash·1.2s
^ grant       ^ budget     ^ active envelope
```

**flash 顯示策略**：覆蓋整行，到期後恢復原 status。flash 不堆疊，新 flash 取代舊 flash。

---

## 5. 視覺族群 renderer

### 5.1 Loom Agent 文本

走既有 `_run_streaming_turn` 的 `TextChunk` 路徑，**不改架構**，只調 visual：

- 每個 turn 開頭：印 `Loom ▎` 引線（絲綢色 = `loom.accent`）+ Rule 但 Rule 樣式弱化
- streaming 過程：`[loom.text]` 奶油色
- think summary：`[loom.muted]💭 ...[/loom.muted]`（已是）

### 5.2 Harness renderer

寫一個 helper 把 `⚙ harness › <message>` 包成統一格式：

```python
def render_harness_inline(message: str, level: str = "info") -> Text:
    """Render harness inline message: ⚙ harness › <msg>

    Level mapping:
      info     → loom.muted   (sanitize, compaction 完成)
      success  → loom.success (auth 綠燈紅燈不會走 inline，但保留)
      warning  → loom.warning (deny, governor reject)
      error    → loom.error   (forensics, fatal)
    """
    return Text.from_markup(
        f"[loom.accent]⚙ harness ›[/loom.accent] [loom.{level_to_token(level)}]{message}[/loom.{level_to_token(level)}]"
    )
```

**對齊 doc/49 設計原則**：harness 訊息一律帶署名，跟 Loom Agent 文本視覺切開。

---

## 6. 攔截點與改動清單

按照 file:line 列出需要動的點：

### `loom/core/harness/middleware.py`

- **L573 `_notify_lifecycle`**：判斷 `reason` 屬於綠燈還是紅燈
  - 綠燈集合：`{"pre-authorized", "exec_auto", "scope-confirm-legacy-authorized"}` 與 `reason.startswith("scope-allow:")`
  - 紅燈：其餘 `result=False` 或 `reason.startswith("user denied")` / `circuit breaker` / `scope-deny`
- 增加可選 callback hook：`self._on_lifecycle_event` 由 platform 注入
  - 綠燈 → `harness_channel.flash(...)` 0.3s
  - 紅燈 → `harness_channel.inline(..., level='warning')`
- **不能** 在 middleware 內直接呼叫 channel（會逆向 import），用 callback 注入

### `loom/core/session.py`

- **L2539 `_sanitize_history`**：在 Pass 1/2 真的修了東西時設定 `self._last_sanitize_repaired: tuple[int, int]`，由 platform 層讀取後印 inline
- **L203 `governed_upsert`**：caller 那邊判斷 `result.written == False` 就 emit harness inline

### `loom/platform/cli/main.py`

- **L79 `console = Console(...)`**：新增 `harness_channel` instance + `footer` instance
- **L1244-1249 Opening Rule (`bold green loom`)**：改成 `Loom ▎` 引線風格（visual）
- **L1322-1323 EnvelopeStart/End rendering**：保留 inline panel，但同時 `footer.set_status('active_envelope', ...)`；envelope 完成後 `set_status('active_envelope', None)`
- **L1381-1404 status_bar**：拆解，token budget 進 footer（>60% 才顯示），其他資訊保留 inline
- **L584-588 `_handle_slash` 中 /compact / /scope** 等命令：訊息走 `harness_channel.inline`
- **L1322-1326 `except` 路徑**：error inline 加上 `⚙ harness ›` 署名（fatal 走 modal）

### `loom/notify/adapters/cli.py`

- 把 `CLINotifier.send` 印的 Panel 加上族群署名（`[autonomy]` / `[external]`）

### `loom/core/memory/__init__` 或 caller

- 確認 `governed_upsert` 的 caller（`session.py:203`）會把 reject 結果通報出去

---

## 7. 三頻道實作順序（commit 拆分）

PR-C 收斂為 C1-C4。C5 移到 PR-D。

| Commit | 主題 | 依賴 |
|---|---|---|
| **C1** | `theme.py` 補 `loom.harness.bg` token；新增 `harness_channel.py`（inline / flash 兩個方法，flash 在 PR-C 是 no-op） | 無 |
| **C2** | Loom Agent 引線重畫；Harness inline renderer（`⚙ harness ›` 署名）覆蓋現有 `console.print`；明確分清 Loom Agent 文本 vs Harness 訊息 | C1 |
| **C3** | `_notify_lifecycle` 綠燈/紅燈分類；綠燈 flash (no-op) 不印，紅燈走 inline；middleware 注入 callback hook | C1 |
| **C4** | sanitize 修復可見化（inline once-per-turn）；governor reject 可見化（accept 沉默） | C1 |

---

## 8. 風險與對策

### 風險 1 — Footer Live 跟 patch_stdout 互動

`prompt_toolkit.patch_stdout` 已經吃掉 stdout，再疊一層 Rich Live 可能打架。**對策**：先用最 simple 的 footer 方案（每次 status 變化重印一行 + ANSI cursor up），不上 `Live`。完整 Live footer 留到 PR-D。

### 風險 2 — middleware → platform callback 注入打破 layering

對策：callback 是 `Optional[Callable]`，預設 None（無動作）；platform 層在 `LoomSession.start()` 後注入。沿用 `session._cancel_spinner_fn` 的模式。

### 風險 3 — `_sanitize_history` 修復頻率高 → 流式被洗版

對策：每個 turn 最多印一次（dedup by turn_index）。狀態存 `session._last_sanitize_repaired_turn`，下一個 turn 才能再印。

### 風險 4 — Harness 訊息族群分類偏差

對策：先做 C2/C3/C4 只關注「該分到哪個族群」，視覺細節（icon、空白、emoji）C5 之前都先用最簡格式，等實際看效果再調。

---

## 9. 已拍板（doc/49 → 此 PR）

- [x] 綠燈不出聲（footer flash），紅燈才講話（inline）
- [x] sanitize 必須可見（但只在真的修了才喊）
- [x] 流式訊息一律帶 `⚙ harness ›` 署名
- [x] MemoryGovernor 只顯示 reject，accept 沉默
- [x] Token budget 採 C 方案（<60% 隱藏，>60% 浮出）
- [x] Loom Agent / Harness / Tool 三族群視覺強切分

---

## 10. 已拍板的細節決議（2026-04-29）

1. **Footer 實作策略**：簡單 ANSI cursor 移動 → 完整 Live 留到 PR-D
2. **多 envelope 並行 footer 顯示**：要把數量寫出來，例如 `2× ▸ run_bash · 1.2s`，不只顯示最新
3. **Grant TTL 倒數頻率**：turn 邊界刷新（不每秒 redraw）
4. **Compaction spinner**：要，壓縮中 footer 顯示 `⚡ 壓縮中…`，完成後 inline 摘要——避免使用者看到停頓困惑

## 11. PR-C scope 收斂：C1-C4，C5 移到 PR-D

確認 C5 (footer 整合 token budget / active envelope / grant TTL / compaction spinner) 全部移到 PR-D 跟 Linear Stream 一氣呵成做。**PR-C 不引入 footer**，只做訊息族群切分 + 三頻道分流的 inline / modal 部分。

這意味著：
- `harness_channel.flash()` 在 PR-C 階段降級為「印一行 dim 訊息然後立即下一行」的 best-effort 實作（沒有真的「閃 0.3s 消失」），完整版等 PR-D footer
- 綠燈訊息在 PR-C 階段選擇：(a) 完全不印 (b) 印一行很 dim 的訊息。**選 (a)**——綠燈不出聲，flash 當作 no-op；PR-D 上 footer 後再加閃光
- 這個收斂讓 PR-C 純粹處理「訊息族群、署名、留底決策」，PR-D 處理「視覺常駐、footer 邏輯」

---

## 11. 出關後 Test plan 草稿

- [ ] Confirm widget 觸發時，Loom Agent 文本與 Harness 訊息視覺差異明顯
- [ ] `pre-authorized` / `exec_auto` 不再印進歷史（grep `~/.loom/debug` log 不再出現）
- [ ] `user denied` / `circuit breaker` 留底 inline 可見
- [ ] sanitize 修復發生時 inline 出現一次（用人為構造 orphan tool_call 觸發）
- [ ] `governed_upsert` reject 訊息 inline 可見；accept 沉默
- [ ] Token budget 在 <60% 時 footer 看不到；65%、85%、95% 顏色階梯正確
- [ ] 多輪連續 abort 時 footer 不殘留 spinner

---

## 12. 出處與相依

- 設計母文件：`doc/49-CLI-Refresh-設計.md`
- Tracking issue：#236
- 已 merge：PR-A (#237) PR-B (#242)
- 後續：PR-D (Linear stream + Live Footer)、PR-E (TaskList)

