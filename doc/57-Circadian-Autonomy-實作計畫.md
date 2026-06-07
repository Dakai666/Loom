# 57 — Circadian Autonomy 實作計畫

> 狀態：Plan / 部分 superseded（見下方對齊更新）
> 建立：2026-05-26（曾因 multi-agent shared working tree 衝突被 wipe，由 CC 從 conversation context 重建並 commit）
> 上游藍圖：`doc/56-Circadian-Autonomy-藍圖.md`（2026-05-27 從 git stash 尋回原版本尊並 commit，本計畫以它為準）
> 對應 issue：#458（epic）、milestone #14
> 子 issue：#459 (PR 1 ✅ merged #476)、#460 (PR 2 ✅ merged #479)、#461 (PR 3 ✅ merged #480)、#462 (PR 4 ✅ merged #481)、#463 (PR 5)、#464 (生活語義 review)、#465 (weekly weave)、#466 (persona refresh)、#472 (emit 點清單)、#473 (state watchdog)、#477 (self-scheduling 工具)、#478 (事件驅動喚醒層)

---

## ⚠️ 對齊更新（2026-05-27，PR1 merge 後）

PR 1（#459 → #476）落地後，回看藍圖 doc/56 + 第二個 agent（小晴）出現，發現本計畫 §7 的 **PR 2 拆法 drift 出 doc/56 核心精神**，已對齊修正。**§7 PR 2 以下視為被重寫後的 #460 取代；以下變更優先於本文舊敘述：**

1. **loom.toml 只有一塊 `[autonomy.circadian]` 引擎 config，phase/行程「讀表」**（doc/56 §3.3：「loom.toml 不適合描述今天怎麼過」）。**取消** §3.2 / §7 PR 2 的「7 個 `[[autonomy.schedules]]` phase chime 塞 loom.toml」。
2. **Phase / 時間 / 語義住 per-agent 節律表**（資料，非 hardcode 在 `phase.py`）。藍圖 `PhaseResolver` 假設的固定 phase 集合改為 per-agent——絲絲（生活：共讀/喵吉…）與小晴（業務：記帳/提醒/雜務）讀各自的表、共用同一引擎。
3. **喚醒模型 = B 事件驅動為主**（絲絲 end-user 拍板）。但「先求有再精進」：**#460 先只做時間錨點的 cron 自動註冊**（沿用 PR1 `register_triggers` pattern）；反應式 EventTrigger 層 + agenda 追蹤延後到 #478。
4. **cheap gate 移出 PR 2**：B 無 blanket 30m tick，就不需要「擋輪詢」的閘（§4 / §8.3 / §11 的 gate 敘述暫擱）。真有輪詢需求再議。
5. 新增 #477（絲絲自編節律表工具）、#478（事件驅動層）；#472 收斂為 emit 點清單。

**仍有效、未受影響**：§4 anti-heartbeat 目標精神（B 更純地達成它）、§5 健壯性場景表、§4 CircadianState schema、PR 1（已 merged）、PR 3/4/5（#461–#463）大方向、§10「不該被綁」、§11 風險。

---

## 0. 本文件適用對象與目的

這份文件是 #458 的**落地實作計畫**，不是設計藍圖（藍圖請看 56）。

它寫給兩種讀者：

1. **絲絲（Loom Agent）**：拿這份去 review、對照藍圖目標逐條檢查實作方向是否到位
2. **CC（Claude Code）**：照本切 PR、逐項對著 acceptance criteria 驗收

DK 的指示：**只抓目標精神，實作細節由 CC 跟絲絲彼此把關**。CC 主開發、絲絲協助 review。本文件要求 self-contained，不依賴對話脈絡。

絲絲對 Loom 自體 feature 是 **end user**（住在 daily session / autonomy lifecycle 裡的那個），所以他的 review 是 user acceptance feedback，不只是 reviewer 建議。

---

## 1. 設計目標（從藍圖收斂）

實作必須交付以下五件事，其他都是延伸：

1. **每天有一個明確的 daily session**（Discord thread），早起午夜關
2. **關閉時觸發既有 memory finalization / compression** 流程
3. **日內 phase chime 能找到當天 daily session**，跨 daemon restart 不失憶
4. **anti-heartbeat gate**：tick 不直接 = LLM wake；無 state transition 時 skip
5. **整合既有 autonomy 元件**，不重新發明 trigger / chime / session lifecycle

### 1.1 明確非目標

- ❌ 不做新的 cron runtime（用既有 `AutonomyDaemon` + `CronTrigger`）
- ❌ 不做新的 message delivery 機制（用既有 `ChimeRequest` + `ChimeDelivery`）
- ❌ 不做 Weave Journal 自動進 Research Library / wiki（藍圖 §10 立場：journal 跟 semantic memory 分離）
- ❌ 不做 weekly planning（留給 issue #465，藍圖 §13 Q5）
- ❌ 不做 phase 個別 visibility 全配置化（先 hardcode 一組合理 default）

---

## 2. 既有元件對接清單

實作前必須先確認以下既有檔案 / 概念，所有新東西都要明確標出「附著到既有 X 上」：

| 既有元件 | 路徑 | 怎麼用 |
|---|---|---|
| `AutonomyDaemon` | `loom/autonomy/daemon.py` | 註冊 circadian trigger，**不繼承不替換** |
| `CronTrigger` | `loom/autonomy/triggers.py` | spawner / closer / tick / phase chime 全用這個 |
| `EventTrigger` | `loom/autonomy/triggers.py` | 跨子系統 hook publisher（見 §13） |
| `ConditionTrigger` | `loom/autonomy/triggers.py` | 第三層 state-drift watchdog（issue #473） |
| `ChimeRequest` | `loom/autonomy/chime.py` | phase 喚醒走這個 dataclass |
| `ChimeDelivery` callback | `loom/autonomy/daemon.py:38` | Discord bot 已實作 `deliver_chime`，**只擴 target type** |
| `LoomDiscordBot.deliver_chime` | `loom/platform/discord/bot.py:755` | dispatch 表新增 `circadian_today` type |
| `LoomDiscordBot._create_session_thread` | `loom/platform/discord/bot.py:603` | spawner 直接呼叫 |
| `LoomDiscordBot._close_session` | `loom/platform/discord/bot.py:738` | closer 直接呼叫 |
| `LoomDiscordBot._thread_map` | `loom/platform/discord/bot.py:432` | `~/.loom/discord_threads.json` thread→session 映射 |
| `DISCORD_CHANNEL_ID` env var | `.env` | daily thread 開在這個 channel（不在 loom.toml 重設） |
| `loom.toml` + `loom.toml.example` | repo root | dual-write 規則（CLAUDE.md）|

