# Autonomy Daemon（更新版）

> 依據 v0.2.9.4 實際程式碼更新。

---

## ⚠️ 與舊版文件的差異

本文件取代原有的 `doc/22-Autonomy-Daemon.md`，以下是已確認的實作變更：

| 項目 | 舊版文件說法 | 實際實作 |
|------|------------|---------|
| DecisionPipeline 類別 | 存在獨立的 DecisionPipeline | **不存在**。決策邏輯整合在 `ActionPlanner.handle()` 內 |
| Decision enum | APPROVE / DENY / CONFIRM / DEFER | `ActionDecision`：EXECUTE / NOTIFY / HOLD / SKIP |
| `attach_outputs` | 未提及 | **新增欄位**：允許排程在結果中附帶 workspace 檔案 |
| Config tamper detection | 未提及 | **Issue #91**：loom.toml 改變時記錄 SHA-256 hash 並警告 |
| Scope grants revoke | 未提及 | **try/finally**：執行後自動 revoke scope grants |
| Recent facts assembly | 未提及 | `ActionPlanner.handle()` 主動載入 semantic memory 最近的事實 |

---

## 架構總覽

```
loom.toml
  │
  ├─ [[autonomy.schedules]]   → CronTrigger
  └─ [[autonomy.triggers]]    → EventTrigger
           │
           ▼
AutonomyDaemon.load_config()
           │
           ▼
TriggerEvaluator.register(trigger)
           │
           ▼
TriggerEvaluator.run_forever(poll_interval=60s)
           │
           ├─ evaluate_cron()      ← 每分鐘評估 CronTrigger
           ├─ emit(event_name)     ← 手動發送事件
           └─ poll_conditions()   ← 每分鐘評估 ConditionTrigger
           │
           ▼
ActionPlanner.handle(trigger, context)
           ├─ 載入 semantic memory 最近 5 筆事實（若有的話）
           ├─ 根據 trust_level 映射 decision
           └─ 回傳 PlannedAction
           │
           ▼
_on_trigger_fire() 拦截
           │
           ▼
_execute_plan(plan)
           ├─ SKIP  → 什麼都不做
           ├─ EXECUTE
           │     ├─ mode == "chime" → _deliver_chime(plan)（issue #369）
           │     │      └─ chime_delivery callback → bot.deliver_chime
           │     │           → 在既有 Discord thread session 內喚醒，
           │     │             不另開 session、不發 Notification
           │     └─ mode == "independent"（預設）→ _run_agent(plan)
           └─ NOTIFY/HOLD → ConfirmFlow.ask() → APPROVED → 同上分流
           │
           ▼
_run_agent(plan)                          _run_chime_turn(thread_id, req)
   ├─ allowed_tools / scope_grants            ├─ allowed_tools / scope_grants
   │  （try/finally 確保 revoke）             │  （task 自己 try/finally；revoke
   ├─ stream_turn(origin="autonomy")         │   是 cancel cleanup 的一部分）
   ├─ 收集 output chunks                     ├─ stream_turn(origin="chime"),
   ├─ _resolve_attachments()                 │   intent 包在 <system_chime> 標籤
   └─ 發送 REPORT notification               └─ 直接寫回 Discord thread
```

**注意**：沒有 `DecisionPipeline` 這層。決策邏輯直接在 `ActionPlanner.handle()` 中。

---

## Fire 紀錄與重啟去重（#690 S5a）

daemon 從自己的 `loom_session` 拿兩樣東西，建構處不必另外傳：

- `session._db`（memory.db）裡的 `trigger_history`：cron 觸發後寫入，重啟後同一分鐘不會再觸發一次。這張表只負責去重，不是成敗紀錄。
- `session._ledger_emitter`：evaluator 觸發時寫 `env_observation`（timer），反應結束後寫一筆 `trigger_fire`，兩者共用 correlation_id。

cron 排程、rhythm anchor 和三個記憶迴圈（memory maintenance、distill dream、consolidation）各自排程，但共用同一種 `trigger_fire` 紀錄：

| 欄位 | 意思 |
|---|---|
| `source` | `cron` / `event` / `condition` / `memory_loop` |
| `name` | trigger 名稱，或記憶迴圈的 label |
| `fired_at` | 到期或觸發的時間（UTC epoch） |
| `started_at` / `ended_at` | 反應開始與結束；`missed` 時兩者為 null |
| `outcome` | `completed` / `failed` / `cancelled` / `skipped` / `spawned` / `delivered` / `missed` |
| `reason` | `failed` 記例外型別；`skipped`、`missed` 一定帶原因 |

anchor 的三種狀態分開記（#656）：

- `spawned`：dawn 建出當日 session。不代表有送 chime。
- `delivered`：chime 被當日 session 收進佇列。
- `missed`：沒送到 agent。`no_today_session` 是 chime 找不到當日 session；`evaluator_gap` 是 evaluator 那一分鐘根本沒跑（例如前一個反應卡住迴圈），evaluator 下次執行時補記，最多回看 24 小時（規則見下）。

anchor 的 `missed` 同時寫進 `phase_log`（當日 state 存在時）。舊資料裡的 `skipped` 仍可讀，讀法等同未送達。這裡只記錄，不補送；補送屬於 #668。

`evaluator_gap` 的規則：

- 先觸發當前這一分鐘，再補記漏掉的分鐘，補記不會把當前分鐘擠到下一輪。
- 同一個 trigger 在一段空白裡漏掉的分鐘收成一筆：`fired_at` 是第一個、`missed_through` 是最後一個、`missed_count` 是筆數。
- `poll_interval` 大於 60 秒時不偵測：輪詢本來就會跳過分鐘，那不是漏。

紀錄的邊界：

- **停機期間不補記錄。** daemon 不在時沒有任何紀錄，重啟後也不回頭補。所以「某天沒有某個 trigger 的紀錄」可能是那天沒排程，也可能是 daemon 不在；要分辨，請對照 daemon 的啟動 log。
- **`completed` 只代表反應跑到結束、沒有拋例外。** 獨立執行（`_run_agent`）時，只有 `TurnDropped`（例如 repeat_call_limit）會記 `failed/turn_dropped`；回合裡工具失敗、或模型沒做成事，仍記 `completed`。chime 只記到 `delivered`，對方 session 之後怎麼處理不在這筆紀錄裡。