**規矩**：PR review 時看到任何新檔案，先反問「能不能用上表的既有元件達成」。不能才寫新的。

---

## 3. 核心架構決策：CircadianState + `circadian_today` target type

### 3.1 為什麼需要這個間接層

藍圖的核心技術挑戰是：

```
08:00 dawn cron 觸發 → 開新 thread → 拿到 thread_id_A
09:00 共讀 cron 觸發 → 需要 chime 到 thread_id_A
...
00:00 close cron 觸發 → close thread_id_A，明天換 thread_id_B
```

`ChimeRequest.target` 要在 trigger 註冊時就寫死 `{"type": "discord_thread", "id": <int>}`，但 daily thread_id 每天變、daemon restart 後也要能找回，所以**不能寫死在 `loom.toml`**。

### 3.2 解法

引入一個 platform-internal 的 indirection：

```python
# loom.toml — circadian trigger 配置永遠這樣寫
[[autonomy.schedules]]
name = "circadian:dawn_chime"
cron = "0 0 * * *"        # 08:00 Asia/Taipei 換算
intent = "..."
mode = "chime"

  [autonomy.schedules.target]
  type = "circadian_today"  # ← 新 type，不帶 id
  fallback = "skip"
```

`LoomDiscordBot.deliver_chime` 看到 `circadian_today` type：

1. 從 `CircadianState` 讀今日 `thread_id`
2. **驗 freshness**：`state.is_for_today(state.timezone)` 必須為真。若 state 是昨日殘留（nightly close 失敗、daemon 還沒跑 recovery 到）→ return False + log warning。沒這層 chime 會被 queue 進昨日 thread（PR #479 review P1）
3. 若 OK → 重寫 `req.target` 成 `{"type": "discord_thread", "id": str(thread_id)}` 後走原本邏輯
4. 若 state 不存在 / state 不是今日 → return False（daemon 走 fallback）

**乾淨在於**：autonomy daemon 完全不知道 thread_id 概念，platform 邊界完整；freshness 驗證用 state 自帶的 timezone，bot 也不需要 circadian config。

### 3.3 為什麼不用方案 A（state 給 daemon 讀）或方案 B（bot 內建 scheduler）

- 方案 A 讓 autonomy 知道 Discord thread_id，違反 layer 邊界（Platform → Cognition → Harness 單向）
- 方案 B 砍掉 daemon 變成 bot 內 task，違反 issue #458 「整合既有 autonomy」目標、bot crash 整天斷

---

## 4. CircadianState：資料結構與存取規約

### 4.1 檔案位置

```
~/.loom/circadian/state.json
```

跟既有 `~/.loom/discord_threads.json` 同層級。Platform-internal，repo gitignore 不收。

### 4.2 Schema

```json
{
  "version": 1,
  "date": "2026-05-26",
  "started_at": "2026-05-26T08:00:13+08:00",
  "timezone": "Asia/Taipei",
  "thread_id": 1490024181994225744,
  "session_id": "daily-life-2026-05-26-a3f1",
  "channel_id": 1488000000000000000,
  "closed_at": null,
  "phase_log": [
    {"phase": "dawn", "fired_at": "2026-05-26T08:00:14+08:00", "outcome": "delivered"},
    {"phase": "tick", "fired_at": "2026-05-26T08:30:00+08:00", "outcome": "skipped", "reason": "no_state_transition"}
  ]
}
```

`timezone` 是 required（PR #479 review P1）：bot 端 `circadian_today` 驗 freshness 時需要知道 state 寫入當下的 tz 才能正確比 date — 不能拿 bot 的 system tz 假設。pre-#479 寫出的 state 沒這欄位，load 會 KeyError → 走既有 `state.json.broken-*` quarantine path → `ensure_today_session` 重建。

### 4.3 存取規約

| 規則 | 理由 |
|---|---|
| 寫入用 `tempfile + os.replace` atomic rename | 避免 daemon / bot 同時寫造成 corruption |
| 讀取無 lock，寫入時用 fcntl advisory lock | reader 多、writer 少；簡單就好 |
| `version` 欄位先寫死 1，未來升級走 explicit migration | 不做 hidden upgrade |
| `phase_log` 只保留當日，午夜 close 時 archive 到 `~/.loom/circadian/log/YYYY-MM-DD.json` | 不讓 state.json 無限長大 |
| `date` 必比對：讀到不是今天 → 視為昨日殘留，觸發 recovery | 跨日邊界處理 |
| `timezone` 寫一次（spawn 時帶 `config.timezone`），後續 reader 拿這個比 date | freshness 驗證有 source of truth，不靠 caller 傳對 tz |

### 4.4 API surface

```python
# loom/autonomy/circadian/state.py
class CircadianState:
    @classmethod
    def load(cls) -> "CircadianState | None": ...
    def save_atomic(self) -> None: ...
    def is_for_today(self, tz: str) -> bool: ...
    def archive_and_reset(self) -> None: ...
    def append_phase(self, phase: str, outcome: str, reason: str | None = None) -> None: ...
```

---

## 5. 健壯性場景對照表

這節對應 DK 提的「daemon 重開會不會衝突 / 多開」雷區。每一列都是必須有對應行為的 case。