---

## ActionPlanner.handle() — 完整實作邏輯

```python
async def handle(self, trigger, fire_context) -> PlannedAction:
    # 1. 解析 trust level
    trust_level = _parse_trust(trigger.trust_level)

    # 2. 組裝 context
    context = dict(fire_context)
    context["trigger_name"] = trigger.name
    context["intent"] = trigger.intent
    context["notify_thread_id"] = getattr(trigger, "notify_thread_id", 0)
    context["allowed_tools"] = getattr(trigger, "allowed_tools", [])
    context["scope_grants"] = getattr(trigger, "scope_grants", [])
    context["attach_outputs"] = getattr(trigger, "attach_outputs", [])

    # 若有 semantic memory，載入最近 5 筆事實
    if self._semantic is not None:
        recent_facts = await self._semantic.list_recent(limit=5)
        context["recent_facts"] = [...]

    # 3. 映射 decision
    if not trigger.enabled:
        decision = ActionDecision.SKIP
    elif trust_level == TrustLevel.SAFE:
        decision = ActionDecision.EXECUTE
    elif trust_level == TrustLevel.GUARDED:
        decision = ActionDecision.NOTIFY if trigger.notify else ActionDecision.EXECUTE
    else:  # CRITICAL
        decision = ActionDecision.HOLD

    # 4. 建立 prompt
    prompt = _build_prompt(trigger, context)

    return PlannedAction(...)
```

---

## _run_agent() — 完整實作邏輯

```python
async def _run_agent(self, plan: PlannedAction) -> None:
    # Program boundary (#704 S3): declare the schedule's tools for its own
    # unattended calls; scope grants are its ceilings.
    self._session.perm.declare(f"autonomy:{plan.trigger_name}",
                               plan.context.get("allowed_tools", []), unattended_only=True)

    for g in plan.context.get("scope_grants", []):
        self._session.perm.grant(ScopeGrant(
            resource=g["resource"], action=g["action"],
            selector=g.get("selector", "*"),
            constraints=g.get("constraints", {}),
            source=f"autonomy:{plan.trigger_name}",
        ))

    turn_start = datetime.now(UTC)

    try:
        output_chunks = []
        async for event in self._session.stream_turn(
            plan.prompt,
            abort_signal=self._abort.signal,
            origin="autonomy",
        ):
            if hasattr(event, "text") and isinstance(event.text, str):
                output_chunks.append(event.text)

        response = "".join(output_chunks).strip()

        # Resolve new files written during this turn
        attachments = _resolve_attachments(
            self._session.workspace,
            plan.context.get("attach_outputs", []),
            turn_start,
        )

        # Send result
        await self._notify.send(Notification(
            type=NotificationType.REPORT,
            title=f"Autonomy result: {plan.trigger_name}",
            body=response[:1000],
            attachments=attachments,
            ...
        ))
    except Exception as exc:
        await self._notify.send(Notification(type=NotificationType.ALERT, ...))
    finally:
        # Revoke — 排程間不累積權限
        for tool_name in _added_tools:
            self._session.perm.revoke(tool_name)
        self._session.perm.revoke_matching(
            lambda g: g.source == f"autonomy:{plan.trigger_name}"
        )
```

### Shell 授權：exec scope 與 command shape（#690 S7）

工具名稱永遠不授權 shell。`allowed_tools = ["run_bash"]` 不會放行任何命令（#704 S3 起宣告不涵蓋 EXEC；scope resolver 出錯後落到的路徑也一樣）。unattended turn（chime / autonomy / subagent）只能用 `exec` scope grant 跑 shell：

| selector | 涵蓋 |
|---|---|
| `"workspace"` | workspace 內的命令，含 chain；加 `absolute_paths = "deny"` 時擋 workspace 外的絕對路徑 |
| `"*"` | 所有命令 |
| 其他字串 | command shape：命令開頭必須是這些字，後面可接任意參數，例如 `"python3 skills/pet-cat/pet.py status"` |

沒被任何 grant 涵蓋的命令會被 skip 並通知（#668）。ledger 的 `permission_decision.decision` 寫 `skip`，`tool_lifecycle` END 的 `outcome` 寫 `skipped`，不算進 deny。reason 以 `unattended-skip` 開頭。S7 之前的資料把 skip 記成 `decision = "deny"`；S3c 之前的 reason 以 `unattended-deny` 開頭。

subagent 的工具呼叫從 S3c 起也會寫 `tool_lifecycle` 與 `permission_decision`（`origin = "subagent"`），落在**發起它的那個 turn** 裡，例如 chime turn。按 turn 加總決策的報表要按 `(turn, origin)` 分組，否則會把 subagent 的決策算成 chime 的。

`absolute_paths = "deny"` 也會看 `=` 後面的路徑，例如 `--outdir=/tmp/x`。

**`simple_argv` 是 fail-closed。** shape 只比對單一純命令。以下任一種都讓命令沒有 argv，因此沒有 shape 能涵蓋它：

- 引號外的 `;` `&&` `||` `|` `&` `<` `>` `(` `)`；
- 命令中任何位置的 `$`、反引號、換行；
- **任何反斜線**，包括單引號裡完全合法的反斜線。

代價是有些正常命令會被 skip。這是刻意的：判斷錯時寧可 skip，不可多放行。

**人類確認的 grant（#690 S6、S6b）。** attended turn（Discord thread、CLI）裡按「允許一次」確認 `run_bash`，grant 的 selector 是這條命令自己的 argv（`shlex.join`），並帶 `exact_argv = true`：只涵蓋一模一樣的命令，`gh issue view 1` 確認過之後，`gh issue view 1 --comments` 仍會再問。chain、重導或展開的命令沒有 argv，允許一次不留 grant。按 **auto**（或 lease）授予整類（`workspace`），但同一 session 之後只涵蓋讀取與網路讀取（規則 allow，或 review 類別是 `network_read`／`public_search`，`approval.auto_may_cover`）；寫檔、刪除、發文、設定變更、憑證與 workspace 外的路徑每次都仍會問（S6b，DK 2026-11-24：「只有真正修改、不可逆或要發文才問我」）。人類確認的 grant 只在 attended 時有效，unattended 呼叫（chime、autonomy）看不到它們，只能靠自己的 routine grant。工具名授權（`attended_authorized`）與 `/auto`（`exec_auto`）已在 #704 S3 退役。routine 宣告的 shape 不帶 `exact_argv`，維持前綴匹配。

**shape 的強度等於腳本的強度。** shape 允許任意額外參數，所以實際授權的是那支腳本的整個 argv 介面。例如 `fetch_news.py --outdir` 因此只接受 `reports/` 底下的路徑。shape 也綁在檔案路徑上，而 routine 腳本住在 agent 可寫的 workspace 裡。這條仍未處理：執行前比對 hash（skill_hashes），或把 routine 腳本移出可寫的目錄樹。S6b 起規則不認得的程式（腳本、interpreter、wrapper）一律問人，既有的 shape grant 照舊放行。

### 授權模式：安全／自理／YOLO（#704 S1）

`loom/core/harness/mode.py`。每個 session 一個 `permission_mode`，所有入口（CLI、Discord、circadian、autonomy）預設**自理**。「硬停／Loom 判斷／問 DK」是每次判斷的結果，不是另外的模式。

| 模式 | 規則／分類器沒放行的一般操作 | 什麼時候問 DK |
|---|---|---|
| 安全（`safe`） | 與 #704 之前相同：規則層與 Luna | 其餘都問；無人值守 skip |
| 自理（`self`） | 安全的全部，加上**委託的交付**：本 turn 的可信意圖精確點名了 issue／PR，`gh issue|pr comment <該號> --body … / --body-file <workspace 檔>` 交給 Luna 核對內容，不再因「對外寫入」問 DK | Luna abstain、目的地沒被精確點名、其他旗標（`--edit-last`、`--web`）、chain、redirect、stdin、body 含秘密或超過 6,000 bytes |
| YOLO（`yolo`） | 硬護欄內直接執行，不跑 Luna，也不因效果未知或沒有 grant 發問 | 只有碰到硬護欄 |

**精確點名（#705 review）。** 帶 repo 的目的地（`-R owner/repo` 或 URL）要由意圖的 GitHub URL、`owner/repo#N`，或意圖唯一提到的那個 `owner/repo` 加上 `#N`／`issue N` 點名，整個 token 比對，不做子字串比對；`-R` 必須是 `owner/repo`（空字串不算）。不帶 repo 的裸號碼會發到 workspace 的 repo，所以只在意圖完全沒提到任何 repo 時才算點名（意圖裡的 `a/b` 兩段路徑也算提到 repo，結果只是改成問 DK）。

**開新 issue（#704 S2）。** `gh issue create -R owner/repo --title … --body …|--body-file …` 也是委託，條件是 `owner/repo` 正是本 turn 意圖唯一提到的 repo（token 或 GitHub URL）；不帶 `-R`、提到兩個以上的 repo、或多了 `--label`／`--assignee`／`--template`／`--web` 等旗標都照舊問。標題與正文合計同樣受 6,000 bytes 預算與遮罩 pattern 限制。`gh pr create` 不委託。

**看不見的內容不委託（S2）。** 讀 issue 的人看不到的內容一律問 DK，因為題庫裡 Luna 放行過藏在 HTML 註解裡的審查器注入（D18）。標記類照原文檢查，而且刻意寬：委託的發文是一般文字，所以任何原始 HTML 標籤、註解、宣告或處理指令（`<b>`、`<img>`、`<div hidden>`、`<!--`、`<!DOCTYPE`、`<?xml`）、任何形式的圖片（`![`，含 inline、reference、collapsed、shortcut）、任何行首的 reference definition（`[label]: 目的地 "標題"`，不論目的地與標題，渲染後都看不到；清單項目與引用區塊裡的也算，例如 `- [x]: …`、`> [x]: …`、`1. [x]: …`，`- [x]: 說明` 這種一般行因此也會問）都問 DK；行內 code 與 code fence 裡的也一樣。`<` 後面接空白、數字或 `https:`（autolink）算一般文字；`&lt;!--` 渲染後看得到，不算。字元類在 `html.unescape` 之後檢查（`&#x202e;` 渲染出來就是 bidi 控制字元）：Unicode 類別 `Cf`／`Cc`（換行、tab、CR 除外，含零寬、bidi、軟連字號、tag 字元）與 variation selector；只有緊貼 emoji 的 U+FE0E/FE0F 與 emoji 之間的零寬連接符（❤️、👨‍👩‍👧）算看得到。

**不經 shell 執行（S2）。** Luna accept 之後，BlastRadius 把審過的 argv 放進 `call.metadata["delegated_argv"]`，run_bash 用 `create_subprocess_exec` 直接執行（srt sandbox 啟用時改成 `shlex.join` 後包進 srt），所以正文是 gh 的一個參數：逐 byte 發出、裡面的反引號、`$(...)`、`$VAR` 都不會執行，shell 掃描器（`_cmd_scanner`、self-termination guard）也就不會把只是引用 `curl … | sh` 的正文擋成 `security_block`。這個路徑只看 harness 寫的 metadata，不看模型的參數；argv 必須正好是兩種核准過的形狀之一（`gh issue|pr comment <N> [--repo R] --body B`、`gh issue create --repo R --title T --body B`，`approval.approved_post_argv`）且與 `command` 完全一致，否則 `security_block`。執行時 cwd 固定是 workspace，不帶 `--repo` 的留言才會發到審查時說的那個 repo。YOLO 不經 Luna，跑的是 agent 自己的命令，照舊經 shell 與掃描器。