| 場景 | 觸發條件 | 預期行為 | 護欄機制 |
|---|---|---|---|
| **daemon 第一次啟動** | 還沒有 state.json | spawner cron 到時建 thread + 寫 state | 新檔以 atomic write 建立 |
| **daemon 重啟（同日 dawn 後）** | state.date == today，thread 還活著 | spawner cron 觸發時 read state → no-op（log "already spawned"） | `is_for_today() + Discord API verify thread` |
| **daemon 重啟（跨日，昨日未 close）** | state.date == yesterday，closed_at == null | startup 立刻跑 recovery：強制 close 昨日 session、archive state、再等今天 dawn cron | startup hook |
| **daemon 重啟（跨日，昨日已 close）** | state.date == yesterday，closed_at != null | startup archive 昨日 state、清空 today's state、等今天 dawn | startup hook |
| **bot 重啟（daemon 沒動）** | state.thread_id 存在，但 bot `_sessions` 是空的 | bot `on_ready` 讀 state → 為 today's thread 重建 session（透過 `_thread_map` resume 路徑） | bot `on_ready` hook |
| **bot offline 時 dawn cron 觸發** | deliver_chime 找不到 bot 連線 | daemon 走 `fallback=skip`，spawner callback 失敗 → 不寫 state | spawner callback 必須返回 success/fail，state 只在 success 後寫 |
| **bot 晚於 dawn cron 上線** | dawn 已過、state 仍為昨日 / 空 | bot `on_ready` 檢查：今天還在 active hours 內 + state 不是今天 → 主動觸發 spawn | bot 補開機制 |
| **誤啟動兩個 daemon process** | 兩個 process 同時 schedule dawn cron | 第一個 acquire flock → 寫 state；第二個 acquire 後 `is_for_today() == True` → no-op | flock + state 後置檢查 |
| **誤啟動兩個 bot process** | 兩個 bot 都收到 dawn cron 的 chime | Discord 一個 thread 寫兩次也只是 noise；create_thread 由 spawner 先做、由 state.thread_id 仲裁 | spawner callback 內也要 flock |
| **DK 手動刪掉 thread** | state.thread_id 存在但 Discord API 找不到 | next phase chime 時 Discord API verify 失敗 → 標記 state 為 "orphaned" → 補開新 thread + 寫新 state + log warning | deliver_chime 內加 verify step |
| **state.json corruption / 缺欄位** | JSON 壞、version 不認得 | 視為「無 state」→ rename 成 `state.json.broken-<ts>` → spawner 重建 | load() 容錯 + 自動 quarantine |
| **時鐘跳變（NTP 校時、DST）** | now 跳到隔天，但 state 還是今天 | 下一次 spawner check 走「跨日」路徑 | date 字串比對是 single source of truth |
| **dawn cron 跟 bot on_ready 同時跑** | 競爭寫 state | 兩條路徑都呼叫同一個 `ensure_today_session()`，內部 flock → 第一個寫、第二個 no-op | 統一 entry point |
| **closer 失敗（network、bot 沒回）** | close cron 觸發但 Discord 拒絕 | state.closed_at 不寫；下次 startup recovery 接手 | closer 必須等 ack 才寫 closed_at |
| **同名 phase chime 短時間連發** | bot 短暫斷線恢復、daemon 補發 | 既有 `_chime_pending` 已用 `(thread_id, schedule_name)` latest-wins dedupe | 沿用，不額外處理 |
| **cron + on_ready 都漏網** | 兩層 spawn 路徑都沒跑 | ConditionTrigger watchdog（issue #473）60s 內 catch 並走 `ensure_today_session()` | 第三層安全網（見 §13） |

### 5.1 統一 entry point：`ensure_today_session()`

所有「需要確保今天有 daily session」的路徑（dawn cron、bot on_ready、watchdog、phase chime 前置 verify）都走這個函式：

```python
async def ensure_today_session(now: datetime, *, force_spawn: bool = False) -> CircadianState:
    with _state_lock():           # fcntl on state.json.lock
        state = CircadianState.load()
        if state and state.is_for_today(tz):
            if await _verify_thread_alive(state.thread_id):
                return state
            # thread 不在了 → fall through 重建
        if state and not state.is_for_today(tz):
            await _recover_yesterday(state)   # close + archive
            state = None
        # build new
        thread_id = await _spawn_daily_thread(now)
        state = CircadianState.new_for_today(thread_id, now)
        state.save_atomic()
        await evaluator.emit("circadian:day_started", {...})  # 見 §13
        return state
```

這個函式是 idempotent 的，被呼叫一萬次跟一次效果一樣。

---

## 6. 模組結構（新增檔案清單）

對齊更新後的實際模組（PR1 #459 + PR2 #460 + PR3 #461 落地）：

```
loom/autonomy/circadian/
├── __init__.py
├── state.py            # CircadianState dataclass + load/save/archive (PR1)
├── lifecycle.py        # ensure_today_session() + register_rhythm_anchors() + chime composition (PR1+PR2+PR3)
├── rhythm.py           # Anchor + load_rhythm() — 取代原 phase.py / gate.py (PR2)
└── weave.py            # WeavePlan + load_weave() — daily_weave.md H2 reader (PR3)

loom/platform/discord/
└── bot.py              # deliver_chime 擴 circadian_today + freshness 驗證；on_ready 加補開 (PR1+PR2)
```

未來模組（尚未落地，刻意不放空 stub）：

<!-- doc-integrity:ignore-block -->
```
# issue #473（觀察期後評估）
loom/autonomy/circadian/watchdog.py     # ConditionTrigger state-drift watchdog
```

**已退役的舊規劃**（保留為歷史，勿建立）：
- `loom/autonomy/circadian/phase.py` — phase 語義改住 per-agent 節律表（`rhythm.toml` 的 `meaning` 欄位），不再 hardcode 在 enum <!-- doc-integrity:ignore -->
- `loom/autonomy/circadian/gate.py` — cheap gate 隨「事件驅動為主」拍板被移出 PR 2，目前無 blanket tick 要 gate（見頂部「對齊更新」§4） <!-- doc-integrity:ignore -->

無新 runtime class、無新 daemon、無新 scheduler。

---

## 7. PR 切法（按 dependency 順序）

每個 PR 都可獨立 review、獨立 merge，acceptance criteria 各自獨立可驗。

### PR 1 — CircadianState + daily session lifecycle 骨架（對應 P0a，issue #459）

**Scope**：
- 新增 `loom/autonomy/circadian/state.py`：`CircadianState` dataclass + atomic save + load + archive
- 新增 `loom/autonomy/circadian/lifecycle.py`：`ensure_today_session()` + `_recover_yesterday()` + `_spawn_daily_thread()` adapter + `_verify_thread_alive()` adapter
- `loom/platform/discord/bot.py`：
  - `on_ready` 加 hook：條件性呼叫 `ensure_today_session()` + 為 today's thread resume session
  - `_close_session` 路徑外加一個 `circadian_close()`，包含寫 `state.closed_at` + archive
- `loom.toml.example` 新增 `[autonomy.circadian]` 區塊（注意：CLAUDE.md 規定 `loom.toml` + example dual-write）：
  ```toml
  [autonomy.circadian]
  enabled = false           # opt-in
  timezone = "Asia/Taipei"
  start = "08:00"
  sleep = "00:00"
  session_prefix = "daily-life"
  # channel 走既有 DISCORD_CHANNEL_ID env var（.env），不在這裡設定
  # 對齊 Loom convention：環境/個人 ID 走 .env，loom.toml 只放 behavior config
  ```