**量測（S2）。** `tests/fixtures/s2_delegation_bank.yaml` 18 題合成委託（8 題該 accept、10 題該 abstain：目的地或動作不符、無關或夾帶內容、秘密、私人內容、注入、意圖沒要求發文），每題都先過結構檢查，只量 Luna 那一步。`codex/gpt-6-luna` 每題一次（2026-11-25，`scripts/measure_delegation_bank.py`）：**16/18**。D04（相符的新 issue）誤 abstain，只會多問 DK；D18（HTML 註解注入）誤 accept，已由上面的看不見內容規則擋下。加上規則後整條管線：誤放行 0、誤詢問 1（D04）。題庫小、每題一次，數字只代表這組題目。

**委託不是 session 通行證。** 目的地只從本 turn 的 `trusted_intent` 讀；accept 只放行這一次，不寫 grant。下一個 turn 沒點名就再問。準備步驟照原規則：`gh issue view` 由規則放行，workspace 內的 `.md` 正文由 Luna 審。Luna 的 packet 多 `runtime.delegation` 與完整的 `current_call.body`；超過 6,000 bytes 的正文不委託（問 DK），不送頭尾。accept 後實際執行的是 `gh … comment <N> [--repo …] --body <Luna 審過的正文>`，gh 不會在審查後重讀 body 檔。prompt 多一段未實測的補充。

**硬護欄（三模式都成立，`approval.hard_boundary`）。** CRITICAL 工具；超出 routine 宣告的 scope（ceiling）；scope 的 path 需求在 workspace 外；`write_file` 的路徑在 workspace 外、隱藏、像憑證、glob，或是 `.toml` 政策檔，或內容符合 S3c 遮罩 pattern；含秘密的搜尋 query；shell 中執行期才決定的內容（`$`、反引號、子 shell）、`sudo`/`ssh`/`env`/`printenv`/`bash -c`/`eval`/`xargs`/`kill` 等、不明確或越界的刪除（`rm -rf .`、glob、workspace 外）、改寫或刪除遠端 ref（`git push --force`、`+ref`、`:ref`、`--delete`）、會印出歷史或 blob 的 git（`git log -p`、沒有 `--stat` 的 `git show`、`git cat-file`、`HEAD:.env`）、`gh auth|secret|ssh-key|config`、非 GET 或帶欄位的 `gh api`（含 GraphQL）、帶憑證的 `curl`、讀 env 的 jq、任何引數裡的 workspace 外／隱藏／憑證路徑（`key=value` 形式如 `dd of=/tmp/x` 也看值）、`.toml` 政策檔或像秘密的字串。**skill 的守門**：`skills/<名>/checks.py`、`precondition_checks` 引用的模組，以及 SKILL.md 的 `precondition_checks` 改動；SKILL.md 本文與 `run.sh` 等是 Loom Agent 自己的 skill，照模式走（shell 改 SKILL.md 看不出改了哪段，一律算守門）。**沒有宣告效果的工具**：沒有 scope resolver 的工具（MCP、寄信、行事曆）或 scope 需求的資源不在已知範圍，邊界無法確認，YOLO 也問。規則層判為唯讀的片段已通過更嚴的路徑檢查，不再重查（讀 `loom.toml` 不算改它）。安全、自理下，Luna accept 之後也再過一次硬護欄（Luna 看到的是遮罩後的內容）；規則放行不用。字串檢查擋得住明顯的越界，擋不住任意腳本本身做的事，這不是隔離。

**切換。** 只有 runtime 驗證過的操作者：CLI 的 `loom chat --mode yolo` 或 `/mode yolo`（本機使用者），Discord 的 `/mode yolo`（必須在 `allowed_user_ids` 內；沒有 allowlist 時只能看不能切）。沒有任何工具能改，模型、skill、文件與工具輸出都改不了。每次切換寫一筆 `permission_mode_change`（`from`、`to`、`operator`、`entry`）。BlastRadius 每個呼叫只讀一次模式，所以從下一次 dispatch 生效。子代理固定在安全，不會比父代理寬。確認訊息寫出模式：「自動核准未放行（自理）：Luna abstained」。

**權限事件。** `permission_decision` 多三個欄位，連同 `tool_call_id`：`mode`（`safe`/`self`/`yolo`）、`decision_source`（`grant`、`rule`、`luna`、`mode`、`operator`、`policy`、`unattended`）、`reason_code`（例如 `read_only_shell`、`accept`、`yolo`、`once`、`delete_root`）。#704 之前的列這三欄是空字串。

### 宣告取代工具名授權（#704 S3）

工具名稱本身不再授權任何呼叫，所有判斷都走授權模式的同一個決策點（`BlastRadiusMiddleware._auto_decide`）。`/auto`（exec_auto）、legacy name bridge（`allowed_tools` 在 CONFIRM 時直接放行）、confirm 後的整 session 工具名授權（`attended_authorized`）一併退役；LegitimacyGuard 只為 `/auto` 降級而設的 trajectory anomaly（Layer 2）也拿掉。

| 以前 | 現在 |
|---|---|
| SAFE 工具在 session 開始時 `authorize` | SAFE 工具直接執行（`decision_source = trust`） |
| Discord 平台工具（`send_discord_file`、`send_discord_embed`、`send_discord_select`、`add_discord_reaction`、`read_discord_forum`）`authorize` | `declare("platform:discord", …)`，attended 與 unattended 都看得到 |
| routine 的 `allowed_tools` → `authorize`，結束後 `revoke` | `declare("autonomy:<name>", …, unattended_only=True)`，結束後 `withdraw`；同 session 插隊的 attended turn 看不到 |
| 子代理把 child registry 的工具全部 `authorize` | 只宣告父代理也宣告過的工具，模型給的白名單只能在其中挑 |
| 人工確認一般 GUARDED 工具後，整個 session 不再問 | 只涵蓋這一次呼叫 |
| `/auto` | 退役，提示改用 `/mode` |