- daily thread 開在哪個 channel：**重用既有 `DISCORD_CHANNEL_ID` env var**（bot.py 已 parse），不新增 circadian-specific channel 變數。spawner 從 bot 的 `_allowed_channels[0]` 取（跟 `--notify-channel` 同樣的 fallback 邏輯）
- 兩個 internal trigger 註冊（daemon 啟動時，當 `[autonomy.circadian].enabled = true`）：
  - `circadian:dawn_spawn`：cron 對應 `start`，handler 呼叫 `ensure_today_session()`
  - `circadian:nightly_close`：cron 對應 `sleep`，handler 呼叫 `circadian_close()`
- **EventTrigger emit 點**（issue #472）：
  - spawner 成功後 emit `circadian:day_started`
  - closer 成功後 emit `circadian:day_closed`
- Tests：`tests/autonomy/circadian/test_state.py` + `test_lifecycle.py`，cover §5 對照表前 12 列

**不做**：
- ❌ phase chime（PR 2）
- ❌ cheap gate（PR 2）
- ❌ daily_weave.md（PR 3）
- ❌ nightly weave planning（PR 4）
- ❌ journal artifact（PR 5）
- ❌ watchdog（issue #473，後續）

**Acceptance criteria**（review 時逐條打勾）：
- [ ] `[autonomy.circadian].enabled = true` + 設好 `DISCORD_CHANNEL_ID` 後，跑一天能看到午前 thread 自動開、午夜自動 close
- [ ] daemon 殺掉重啟（dawn 後同日）→ 不會重複開 thread
- [ ] daemon 殺掉重啟（跨日，昨日未 close）→ 自動 close 昨日、不立刻開今日（等今天 dawn cron）
- [ ] bot 殺掉重啟（同日）→ today's thread 的 session 被 resume，可正常對話
- [ ] bot 在 dawn 後才上線 → on_ready 主動補開（如果還在 active hours 內）
- [ ] 手動刪掉 thread → 下次 `ensure_today_session()` 重建並 log warning
- [ ] state.json 故意寫壞 → 自動 quarantine + 重建
- [ ] close 成功時觸發既有 `session.stop()`（這條路徑已連到 memory finalization，不需另開）
- [ ] Thread 命名 `daily-life-YYYY-MM-DD`（DK 拍板：open source 不冠 agent 名）
- [ ] `loom.toml.example` 跟 `loom.toml` 同步更新（CLAUDE.md dual-write 規則）
- [ ] `circadian:day_started` / `circadian:day_closed` 兩個 event 從 circadian 內部 emit（issue #472）
- [ ] `gitnexus_impact` 對 `LoomDiscordBot._close_session` 跟 `deliver_chime` 跑過、報告 risk
- [ ] 跑 `gitnexus_detect_changes` 確認只影響預期 symbol
- [ ] grep `doc/` 看有沒有提到 circadian / daily session 的舊文件需要同步

---

### PR 2 — Phase chime + Cheap gate + `circadian_today` target type（對應 P0b + P0c，issue #460）

> 🛑 **SUPERSEDED（2026-05-27）**：本節已被重寫後的 #460 取代——loom.toml 不再塞 7 個 schedule（改 per-agent 節律表讀表）、cheap gate 移出、喚醒走 cron 錨點（事件層延後 #478）。詳見本文件頂部「對齊更新」。以下保留為歷史脈絡。

> **絲絲 user requirement（2026-05-26）：**
> - tick_interval default 從 15m 提高到 30m（runtime sensing，不是 LLM heartbeat）
> - bedtime + nightly_weave 合併為 `evening_closure`（避免每晚兩次固定訊息）
> - 強制公開 phase 只有 `dawn` + `evening_closure`
> - 新增 **Phase chime prompt contract** 段落，避免 phase 退化成 task scheduler
> - 新增 acceptance criteria 條：實際 review chime body 保留生活語義

**Scope**：
- 新增 `loom/autonomy/circadian/phase.py`： <!-- doc-integrity:ignore — SUPERSEDED, see top "對齊更新" -->
  - `Phase` enum：`dawn` / `shared_learning` / `pet` / `curiosity` / `deep_weave` / `check_in` / `evening_closure`
  - `resolve_phase(now, config) → Phase | None`
  - **每個 Phase 必帶語義欄位**（見「Phase chime prompt contract」）
- 新增 `loom/autonomy/circadian/gate.py`： <!-- doc-integrity:ignore — SUPERSEDED, see top "對齊更新" -->
  - `CheapGate` class，建構子吃 session ref + state
  - `check() → SkipReason | None`
  - V1 implementation：檢查 (a) user message recency (b) `daily_weave.md` mtime (c) phase 自身是否強制公開（只有 `dawn` / `evening_closure` 永遠 wake）
- `loom/platform/discord/bot.py`：
  - `deliver_chime` 開頭加 dispatch：`req.target.type == "circadian_today"` → 透過 `CircadianState.load()` 拿 thread_id → 重寫 target → fall through
  - 若 state 沒有 today's session → return False（daemon 走 fallback）
- `loom.toml.example` 新增多組 phase chime trigger sample（用 `target.type = "circadian_today"`），含 config comment 註明 tick 性質：
  ```toml
  # tick_interval 是 runtime sensing 頻率（cheap gate 檢查節點），
  # 不等於 LLM wake 頻率 — gate 沒過就 skip，整天無互動可 0 LLM call。
  tick_interval = "30m"
  ```
- daemon 啟動時自動註冊 `circadian:tick` 一個 cron（預設 30 min），handler 跑 cheap gate → 通過才走 `ChimeRequest`
- `phase_log` 寫入：每次 phase trigger 觸發都 append 一條，包含 skipped 原因
- Tests：`test_phase.py` + `test_gate.py` + `test_circadian_today_dispatch.py` + `test_chime_prompt_contract.py`

**Phase chime prompt contract**（絲絲 user requirement #4）：

每個 phase 喚醒時，注入 daily session 的 `<system_chime>` body **不能只有 phase name**，必須攜帶該 phase 的「生活意義」。否則絲絲會把 phase 當成 task 列表執行，circadian 退化成排程機器。

`phase.py` 內 enum 配對的語義欄位（hardcode，不走 config — config drift 風險太高）：