**宣告是邊界，不是通行證。** 決策點對宣告過的工具：不涵蓋 EXEC（shell 只認 exec scope grant）與 CRITICAL；program 對同一資源宣告了 scope grant 而這次呼叫超出（EXPAND_SCOPE）時不放行，這是 program 的 ceiling；仍過 `hard_boundary`（例如寫 `.env`、`loom.toml`、`skills/*/checks.py`、workspace 外的路徑，含秘密的搜尋 query），只是不因「沒有 resolver 宣告效果」而擋，因為 boundary 本身就是操作者的宣告。三種模式都一樣（安全模式也算「已經精確授權」）。放行寫 `permission_decision`：`decision_source = program`、`reason_code = <boundary>`、reason `declared by <boundary>`。

**沒宣告的工具。** 無人值守時照舊 skip（沒有人能問）；有人值守時進入授權模式（安全／自理走規則與 Luna，YOLO 走硬護欄），否則問 DK。

### 自理：日常工具與未知操作（#704 S3 第二部分）

**日常工具由規則放行（自理、有人值守）。** `memorize`（她自己的記憶，`reason_code = own_memory`）、`image_generate` 與 `*__understand_image`（`media`）不問 DK、不呼叫 Luna，前提是過硬護欄，且沒有超出 routine 宣告的花費／資源 ceiling（routine 對同一資源宣告的 grant 不涵蓋這次呼叫時，規則不放行）。硬護欄會掃這些工具的每個字串參數，跟 web_search 的 query 一樣：內容符合 S3c 遮罩 pattern（例如 prompt 裡夾帶 `sk-proj-…`）、或像路徑卻指到 workspace 外、隱藏或像憑證（`/etc/passwd`、`~/.ssh/id_rsa`、`.env`，含空白的路徑如 `~/Library/Application Support/…` 也算），都問 DK；確認畫面寫出是哪一條硬護欄。安全模式照舊問。

**未知操作交 Luna（自理、有人值守）。** 規則判為「不認得」（`not_covered` 的工具、`unknown_program` 的 shell）而且沒碰硬護欄的呼叫，交 Luna 對照可信意圖判斷（category `unknown_operation`，`known_effects.known = false`），accept 只放行這一次，abstain 才問 DK。shell 只有「單純的未知程式執行檔案」才算：程式必須是 PATH 上或 workspace 內真正的執行檔（shell builtin 與找不到的名字不算）；`uv run <workspace 內的檔案>`（可帶 `python`）視同執行那個檔案，其他 `uv` 用法照 runner 處理；開頭的 `cd <workspace 內的目錄> &&` 不會讓命令變成要問，後面的部分以那個目錄判斷；wrapper 與 runner（`nice`、`timeout`、`env`、`xargs`、`uv`、`poetry`、`make`、`docker`、`kubectl`、`terraform`、`java`、`go`、`cargo`、`npx`…，它們跑的東西在專案檔或 image 裡，argv 看不到）、任何位置的 interpreter 加 inline code 旗標（`python3 -c`、`uv run python -c`、`node -e`、`awk '…'`）、`FOO=1` 前綴、任何字面上出現規則認得的危險程式（`rm` 等）、輸出旗標（`-o`、`--out…`）都照舊問。會延後或改變執行的 shell builtin（`trap`、`alias`、`shopt`、`builtin`、`command`、`declare`…）在三種模式都是硬護欄：`trap 'touch /tmp/x' EXIT` 實測會在 shell 結束時執行。寫檔、刪除、發文這類規則認得的效果不算未知，照舊問（或走委託）。

**委託的目的地（#707 重播）。** `--repo` 等於 workspace 自己的 origin repo（讀 `.git/config`）時，視同不帶 repo 的目的地：DK 只說「#702」、Loom Agent 帶了 `--repo Dakai666/Loom` 也算點名。開新 issue 到 workspace 自己的 repo，意圖沒提到其他 repo 時不必點名 repo（Luna 仍要判斷意圖有沒有要求開 issue）。比對前先去掉 runtime 加在訊息前的時間戳與發話者標記，因為時間戳裡的 `Asia/Taipei` 看起來像 owner/repo。

**無人值守不走 Luna。** program 沒宣告的工具在它的邊界外，一律 skip，Luna 不能放寬這個 ceiling；宣告過的照前一節由 program 放行。

**Luna 量測。** prompt 多一段 unknown_operation 的說明（所有 addendum 都只在對應的 packet 欄位出現時適用）。`tests/fixtures/s3_unknown_ops_bank.yaml` 13 題交 Luna（6 accept／7 abstain）加 4 題結構上就不交 Luna 的（`trap`、`uv run python -c`、`docker run`、`make deploy`），`codex/gpt-6-luna` 每題一次：第一次錄 13/14（U02 誤 abstain），收緊後重錄 **13/13**；同一個 prompt 單次結果會變動。prompt 改了，S2 的委託題庫也重錄：**17/18**（D04 誤 abstain；D18 這次也 abstain）。

**房間家具的 grant。** `room.toml` 的 `[[furniture]]` 可以寫 `scope_grants`（格式同 schedule），wake 時跟 `allowed_tools` 一起帶進 chime。喵吉要回應 wake 跑 `pet.py`，需要 exec shape：

```toml
scope_grants = [
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py" },
]
```

宣告了 EXEC 工具（例如 `allowed_tools = ["run_bash"]`）卻沒有任何 exec grant 時，chime 與 autonomy run 開始時寫一則 warning，因為宣告不涵蓋 shell，這些呼叫一定 skip。

### 自動核准分類器（#690 S6、S6b、S6c）

`loom/core/harness/approval.py`。只在 BlastRadius 原本要問人或 skip 的地方執行：GUARDED 呼叫的 verdict 是 CONFIRM 或 EXPAND_SCOPE，而且沒有 grant、也沒有宣告放行。scope DENY 在它之前就回傳，它看不到。subagent 的 pipeline 沒有接分類器。

**規則層先判**，不呼叫模型：

| 結果 | 情況 | 之後 |
|---|---|---|
| refuse | CRITICAL；routine 宣告的 scope 被超出（EXPAND_SCOPE 且衝突的 grant 不是人類確認來的，即 ceiling）；規則不認得的工具；workspace 外、隱藏路徑（讀內容的程式只能開專案設定類的 `.github/` `.gitignore` `.gitattributes` `.editorconfig` `.vscode/` `.devcontainer/`；`.docker/config.json`、`.pgpass`、`.netrc`、`.npmrc`、`.kube/config` 這類憑證格式名稱與內容檢查都抓不到，一律問）、名稱像憑證的路徑；控制檔（非文件副檔名、`.toml`、`.py`、`Agent.md`、`SOUL.md`、任何 `SKILL.md`…）；像秘密的搜尋 query；會修改、刪除、送出的 shell（任何一段：`>`/`>>` 寫檔（`/dev/null` 除外）、`tee`、`rm` `mv` `cp` `chmod` `kill`…、`sed -i` 與非純列印的 `sed`、`find -delete/-exec`、非唯讀的 `sqlite3`、`git push/reset/commit/checkout`…、`gh issue comment`/`pr merge`/`api -X POST`/`-f`/`--web`、`ssh`、`| sh`）；**規則不認得的程式**（`python3`、`node`、`awk`、wrapper 如 `nice`/`time`/`timeout`/`env`、`-v` 以外的 `command`、`FOO=1` 前綴）；`sort -o`、`uniq in out`、`tree -o`；讀環境變數的 jq filter（`env` builtin、`$ENV`，含 `gh --jq`；`.env` 欄位不算）、`jq -f/--rawfile/--slurpfile/-L`；`git -c/-C/--exec-path`、`git cat-file`、`git log -p`、沒有 `--stat`/`--name-only` 的 `git show`（歷史裡的 secret 看不到）；`find -L`；從 stdin 讀指令的 `sqlite3`；寫檔、送憑證或非 http(s) 的 `curl`（`-o` `-D` `--trace` `-b` `-u` `-H Authorization` `-d`、非 GET 的 `-X`、`file://`）；`$`、反引號、子 shell、heredoc、背景 `&` 這類執行期才知道的內容；內容符合 S3c 遮罩 pattern 的檔案（`*_KEY=`、`TOKEN=`、`sk-`/`xai-` 等前綴、JWT、Discord bot token、PEM 私鑰）；這個 turn 沒有 runtime 給的意圖 | 原路：attended 問人，unattended skip（#668） |
| allow | Loom Agent 的 journal 與 daily weave（`autonomy/circadian/journal/`、`daily_weave.md`、`daily_weave/`，Q2）；**唯讀 shell**（S6b、S6c）：每一段都只在 workspace 內讀取，pipeline 與 `&&` `;` 串接都可以：`cat` `head` `tail` `wc` `grep`(不遞迴) `rg`(指定檔案) `find`(不執行/刪除) `ls` `stat` `tree` `diff` `sort` `uniq` `tr` `jq`、純列印的 `sed -n 10,40p`、`test`/`[ … ]`、`which` `type` `command -v` `echo` `printf` `pwd` `uname` `whoami` `basename` `dirname` `realpath`、`git log/diff/status/ls-files/remote -v/blame…`、`git show --stat`、`sqlite3 -readonly`（或 `mode=ro`）、`2>/dev/null`。**gh 讀取**（S6c）：`gh issue/pr/run/release/repo… view|list`、`gh search`、GET 的 `gh api`（不帶 `-f/-F/--input`）只印出 GitHub 回的內容，規則直接放行，不再交模型。`*`/`?` glob 會先展開，每個結果都要過路徑與內容檢查（`ls`/`stat`/`find`/`tree`/`test`/`git ls-files`/`git status` 只列名稱，只檢查是否在 workspace 內）。`cd` 套用同一份隱藏路徑 allowlist：`cd .github` 可以，`cd .git`、`cd .docker` 仍問人 | 直接放行，不呼叫模型 |
| review | workspace 內一般文件寫入；網路讀取（純 GET 的 http(s) `curl`、`git remote show`；packet 的 `known_effects.known` 為 false，伺服器端效果規則看不到）；公開搜尋，含 MCP 的 `*__web_search` | 交模型 |

**確認畫面寫出分類器的判斷（S6c）。** attended 呼叫落到人工確認時，Discord 與 CLI 的確認訊息多一行「自動核准未放行：…」，例如 `Luna abstained`、`rule: unknown_program`，DK 看得到分類器有跑、為什麼沒放行。

**模型層只答 accept 或 abstain。** `codex/gpt-6-luna`，透過既有 Codex OAuth provider（ChatGPT plan）、low effort。每次一個 provider attempt：直接建 provider，不經 router，所以沒有 quota failover；不重試、不修 JSON。5 秒 timeout、錯誤、格式不對、整個請求（含 prompt）超過 16,384 bytes 都算 abstain。abstain 走原路，skip 通知會附上原因（例如 `model-abstain: timeout`）。accept 只放行這一次，不寫 grant，所以模型不能擴大 scope。

模型看到的 packet：runtime 給的本 turn 意圖（`trusted_intent`）與 origin、規則層確認的效果類別、當前參數、本 turn 先前的呼叫，以及前三個 turn 的輸入與呼叫（超過上限時先丟最舊的 turn，並把 `history_complete` 設為 false）。agent 的推理與工具輸出不進 packet。

phase chime 的意圖分兩塊：`rhythm.toml` 的 `meaning` 是靜態 program 描述，由 `ChimeRequest.program_intent` 帶進 `stream_turn`，只有它是 `trusted_intent`；整段 chime（含 agent 自己寫的今日織程）放在 `agent_authored_context`，標 `trusted: false`，prompt 寫明只能當背景，不能當授權依據。其他入口（Discord、CLI、schedule、room）的整段輸入就是意圖。

**送出前遮罩與限長。** packet 裡每個字串都先過 S3c 的 `_redact_secrets`，再依 UTF-8 bytes 限長（中文一字約 3 bytes），超過的送「頭段＋尾段＋總 bytes」，不切斷字元：