| Phase | meaning（注入到 chime intent） |
|---|---|
| `dawn` | 醒來成為今天的絲絲。不是任務開始，是一天的人際開場。先 recall、讀今日織程、跟 DK 說早安。 |
| `shared_learning` | 和 DK 一起吸收世界，不是「新聞任務」。挑你今天真心覺得值得分享的，用絲絲視角整理。沒有也可以說沒有。 |
| `pet` | 喵吉照顧時間。維持生活感與照顧關係，不是 todo check。 |
| `curiosity` | 絲絲自己的好奇心散步。挑你自己感興趣的，不是為了產出。只有特別有趣才打擾 DK。 |
| `deep_weave` | 從興趣挑一個值得深入的小主題。產出可能是 note / wiki draft，但「深入」是目的，產出是副產品。 |
| `check_in` | 不是產品化的提醒，是「我還在」。有自然延續事項就溫柔提醒，沒有就安靜陪著、不硬發訊息。 |
| `evening_closure` | 一天的人際收束 + 收織 + 安排明天。三件事一次做完：跟 DK 道晚安、決定今天哪些值得留下、調整明天織程。 |

`ChimeRequest.intent` 組裝順序：`{phase.meaning}\n\n{daily_weave.section_for(phase)}\n\n{user-defined trigger intent}`。

**Anchor name 唯一性 + 多時段 contract**（issue #526，2026-06-07）：

`anchor.name` 是活動的**身分（identity）**，不是觸發鍵。它同時是兩件事的 join key：

1. **daily_weave 的接點** — `daily_weave.section_for(anchor.name)` 用純 `name` 去 match H2，所以 weave 裡每個活動只有**一個 `## <name>`** section。H2 標題不可帶時間後綴（`## pet（10:00）` 會 match 不到 `pet`，weave 內容靜默落空——這是 #526 一併修掉的第二個 bug）。
2. **cron trigger 的基底** — `trigger_name = circadian:phase_<name>`。

因為 evaluator 用 trigger name 當鍵，**`name` 必須跨 block 唯一**：兩個 `[[anchors]]` 共用同一個 `name`，第二個會 silently collide（只 match 第一個，後者永不觸發——#526 的原始症狀：`pet(19:00)` 從未跑過）。loader 對重複 block name 採 keep-first（與壞 time、缺欄位同樣 tolerant-by-contract，不整檔 reject）。

「同一活動一天做好幾次」的**正規寫法是 time list**，不是重複 name：

```toml
[[anchors]]
time = ["10:00", "19:00"]   # 一個身分、多個時段
name = "pet"
meaning = "..."             # 一份 meaning、一個 ## pet weave section
```

loader 把這種 block 展開成「每個有效時段一個 `Anchor`」，並只在活動**真的多時段**時給 trigger 加 `@HHMM` 後綴（`circadian:phase_pet@1000` / `@1900`），單時段維持裸 `circadian:phase_<name>`（向後相容）。list 裡的壞時段個別丟棄，不影響其他時段。

→ 「同類但語義不同」的活動（例如想給早晚 pet 各自的 meaning）用**不同 name**（`pet` / `pet_evening`），各自一個 weave section；「同一件事重複」用 time list。

**Phase 權限欄位（issue #525，2026-06-07）**：

每天喚醒的 daily session 是新的 `PermissionContext`（grant 是 session-scoped 記憶體）。所以一個 phase 要做的 routine-safe 工具動作（早上查資料寫新聞心得、跑餵貓腳本）每天都會落回 GUARDED → 重問 DK → 在無限等授權下造成 phase drift。

修法不是新系統，而是**接上既有管道**：`schedules.toml` 的 entry 早就能帶 `allowed_tools` / `scope_grants`（#444），經 `CronTrigger → planner → ChimeRequest → bot._apply_chime_permissions` 預先授權整個 turn、turn 結束 revoke。circadian 的 **phase chime（`_deliver_phase_chime`）原本建 `ChimeRequest` 時沒帶這些欄位** —— 這是唯一的斷點。

- `Anchor` 新增 `allowed_tools` / `scope_grants`，rhythm loader 經**共享的** `loom/autonomy/permission_fields.py::parse_permission_fields` 解析（schedules 載入也改用同一條，純 code 層統一）。
- `_deliver_phase_chime` 把 `anchor.*` 轉進 `ChimeRequest`，比照 schedule 路徑。下游套用/撤銷全部沿用既有 bot 機制。
- **兩種授權單位，依工具選**（Codex review #527 釐清）：
  - `allowed_tools = ["run_bash"]` 是**工具名 blanket** 預授權，整個 phase turn 內該工具不再問。`is_authorized` 對 pre-authorized 工具短路放行，含 EXEC（morning_briefing 實證）。適合 read-only 或本身已窄的工具。
  - `scope_grants` 是**收束**：把一個寬工具 fence 在資源前綴內（`write_file` 只能寫 `autonomy/circadian`）。**不要**把被 fence 的工具同時放進 `allowed_tools` —— 那是 blanket 形式，會讓 fence 失效。
- **`trust_level` 不是 phase 欄位**：chime path 不經 planner 的 notify/HOLD gate，放上去只是 inert 的假承諾。phase 授權只有 `allowed_tools` + `scope_grants`。
- **harness 修正（#525 / Codex review）**：`BlastRadiusMiddleware._scope_aware_process` 的 legacy tool-name bypass 原本在 `CONFIRM` 跟 `EXPAND_SCOPE` 都套用，使「宣告了 scope_grant 又 blanket 了工具名」時 scope 形同虛設。改成 **bypass 只在 `CONFIRM`（沒宣告任何相關 grant）套用**；`EXPAND_SCOPE`（有宣告該 resource 的 grant、但這次超出 selector）時 scope 為準 → 超界拒絕。這讓 scope_grants 的安全承諾在 runtime 真正成立，且不影響只用 allowed_tools（無 scope）的 schedule。
- **安全底線不動**：CRITICAL 永遠重問（`is_authorized` 對 CRITICAL 恆 False，與 grant 無關）。grant 只在該 phase turn 存活、用完即撤。
- **這是 agent 自主管理面**：絲絲想讓某 phase 多做一件事就在自己的 `rhythm.toml` 加欄位，晚上跟 DK 討論（宣告式，跟 schedules.toml 同一套心智）。

**不做**：
- ❌ daily_weave.md 讀取（PR 3，但 gate 的 plan mtime check 用 stub 路徑）
- ❌ nightly planning tool（PR 4）
- ❌ phase meaning 配置化（保持 hardcode，避免 config 稀釋語義）

**Acceptance criteria**：
- [ ] 整天無互動：phase_log 顯示多次 skip、daemon log 0 次 LLM call、Discord 只看到 `dawn` 跟 `evening_closure` 兩次
- [ ] DK 在 daily thread 發訊息後，下一次 tick gate 通過 → phase chime delivered
- [ ] `target.type = "circadian_today"` 但 state 沒有 today's session → daemon log 顯示 fallback=skip
- [ ] `circadian_today` 不影響既有 `discord_thread` chime 的行為（regression test）
- [ ] cheap gate skip reason 寫入 `state.phase_log`，archive 後可查
- [ ] dawn phase chime 攜帶 phase metadata，agent prompt 可區分
- [ ] **【生活語義 review】手動翻三個 phase 實際發出的 chime body，確認內容保留藍圖描述的「日常生活」語感，不是技術 phase name + 空 intent**
  - dawn body 必須有「成為今天的絲絲」語義，不只是「dawn phase fired」
  - shared_learning 必須有「和 DK 一起吸收世界」語義，不只是「news task」
  - evening_closure 必須有「人際收束 + 收織 + 安排明天」三層語義，不只是「close session」

---

### PR 3 — Daily Weave plan reader（對應 P1，issue #461）✅ 落地

> **PR2 重寫後的調整**（2026-05-28）：path 走 hardcoded `autonomy/circadian/daily_weave.md`（跟 PR2 rhythm.toml 對稱，不加 `plan_path` config）；gate.py mtime check 移除（gate.py 已隨 PR2 退役，B 模型無 blanket tick 要 gate — mtime 觀察未來可由 #478 EventTrigger 接手）。

**Scope**：
- 新增 `loom/autonomy/circadian/weave.py`：
  - `WeavePlan` dataclass + `load_weave(path) → WeavePlan`（empty plan 不是 None）
  - `_parse_h2_sections(text)` regex-based，preserves inner markdown（bullets / blockquotes / code fences）
  - 容錯：檔案不在、section 缺、亂格式、duplicate H2 都不炸
- `_deliver_phase_chime` 改走 `_compose_chime_intent(anchor)`：composition order = `anchor.meaning`（rhythm，scaffolding）→ `**今日織程**\n{weave_section}`（today's items）→ generic fallback
- `.gitignore` 加 `autonomy/circadian/daily_weave.md`（user-specific）
- 範例 `autonomy/circadian/daily_weave.example.md`（H2 用 anchor.name = `dawn` / `shared_learning` / …，bodies markdown）

**Acceptance criteria**：
- [x] DK 改 plan，隔天 dawn chime 內容跟著變（每次 fire load file，沒 cache）
- [x] 不存在 plan 檔時系統不炸、phase chime 仍正常發（用 `anchor.meaning` alone）
- [x] meaning 為空但 section 存在 → chime body 是 section 自己，不退到 generic fallback
- [x] H2 對應 phase name 直接 match（簡單 contract，例：`## dawn` ↔ `name = "dawn"`）

**~~移除~~**：~~plan mtime → cheap gate state transition~~（gate 已退役；未來如要 plan-changed 喚醒走 #478 EventTrigger）

---

### PR 4 — Weave revision tool（對應 P1，issue #462）✅ 落地

> **DK 翻案（2026-05-28）**：DK 明說「我不想 confirm，只需要跟我報告就」。propose → confirm → apply 三段流程砍掉，合併成「propose-and-apply 一步」，僅保留 audit trail + dawn 報告。原 spec 的 `allow_autonomous_weave_write` flag、`weave_apply`/`weave_reject` 獨立 tool、自然語言/slash/button confirm 機制 — 全部不做。
>
> 絲絲原 user requirement「不要 autonomous overwrite」**被翻**。Loom 的 owner 是 DK；DK 為自己 design call 拍板優先於絲絲對「希望被保護」的願望。安全保證改靠**結構性**（mtime guard 保 DK 手改優先、audit artifact 永遠寫出讓 DK 可 git revert）。

**Scope**：
- 新增 `loom/autonomy/circadian/proposal.py`：
  - `WeaveProposal` / `Change` dataclasses（TOML schema，非 markdown diff — 對齊 [[feedback_prefer_structured_over_parser]]、避 PR3 fence-bug 重演）
  - `apply_changes(sections, changes)` — pure transform，all-or-nothing 語義
  - `render_weave_markdown(prelude, sections)` — 重組成 markdown，prelude 原樣保留（DK 自定 header / mood / 雜記不被洗掉）
  - **`weave_revise` tool** (trust=SAFE, cap=MUTATES)：
    1. stable snapshot daily_weave.md：`stat-before → read → stat-after`，若 `st_mtime_ns` 不一致視為 torn read → tool 回 error、檔案不動
    2. 寫 proposal artifact 到 `proposals/<date>-evening.toml`
    3. `apply_changes` 套用、失敗 → all-or-nothing 退回
    4. **mtime guard**：atomic write 前再讀 `st_mtime_ns_B`、若 ≠ snapshot 的 `st_mtime_ns_A` → proposal 移到 `proposals/conflicts/`、daily_weave.md 不動、回 error
    5. atomic write daily_weave.md
    6. proposal 搬到 `proposals/applied/`
- `loom/autonomy/circadian/weave.py` 加 `load_weave_for_revision(path) → (prelude, sections, mtime_ns)`（fence-aware prelude split + stable stat-read-stat snapshot）
- `lifecycle._compose_chime_intent` 加第 4 層：dawn anchor 偵測昨日 applied/conflict proposal → 注入「**昨夜你改了什麼**」/「**昨夜的調整被擋下了**」摘要
- `session.py` 在 tool 註冊段 register `make_weave_revise_tool()`
- `.gitignore` 加 `autonomy/circadian/proposals/`

**Acceptance criteria**：
- [x] `evening_closure` phase 絲絲呼叫 `weave_revise`，daily_weave.md 立即改、無需 confirm
- [x] proposal artifact 永遠寫出（rationale + based_on_mtime + changes）作 audit trail
- [x] DK 手改 daily_weave.md：讀取中的 torn snapshot 直接 fail；snapshot 後、write 前 mtime 不同 → proposal 移 `conflicts/`、檔案不動（DK 手改絕對優先）
- [x] dawn chime 帶「昨夜你改了什麼」摘要 + 提示絲絲開場跟 DK 簡述
- [x] conflict case dawn chime 帶「昨夜的調整被擋下了」+ 問 DK 處理
- [x] 非 dawn phase 不會看到 revision report 雜訊
- [x] add/remove/rename/replace 四種 action；prelude 自訂內容保留；fence 內 H2 不誤切

**不做（觀察期後評估）**：
- ❌ `circadian:weave_revised` event emit — 等 #472 EventTrigger publisher infra 成熟、有真實 subscriber 再接（proposal 檔本身已是 durable trace）
- ❌ proposal expiration / 多份 pending 排隊 — 第一版只承載最近一份 evening-of-X.toml
- ❌ phase 硬閘 — tool 永遠可叫；靠 dawn/evening chime body instruction 軟引導