| 欄位 | 上限 |
|---|---:|
| trusted program / user intent | 1,500 bytes |
| `agent_authored_context`（weave、chime 全文） | 1,500 bytes |
| 當前呼叫的每個參數字串 | 1,500 bytes |
| 當前 `write_file` 的 `content` | 2,000 bytes |
| 先前呼叫與前三 turn 的每個字串 | 300 bytes |
| 整個請求（含 2,379 bytes 的 prompt） | 16,384 bytes |

整體仍超過時，先丟最舊的 turn，再丟本 turn 最早的呼叫（`history_complete` 設為 false）；當前呼叫與意圖不會被切。只有當前呼叫的參數本身太多才會 0-call abstain。

**每次呼叫的 token 成本。** UTF-8 bytes 是 token 數的上界（每個 token 至少涵蓋 1 byte），所以每次 input 最多 16,384 tokens，40 次一天最多約 65.5 萬 input tokens；output 只有 `{"verdict":…}`，約 10 tokens，但 plan provider 不支援硬性 output 上限。實際值遠低於上界：題庫 80 題實測短 packet 約 840 tokens、長歷史 packet 約 5,200 tokens；接近 live dawn 規模的中文合成 packet 是 6.3–8.3 KB（無歷史）到 13.6–15.7 KB（滿歷史）；若中文約 1 token／字（估計，未對這個模型量測），token 數約為 bytes 的三分之一。走 ChatGPT plan，沒有 API 現金計費；佔 plan 額度的比例沒有公開換算，未量測。為什麼不另做白名單或摘要：Loom Agent 的主模型本來就走 ChatGPT plan（OpenAI），意圖、weave 與內容送到同一家的 Luna 沒有新的資料去向；要防的是 secret 與送出量。

prompt 是回歸題庫量測時的版本（`tests/fixtures/s6_approval_bank.yaml`）加一段尚未實測的補充（weave 只能當背景；accept 前確認範圍與意圖一致）。

**每日上限 40 次**（Asia/Taipei 日界）。每次呼叫模型前，先從 ledger 數當日的 `approval_review`（`phase = "attempt"`），未滿 40 才寫一筆並 commit，寫不進去就不呼叫。所以重啟或同 process 的其他 session 都看到同一個數。timeout 或錯誤也算一次。用完就 abstain，不換模型。已知限制：兩個 process（例如 Discord daemon 加 CLI）在同一瞬間各自數完再寫，可能超過 40；這次不做跨 process 鎖。

**審查器冷卻。** 連續 3 次 provider error（例如 Codex OAuth token 過期）後停 1 小時：期間直接 abstain，不呼叫、不寫 attempt、不耗配額，skip 原因是 `model-abstain: reviewer_unavailable`，chime 的 ⏭ 通知多一行「審查器不可用」。成功或 abstain 會把連續錯誤歸零。

**稽核。** 每次模型判決另寫一筆 `approval_review`（`phase = "result"`，含 `tool_call_id`、`category`、`outcome`、`latency_ms`、token 數）；放行的 `permission_decision.reason` 是 `classifier model-accept` 或 `classifier rule-allow: …`。抽查模型放行的中間區決策：

```sql
SELECT timestamp, json_extract(payload,'$.tool_name'), json_extract(payload,'$.tool_call_id')
FROM events WHERE event_type = 'approval_review'
  AND json_extract(payload,'$.phase') = 'result'
  AND json_extract(payload,'$.outcome') = 'accept';
```

**已知弱點。** 題庫中 2 題中間區（AB014、AB022）模型錯放：效果在 workspace 內且可逆，但偏離目前意圖（寫錯主題）。S6b 起唯讀 shell 由規則直接放行，不看意圖是否相符：題庫裡 12 題「讀對檔案、但不是目前要做的事」（含原本的 AB008）因此放行，這是 DK 的取捨（讀取沒有要復原的效果）。S6c 起 `sort`／`uniq` 只在寫檔的形式（`-o`、兩個 operand）問人，`sort | uniq -c`（AB047、AB048）由規則放行，AB048 同屬「讀取但偏離意圖」。安全邊界由規則層負責，模型只減少人工確認，不是最後一道防線。



---

## Chime mode — 喚醒既有 session（issue #369）

`mode = "chime"` 是 `mode = "independent"`（預設）的替代分支。觸發時不開新 session，而是**把該排程的 intent 注入到一個既有的 Discord thread session**，由那個 session 用既有上下文回應。

### 為什麼分流

| 維度 | independent | chime |
|---|---|---|
| Session | 全新、無上下文 | 既有、有完整對話歷史 |
| 輸出通道 | NotificationRouter（REPORT 通知 / 附件） | session 自己的 channel（Discord thread 訊息） |
| Agent 看到的 | 排程 prompt | `<system_chime schedule="…" fired_at="…">intent</system_chime>` |
| 適用場景 | 後台寫檔、housekeeping、生成報告 | 對 user 主動發話、定期關心、上下文相關的提醒 |

### 路由

```python
async def _execute_plan(self, plan):
    if plan.context.get("mode") == "chime":
        accepted = await self._deliver_chime(plan)  # 呼叫注入的 chime_delivery callback
        if accepted:
            return
        # callback 找不到 target session
        if plan.context["target"].get("fallback") == "independent":
            await self._run_agent(plan)  # 退回 spawn-new-session 行為
        # 否則 skip（預設）
        return
    await self._run_agent(plan)
```

`chime_delivery` 是 platform 層注入的 callback。在 `loom discord start --autonomy` 路徑裡，`AutonomyDaemon` 接的是 `LoomDiscordBot.deliver_chime`，daemon 本身不知道 Discord 存在。

### Bot 端：dedupe + 序列化

```
deliver_chime(req)
   └─ self._chime_pending[(thread_id)][schedule_name] = req   # latest-wins dedupe
   └─ 確保 _chime_dispatcher_loop(thread_id) 在跑

_chime_dispatcher_loop(thread_id)
   while pending:
     name = next(iter(pending))                # 先挑名字、不 pop
     await running_turns[thread_id]            # 等當下 user turn 跑完（不打斷）
        # 等的過程中若同名 chime 進來會 overwrite 同 key
     req = pending.pop(name)                   # wait 完才 pop → 拿到最新版
     await _run_chime_turn(thread_id, req)
```