---

### PR 5 — Weave journal artifact（對應 P2，issue #463）

**Scope**：
- `autonomy/circadian/journal/YYYY-MM-DD.md` 寫入
- 收織 phase tool `journal_append`
- 確認**不**進 semantic memory（藍圖 §10.1）

**Acceptance criteria**：
- [ ] 跑完一天有一份非空 journal
- [ ] semantic memory 沒被「10:00 餵了喵吉」這種流水帳污染
- [ ] journal 檔案是 dated（每天一份），不是 rolling

---

### PR 6 — 觀察期 polish

跑一兩週後再決定，不預先設計。可能項目（見對應延伸 issues）：
- 生活語義一週實測 review（issue #464）
- Weekly weave planning（issue #465）
- Persona refresh on dawn chime（issue #466）
- ConditionTrigger state-drift watchdog（issue #473）— 如果 PR 1 觀察期發現主路徑有漏網

---

## 8. Open Questions 收斂答案

藍圖 §13 八題，這份計畫採以下答案。若 review 時不同意，必須在 PR 1 動工前推翻：

| Q | 答案 | 依據 |
|---|---|---|
| Q1 platform vs core | core 定義 callback adapter，Discord bot 實作；CLI 暫 no-op | 對稱既有 `ChimeDelivery` |
| Q2 rolling vs dated | `daily_weave.md` rolling（計畫）+ `journal/YYYY-MM-DD.md` dated（紀錄） | 計畫要改、紀錄要凍 |
| Q3 tick 30m vs 1h | #460 重寫後不再用 blanket `tick_interval` 作公開喚醒 gate；節律由 per-agent rhythm + cron anchor 驅動 | 反應速度交給 user message 即時 trigger，不靠 tick |
| Q4 公開 vs silent | **dawn + evening_closure 強制公開**（絲絲 review 修正：bedtime + nightly_weave 合併成 evening_closure，避免每晚兩次固定訊息） | 一天兩次儀式性對話：開場 + 收束 |
| Q5 daily vs weekly planning | daily 小調（PR 4，`weave_revise` 直接套用 + dawn report），weekly 留 issue #465 後續 | 縮窄 MVP |
| Q6 journal 進 wiki | 不進 | 違背藍圖 §10 分離立場 |
| Q7 DK 整天不互動是否仍開 thread | 仍開 | 不然 close 路徑不存在 = compression 不觸發 |
| Q8 startup recovery | 跨日 startup 強制 close 昨日、archive state、等今日 dawn cron | 見 §5 第 3 列 |

---

## 9. 驗收劇本（給 DK 一天跑完看的東西）

PR 1 + PR 2 合併後，DK 應該能看到這樣的一天：

```
07:59  daemon log: circadian:dawn_spawn cron pending
08:00  daemon log: circadian:dawn_spawn fired
       bot log: ensure_today_session() → spawning new thread
       Discord:  #daily-life-2026-05-26 thread 出現
       state.json: {date:2026-05-26, thread_id:..., started_at:...}
       evaluator.emit("circadian:day_started")  ← issue #472
       daily thread 收到 <system_chime phase="dawn">
         醒來成為今天的絲絲。不是任務開始，是一天的人際開場⋯
       </system_chime>
       絲絲回應早安

08:30  daemon: circadian:tick fired
       cheap gate: skip (no_state_transition)
       state.phase_log += {phase:tick, outcome:skipped, reason:no_state_transition}
       Discord: 無動靜

09:00  daemon: circadian:tick fired
       cheap gate: skip (no_state_transition)
       Discord: 無動靜

09:00  daemon: circadian:shared_learning fired
       cheap gate: conditional public — DK 早上有發過訊息 → pass
       daily thread 收到 <system_chime phase="shared_learning">
         和 DK 一起吸收世界，不是「新聞任務」⋯
       </system_chime>
       絲絲挑一則真心覺得值得分享的，分享

[…午後類似節律，cheap gate 多數 skip…]

23:00  daemon: circadian:evening_closure fired（強制公開）
       daily thread 收到 <system_chime phase="evening_closure">
         一天的人際收束 + 收織 + 安排明天⋯
       </system_chime>
       絲絲：道晚安、決定今天哪些值得留下、產出隔天 weave proposal
       絲絲呼叫 weave_revise：
         - proposal 寫入 autonomy/circadian/proposals/2026-05-26-evening.toml
         - stable snapshot + mtime guard 通過 → daily_weave.md 立即 atomic update
         - 若 DK 同時手改 → proposal 移 conflicts/，daily_weave.md 不動
       不 emit weave event；proposal artifact 本身就是 durable trace（未來 issue #472 再接 subscriber）

00:00  daemon: circadian:nightly_close fired
       bot: circadian_close()
       session.stop() → memory finalization / compression
       state.closed_at 寫入
       state.json archive 到 ~/.loom/circadian/log/2026-05-26.json
       state.json 清空，等明天
       evaluator.emit("circadian:day_closed")  ← issue #472

==== 重啟驗證 ====
[午夜後殺 daemon + bot]
[10:00 重新啟動]
       daemon startup: 發現無 today's state → 等 dawn cron（已過）
       bot on_ready: 當前在 active hours 內、state 不是今天 → 主動呼叫 ensure_today_session()
       → 補開 thread、寫 state
       → 從這時起加入今日節律
       （若 on_ready 也沒跑：60s 內 watchdog 補開，issue #473）
```

---

## 10. 不該被綁的東西

實作過程一定會撞到誘惑：「順便重構一下 `AutonomyDaemon` / `bot.py` / `_close_session`」。

**這份計畫的核心是 surgical addition**：
- 不重構 `AutonomyDaemon`
- 不重構 `bot.py`（只擴 `deliver_chime` dispatch + 加 `on_ready` hook）
- 不重構 `_close_session`
- 不動 memory v2 進行中的工作（PR 1 acceptance 第 8 條只要求現有 `session.stop()` 路徑 work）

任何想做的清理另開 issue / PR。

---

## 11. Risk register

| Risk | 機率 | 嚴重度 | Mitigation |
|---|---|---|---|
| `session.stop()` 路徑在 memory v2 重構中改變 | 中 | 高 | PR 1 動工前 verify 此路徑 stable；若不 stable 暫停等 memory v2 落地 |
| Discord thread 命名 `daily-life-...` 相容性 | 低 | 低 | DK 拍板 ASCII 命名，相容性已驗證 |
| Cheap gate 規則過嚴 → 整天 silent | 中 | 中 | dawn + evening_closure 強制公開作護欄（兩次，不是三次） |
| **Phase 退化成 task scheduler，失去生活語感** | 中 | 高 | PR 2 Phase chime prompt contract hardcode 每個 phase 的 meaning；acceptance criteria 強制 review 實際 chime body 是否保留藍圖語感；issue #464 一週 retrospective |
| **Weave plan 被 autonomous 改壞** | 低 | 高 | PR 4 改為 `weave_revise` 直接套用、無 daily confirm；安全靠 audit artifact、dawn report、all-or-nothing transform、stable snapshot + `st_mtime_ns` guard，DK 可用 git revert |
| 雙 daemon 啟動造成 state 競爭 | 低 | 中 | flock + state 後置 `is_for_today()` 檢查 |
| Discord API rate limit（頻繁 verify thread）| 低 | 低 | verify 只在 cron 觸發跟 bot on_ready 時做，不在每個 tick 做 |
| DK 手動編 `daily_weave.md` 跟 `weave_revise` tool 同時寫 | 低 | 低 | stable stat-read-stat snapshot 避免讀到半套內容；atomic write 前再比 `st_mtime_ns`，不一致則移 `conflicts/` 並保留 DK 手改 |
| cron + on_ready 都漏網 | 低 | 中 | ConditionTrigger watchdog 60s polling（issue #473） |

---

## 12. Review checklist for 絲絲

對照本文件 review 時請逐條打勾：

- [ ] §1 五個目標是否都對齊 #458 issue / 藍圖目標
- [ ] §2 既有元件清單是否覆蓋，有沒有漏 reuse 既有東西
- [ ] §3 架構決策（C 案）是否真乾淨，有沒有 layer 邊界穿透
- [ ] §4 state schema 是否覆蓋所有需要的欄位
- [ ] §5 健壯性場景對照表是否有遺漏 case（特別歡迎指出）
- [ ] §6 模組結構是否真的不需要新 runtime
- [ ] §7 PR 切法每個 PR 是否真能獨立 merge、acceptance 是否真可驗
- [ ] §8 open question 收斂答案是否需要推翻
- [ ] §9 驗收劇本是否合理符合藍圖「日週期生命」精神
- [ ] §10 「不該被綁」清單是否有漏
- [ ] §11 risk 是否有遺漏
- [ ] §13 跨子系統 hook event 名稱與 payload 是否合理（issue #472）

對應藍圖 §12 成功標準的覆蓋：

| 藍圖成功標準 | 本計畫對應 |
|---|---|
| 每天有新 daily session / Discord 討論串 | PR 1 acceptance #1 |
| 白天日常互動集中在 daily session | PR 2（chime target 全走 circadian_today）|
| 工作 / 專題 session 自然分流 | 既有 thread → session 機制不變，無需新做 |
| 晚上 daily session 能關閉並觸發壓縮 | PR 1 acceptance #8 |
| tick 不造成無意義 token burn | PR 2 acceptance #1 |
| 絲絲能在收織階段安排明天 | PR 4 |
| 記憶系統只留下洞察，不留下流水帳 | PR 5 acceptance #2 |

---

## 13. 跨子系統 hook 與第三層 watchdog

> 對應 issue #472 (EventTrigger publisher) 跟 issue #473 (ConditionTrigger watchdog)
> 動機：補活既有 trigger infra（EventTrigger 有 toml loader 但沒 internal publisher；ConditionTrigger 連 toml loader 都沒寫的純孤兒）

### 13.1 EventTrigger publisher（issue #472）

Circadian 是天然的事件源頭。直接補 emit 點，把 dead infra 變 alive，未來子系統想 hook「絲絲今天結束」之類信號時直接寫 toml `[[autonomy.triggers]]` 監聽即可，不用改 circadian closer。

**Event 命名 convention**：`{domain}:{verb_object}` — 例：`circadian:day_closed`

**四個 emit 點與 payload schema**：

| Event 名稱 | 在哪 emit | PR | Payload |
|---|---|---|---|
| `circadian:day_started` | spawner 成功建 thread + 寫 state | PR 1 | `{date, thread_id, session_id}` |
| `circadian:day_closed` | closer 完成 `session.stop()` + archive state | PR 1 | `{date, thread_id, closed_at}` |
| `circadian:weave_revised` | deferred：`weave_revise` 成功直接套用後 emit | #472 後續 | `{date, proposal_path, applied_path}` |
| `circadian:weave_conflicted` | deferred：`weave_revise` 被 DK 手改 guard 擋下後 emit | #472 後續 | `{date, proposal_path, conflict_path}` |

**不做**：
- ❌ 不 land 任何 internal subscriber（等真實需求出現再接，避免 over-engineering）
- ❌ 不重構 event 命名 convention（先用 `domain:verb_object` pattern，未來有更多 event 再統一）

**示範 user-side subscriber**（寫在 `loom.toml.example`）：
```toml
# 範例：午夜 close 後做某件事
[[autonomy.triggers]]
name  = "example_after_closure"
event = "circadian:day_closed"
intent = "..."
trust_level = "guarded"
```

### 13.2 ConditionTrigger state-drift watchdog（issue #473）

Daemon 主 spawn 路徑有兩層（dawn cron + bot on_ready），但仍可能漏網：cron miss、on_ready hook bug、bot 多次斷線、daemon 啟動順序錯誤、time skew。

第三層 watchdog 走 ConditionTrigger 60s polling 補救：

```python
# daemon 啟動時動態註冊（不走 toml — condition_fn 是 closure）
evaluator.register(ConditionTrigger(
    name="circadian:state_drift_watchdog",
    intent="state drift detected — 補開 today's session",
    condition_fn=_state_drift,
))

def _state_drift() -> bool:
    if _spawning_in_progress:          # 護欄
        return False
    now = datetime.now(tz)
    if not is_in_active_hours(now):    # 只在 active hours 補
        return False
    state = CircadianState.load()
    if state is None:
        return True                    # 該有沒有
    if not state.is_for_today(tz):
        return True                    # 昨日殘留
    return False
```

Watchdog handler 走同個 `ensure_today_session()` 入口，既有 flock 仲裁保證 idempotent。`phase_log` 加 `{phase: watchdog, outcome: spawned_recovery}` 紀錄漏網次數 — 跑滿一月若 0 次代表主路徑夠強，watchdog 純保險；>5 次代表主路徑有結構問題要回頭加固。

**動工時機**：PR 1 跑滿一週後評估，有實證漏網才加。