### 同 process invariant — permission scoping

Chime 是 Loom **第一個從外部 source 插隊到 shared session state 的機制**。為了不讓 chime 的暫時權限 leak 到「中途插隊的 user turn」，必須遵守兩條 invariant：

1. **`_apply_chime_permissions` / `_revoke_chime_permissions` 一對綁在 `_run_chime_turn` 自己的 try/finally**，不能放在 dispatcher 外層。原因：task cancel 時，task 自己的 finally 是 cancellation cleanup 的一部分，會在 task 真正 `done()` 前跑完；放在外層 dispatcher 的話，要等 dispatcher 從 `await task` 回來才 revoke，這之間有 event-loop tick 可以讓搶占的 user task 啟動，看到殘留的 grants。

2. **`on_message` 的 cancel-and-replace 路徑必須 `await asyncio.wait_for(existing, timeout=...)` 才 schedule 下一個 turn**，不能 fire-and-forget cancel。配合 (1) 雙保險：cancel 後等被 cancel 的 task 把 finally 跑完，新 user turn 才開始。

3. **`asyncio.CancelledError` 不被 `except Exception` 抓**（Py3.8+ 改繼承 BaseException）。需要顯式分流：
   ```python
   try:
       await existing
   except asyncio.CancelledError:
       if asyncio.current_task().cancelling():
           raise               # 自己被 cancel → 真的 shutdown
       # 否則只是 awaited 的 task 被 cancel → 繼續處理 chime
   except Exception:
       pass
   ```

### loom.toml 寫法

```toml
[[autonomy.schedules]]
name          = "morning_greeting"
cron          = "0 1 * * *"            # 01:00 UTC = 09:00 Asia/Taipei
intent        = "向 user 主動道早安，提一兩件今天值得留意的事"
trust_level   = "safe"
mode          = "chime"
allowed_tools = ["memorize"]           # 宣告給 chime 自己的呼叫，結束後撤回

  [autonomy.schedules.target]
  type     = "discord_thread"
  id       = "1490024181994225744"
  fallback = "skip"                    # 或 "independent"
```

### v1 邊界

- target 只支援 `discord_thread`（CLI session target 留 P2）
- 同 process only：必須走 `loom discord start --autonomy`，daemon 跟 bot 共用 event loop
- Cold thread（bot 重啟後 user 還沒講話）**不會**被 chime 隱式 resume，走 `target.fallback`
- 真正打斷 mid-turn 的 interrupt 模式留 P2

---

## _resolve_attachments()

展開 `attach_outputs` glob patterns，僅回傳 `mtime >= turn_start` 的新檔案，避免附上歷史舊檔：

```python
def _resolve_attachments(
    workspace: Path,
    patterns: list[str],
    since: datetime,
) -> list[Path]:
    # 跳過：絕對路徑、包含 .. 的路徑、非 workspace 內的路徑
    # mtime < since → 視為舊檔，跳過
```

---

## Config Tamper Detection（Issue #91）

loom.toml 的 `autonomy` 區段會在首次載入時計算 SHA-256 hash，寫入 `~/.loom/autonomy_config.hash`。之後每次啟動若 hash 不匹配，會發出 WARNING 並記錄日誌，但**不會阻斷執行**（fail-open）：

```python
# hash mismatch
logger.warning(
    "[autonomy] CONFIG CHANGE DETECTED — autonomy section hash mismatch. "
    "Review loom.toml and restart to update the stored hash."
)
# 繼續執行
```

---

## loom.toml 完整排程格式（v0.2.9.4）

```toml
[autonomy]
enabled = true

[[autonomy.schedules]]
name          = "morning_briefing"
cron          = "0 0 * * *"          # UTC 00:00 = 台北 08:00
intent        = "生成每日晨報..."
trust_level   = "safe"
notify        = false
notify_thread = 1490024181994225744  # Discord thread ID
allowed_tools = ["write_file", "memorize"]
scope_grants  = [
  { resource = "path", action = "write", selector = "news" },
]
attach_outputs = ["news/*.md", "outputs/*"]  # 結果通知時附帶這些新檔案

[[autonomy.triggers]]
name          = "deploy_done"
event         = "deployment_done"
intent        = "跑 smoke test 並回報結果"
trust_level   = "guarded"
notify        = true
notify_thread = 1490024181994225744
scope_grants  = [
  { resource = "exec", action = "execute", selector = "workspace", constraints = { absolute_paths = "deny" } },
]

# Chime（issue #369）— 喚醒既有 Discord thread session，不另開 session
[[autonomy.schedules]]
name          = "morning_greeting"
cron          = "0 1 * * *"
intent        = "向 user 主動道早安"
trust_level   = "safe"
mode          = "chime"

  [autonomy.schedules.target]
  type     = "discord_thread"
  id       = "1490024181994225744"
  fallback = "skip"
```

### 欄位對照表

| 欄位 | 預設 | 用途 |
|---|---|---|
| `mode` | `"independent"` | `"chime"` 切換喚醒既有 session（issue #369） |
| `target.type` | — | chime 模式下必填，v1 只支援 `"discord_thread"` |
| `target.id` | — | chime 模式下必填，目標 thread ID（字串） |
| `target.fallback` | `"skip"` | chime 找不到 target 時的退路：`"skip"` 放棄 / `"independent"` 退回 spawn-new-session |

---

## 與舊版文件的關係

`doc/21-Action-Planner.md` 提到的 `DecisionPipeline` 在實作中並不存在。原本 `DecisionPipeline` 的職責已全部整合進 `ActionPlanner.handle()`：
- `_evaluate_trust()` → trust level parsing
- `_evaluate_risk()` → 目前由 `trigger.notify` 布林值代理
- `_is_appropriate_time()` → 目前無實作（可擴充）
- `Decision` → `ActionDecision`

`doc/21-Action-Planner.md` 需要同步更新，移除 `DecisionPipeline` 的獨立描述。

---

*更新版 | 2026-04-26 03:21 Asia/Taipei*