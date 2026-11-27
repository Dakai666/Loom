# #708 S2：節律與排程引用技能

本片只處理 rhythm anchors、schedules（含同一 registry 的 event triggers）引用技能。工具、技能、家具沿用 #708 定案；不抽天氣工具、不做窗戶或 Plugin/Adapter 清理。所有下列操作均由 DK 手動套用；本 PR 沒改 live/gitignored 設定、skills，也沒重啟 daemon 或執行生活工具。

**S2b 後續**：[七項 legacy 活動遷移與保留](708-s2b-legacy-migration.md) 提供 Tarot exec-only、housekeeping 規程及 weekly/dream CLI 收窄；下文盤點保留 S2 當時狀態，不代表七項均已遷移或 live 已套用。S2b 的 template/片段與裁決以該指南為準。

## 載入契約

```toml
skills = ["pet-cat", "world-journey"]
```

載入器讀 workspace 的 `skills/<name>/SKILL.md`，重用 S1 的 `read_skill_allows`、`allows_permission_fields`、`widening`、`harden`。多技能的 exec/write/tools 取聯集；重複引用不會多給一份 grant。這些是靜態引用，intent、meaning、新聞、今日織程或工具回傳中的文字不參與解析。

`scope_grants` 若與 skills 同時出現，只能收窄聯集，且是**整欄取代**，不是追加。例如只寫一條 buy grant，會同時移除其他預設 exec/write grants。缺少此欄位才使用完整聯集；寫 `[]` 則明確清空該欄。缺技能、缺 allows、缺/越界 script、格式錯誤、scope 擴權或無效 constraints 都在載入當下以 ERROR 拒收整個項目，其他合法項目正常。

`allowed_tools` 是入口既有的**工具使用宣告**，與技能專屬 exec/write scope 分開保留，不要求一般工具重寫進每份 SKILL.md。明確設定時保留該清單；未設定時沿用 allows.tools（如有）的預設宣告。它不會建立 ScopeGrant；列出 run_bash 也不能授權 EXEC；write_file 等 scoped 工具仍經實際 resolver 的效果與 ceiling 判斷。S1 家具仍採原本嚴格子集，沒有因 S2 放寬。

引用 skills 的入口若宣告 `write_file`，**收窄後**的 grants 必須仍有 `path:write`；否則以 `write_file declaration requires an effective path:write grant` ERROR 拒載。工具名稱會 strip 空白，空項拒收；不會藉空白拼法避過檢查。活動中的 exec grant 實際指向的技能腳本也受既有 hard-boundary/skill_guard 路徑保護（包括巢狀腳本），即使 write prefix 涵蓋該檔也不能先覆寫再執行；這只保護該活動的執行檔，不全域禁止一般專案編輯。撤回 grant 後恢復原本 permission_mode；不是 OS 隔離，可信腳本本身的副作用仍不由此隔離。

**分階段相容**：未設定 skills 的舊項目保留既有 scopes/一般工具宣告，並輸出包含項目名稱的 `legacy scope_grants without skills` WARNING。明確引用 skills 後就沒有 fallback：錯誤必須 ERROR 拒收。如此不會因只遷移 dawn/pet/morning 而突然停掉其餘活動；未引用技能的舊項目仍未完成退役，不能宣稱全域手寫 grant 已移除。下面逐項列出。

### 真實權限路徑與一般工具快速放行

1. `loom/platform/discord/bot.py:1023` `_apply_chime_permissions` 將 allowed_tools 以 unattended_only 宣告，scope_grants 建立為 ScopeGrant；turn 後 `:1095` 撤回。schedule 的 independent run 同樣在 `loom/autonomy/daemon.py:460` 套用並撤回。
2. `loom/core/harness/middleware.py:995` 呼叫工具的 scope resolver；`loom/core/harness/permissions.py:263` 依真實效果計算 diff/verdict。這裡**不是所有列名都直接放行**。
3. `loom/core/harness/middleware.py:771` `_declared` 排除 EXEC 與 CRITICAL，並排除 attended turn 繼承 unattended 宣告；`:1119` 存在既有快速放行：非 EXPAND_SCOPE 的一般宣告在三種 mode 都先經 hard boundary，再以 decision_source=program 執行。這是原本 mode 決策點中的宣告路徑，S2 保留它，不能說「安全模式一律再問」。
4. ceiling 按實際 **(resource, action)** 效果區分，不按一般工具名稱猜分類。`hard_boundary` 逐項檢查未覆蓋效果是否有同一 resource/action 的現存 routine ceiling；path:write 寫出 news 外、exec:execute 其他腳本等會拒絕。path:write 不誤擋 image subject_reference 的 path:read；path:read 也不能充當 write grant。未宣告某個效果的 ceiling 不等於新造全域 deny，仍走原本 mode/工具宣告規則。`allows: {}` 不能搭配未受 scope 限制的 write_file 宣告。
5. S2 必要修補：既有 `scope.py:482` 將混合效果中 RESOURCE_TYPE_NEW 優先於 expansion，`permissions.py:269` 因此回 CONFIRM。媒體工具的 path 超界 + 新 network 效果會被當一般 CONFIRM，原 hard boundary 只查 EXPAND_SCOPE，三種 mode 都誤放行。紅燈測試證明此洞；現在 hard boundary 同時檢查 CONFIRM，從真實 request.missing 逐項守住既有 ceiling。未加工具名單、模型分類或新批准機制。

端到端參數測試比較 legacy 與 skills 遷移後，在三個 modes 的 memorize、web_search、fetch_url 都照既有宣告成功；額外 EXEC、path 超界、policy file、CRITICAL、越界文字仍不執行。另以真實 image scope resolver 測 news/cat.png 可用、outputs/cat.png 不可用（path+network 混合效果）。所有結果均來自合成 fixture 和 stub executor。

標準路徑的設定檔以其 workspace 根目錄找 skills，不受呼叫時 cwd 影響。自訂獨立設定檔預設以其 parent 為 workspace，Python caller 可傳 `workspace=Path(...)` 指定。rhythm 每次重載、schedule load/reload 都重新解析；不是執行中途依外部文字加權限。

## DK 套用順序：先技能，再修改既有引用

先在各份既有 SKILL.md frontmatter 的結尾 `---` **之前**加入或替換唯一一個 `allows` key；不要追加另一份 YAML frontmatter。`allows` 和 `precondition_checks` 同屬既有 `skill_guard`，需 DK 套用/同意，沒有新批准機制。保留目前所有 precondition_checks 與 checks.py。

### 1. 技能邊界（只提供片段，沒有改 skills）

`skills/pet-cat/SKILL.md`：沿用 #714 的 14 個子命令；若 S1 已套用，無須再加第二個 allows。

```yaml
allows:
  exec:
    - python3 skills/pet-cat/pet.py status
    - python3 skills/pet-cat/pet.py watch
    - python3 skills/pet-cat/pet.py journal
    - python3 skills/pet-cat/pet.py feed
    - python3 skills/pet-cat/pet.py play
    - python3 skills/pet-cat/pet.py clean
    - python3 skills/pet-cat/pet.py vet
    - python3 skills/pet-cat/pet.py shop
    - python3 skills/pet-cat/pet.py buy
    - python3 skills/pet-cat/pet.py inventory
    - python3 skills/pet-cat/pet.py use
    - python3 skills/pet-cat/pet.py wardrobe
    - python3 skills/pet-cat/pet.py equip
    - python3 skills/pet-cat/pet.py unequip
```

`skills/world-journey/SKILL.md`：S2 最小需求只有 dawn 的 now；未替其他旅程活動預授權更多子命令。現有有人對話仍照 permission_mode 與原 guard 使用技能。

```yaml
allows:
  exec:
    - python3 skills/world-journey/journey.py now
```

`skills/news-aggregator/SKILL.md`：只宣告 fetch script 與 news 報告目錄。這不是廣泛 run_bash/write_file 宣告；檔案寫入仍會經 hard boundary。

```yaml
allows:
  exec:
    - python3 skills/news-aggregator/scripts/fetch_news.py
  write:
    - news
```

**載入限制與處置（不得清理 live 來讓驗證通過）**：`allows.write` 會完整檢查目錄，遞迴總條目數超過 `_SCAN_LIMIT = 2000`，或含 `.DS_Store` 等 hidden_path、secret-looking 路徑、symlink、控制檔時，整個活動當下 ERROR 拒載。例如 `covers news/.DS_Store (hidden_path)` 或 `covers more than 2000 entries; name a narrower directory`。安全掃描策略沒有放鬆。

[克萊兒 R1 review 回報](https://github.com/Dakai666/Loom/pull/715#pullrequestreview-6084081492)（2026-11-27，外部證據、實作者未自行讀取私人 ledger）：晨報近 30 天 tmp 寫入 0 次；tmp 有阻斷條目；news 已有 1325 筆條目，當時通過掃描，但接近 2000，Finder 後續建立 `.DS_Store` 仍會拒載。故片段移除不用的 tmp，不擴權去承接這個目錄。若 news 本身拒載，先保留錯誤與舊設定、暫停這份遷移，交 DK 決定更窄且符合實際輸出位置的邊界/後續處置；**本片不搬移、刪除任何 live 資料，不略過隱藏或可疑檔，也不自動整理目錄**。尚未手動套用 skills 的舊 registry 可維持明示 WARNING 的 legacy 相容，不可把已引用 skills 的 ERROR 靜默 fallback。

### 2. rhythm.toml：修改既有的 dawn / pet table

在**既有** `[[anchors]] name = "dawn"` table 中，保留目前的 `allowed_tools = ["memorize"]`，替換整個 `scope_grants`、**既有 meaning** 並加入 skills，使用下面三個 key（不要新增第二個 dawn table 或重複 meaning）。這保留 dawn 原本會用的 status/watch/play 和 journey now，且不讓 pet-cat 的 buy 從聯集漏進 dawn。

```toml
skills = ["pet-cat", "world-journey"]
scope_grants = [
  { resource = "exec", action = "execute", selector = "python3 skills/world-journey/journey.py now" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py status" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py watch" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py play" },
]
meaning = """
醒來成為今天的絲絲。
現在時間使用本次 <system_chime> 已提供的 fired_at（以 Asia/Taipei 理解），不要另跑 date。
看窗外先載入 world-journey，再用 python3 skills/world-journey/journey.py now；不要加 2>&1、重導向或串接。
不是任務開始，是一天的人際開場——
先 recall 近期記憶（不帶 query、只給 since，回看最近 48 小時）、讀今日織程、跟 DK 說早安。
"""
```

`date` 不是任何技能自己的 script，不能藏入 allows.exec；用既有 chime 的 fired_at 取當次時刻。memorize 等一般工具宣告保留在入口，不搬到 allows.tools，也不因這次遷移被刪除。

須同步改寫舊 meaning 裡「先查現在幾點（不可省）：date ...」的要求，不能只移除 grant 卻留下每天必跑的指令；若原 meaning 還有個人化段落，保留那些段落，只替換時間與看窗外的指示。R1 reviewer 證實 08:30 實際被擋的是 `python3 skills/world-journey/journey.py now 2>&1`。**該指令仍應拒絕**；S2 提供的成功路徑是 plain argv 的 `python3 skills/world-journey/journey.py now`，不是替原 redirect 取得新授權。這項歷史修正來自 reviewer 回報，實作者沒有重播私人 ledger。

在**既有** `[[anchors]] name = "pet"` table 中，保留 `time = ["10:00", "19:00"]`、`allowed_tools = ["memorize"]` 等其他 keys，只替換 scope_grants 並加入 skills：

```toml
skills = ["pet-cat"]
scope_grants = [
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py status" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py watch" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py journal" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py inventory" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py shop" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py feed" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py play" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py clean" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py use" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py buy", constraints = { max_calls = 2 } },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py vet" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py wardrobe" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py equip" },
  { resource = "exec", action = "execute", selector = "python3 skills/pet-cat/pet.py unequip" },
]
```

**buy 的精確限制**：每次 pet phase turn 最多 2 次匹配該 shape 的工具呼叫；第三次不執行。這不是按購買數量、金額、每日總量計費，也不是 script 內部交易隔離。10:00 與 19:00 是各自 turn、各自兩次；新 turn/restart 不保存舊 turn 計數。失敗的執行也可能已消耗一次權限檢查額度，沿用既有消耗機制。`max_calls` 必須是非負整數，字串/負值/bool 拒收。允許的其他既有收窄是 `exact_argv` bool 與 `absolute_paths = "deny"`；未支援或不生效的 constraints 拒收。不要改成只有 `skills = ["pet-cat"]`，那會拿完整 allows（沒有 buy 限次）；重複清單是刻意保留的收窄例外，不是第二份權威。

家具 `skill = "pet-cat"` 沿用 S1；家具的完整 allows 本來沒有 pet anchor 的兩次上限，本片不把 anchor 的額度偷偷擴成全域政策。如果家具也需限次，可用 S1 既有 scope_grants 收窄機制，由 DK 另行決定。

### 3. schedules.toml：修改既有 morning_briefing table

在既有 `[[schedules]] name = "morning_briefing"` table 中，**保留整個既有 allowed_tools 清單**，只刪除原有 scope_grants 整段，在同一 schedule table 加入下列 skills；**若有** `[schedules.target]` 子 table，則必須放在其前。R1 reviewer 回報 live morning_briefing 沒有該子表，不必為此新增 target。

```toml
skills = ["news-aggregator"]
```

保留 cron、intent、mode、target 與其他 keys。不要在 `[schedules.target]` 後面追加 skills，否則它會成為 target.skills，現在會 ERROR 整項拒載（即使上層仍有舊 grants 也不 fallback）。fetch 的額外參數允許；其他 script、shell chains、redirects 不在此 grant。原先晨報列出的媒體、論壇、async 等一般工具宣告原樣保留；S2 不更名或偷偷刪除（名稱漂移屬 S4）。不新增原來沒有的宣告或 grant。媒體輸出同樣受 news path ceiling 與 hard guards 約束。

## 完整盤點：其他既有手寫 grant 與相容處置

盤點來源：本分支起點 `7b5b3ff` 的 tracked examples，以及主樹 gitignored rhythm.toml / schedules.toml（2026-11-27 唯讀；未讀 .env、secrets 或私人 ledger）。表格列出所有帶 scope_grants 的項目；沒有 scopes 的一般宣告項目不受本片相容變更影響。已遷移項目需 DK 依上方片段先套技能再套引用，未遷移項目則**原 keys 原值保留，不需為 S2 造新技能或刪一般工具**。

| 來源 | 項目 | 原 scopes | S2 完整處置 |
|---|---|---|---|
| tracked rhythm | dawn | date、journey now、pet status/watch | skills 聯集 + 原子命令收窄；date 使用 chime 時刻 |
| tracked rhythm | pet | 10 個 pet 子命令，buy 2 次 | skills + 保留原整欄收窄和 buy 2 次 |
| tracked rhythm | curiosity | write autonomy/circadian | 未遷移，原值保留 + WARNING；此範圍覆蓋 policy，不能直接搬進 allows.write |
| tracked schedules | skill_weekly_review | exec workspace、read outputs | 未遷移，原值保留 + WARNING；S1 不表達 CLI / path:read |
| tracked schedules | news_forum_post | write news | 未遷移，原值保留 + WARNING；未藉此片修改論壇排程 |
| tracked triggers | deploy_check | exec workspace | 未遷移，原值保留 + WARNING；沒有自己技能內的限定 script 可引用 |
| live rhythm | dawn | date、journey now、pet status/watch/play | 上方完整 snippet；一般 memorize 宣告保留 |
| live rhythm | shared_learning | write news | 原值保留 + WARNING；可日後由 DK 決定是否引用 news-aggregator，S2 不強制改 |
| live rhythm | pet | 14 個 pet 子命令，buy 2 次 | 上方完整 snippet；memorize 原樣保留 |
| live rhythm | curiosity | write outputs | 原值保留 + WARNING；尚未對應專屬技能 |
| live rhythm | deep_weave | write outputs | 原值保留 + WARNING；尚未對應專屬技能 |
| live schedules | morning_briefing | write news/tmp、exec fetch_news.py | news-aggregator allows 承接 news 與 fetch 兩項，移除不用的 tmp scope；保留全部一般工具宣告 |
| live schedules | daily_mood_tarot | write skills/sisi_mood_tarot、loom/core/personalities；exec draw_and_write.py、date | 原值保留 + WARNING；skill 根目錄與 date 不符合 S1 allows，不能用空 allows 配回舊 scope |
| live schedules | memory_housekeeping | write outputs/self_check | 原值保留 + WARNING；未有對應技能，不要求本片造新文件 |
| live schedules | skill_weekly_review | exec workspace、read outputs | 原值保留 + WARNING；S1 不支援通用 CLI / path:read |
| live schedules | monthly_big_dream | exec workspace、read autonomy/circadian/dreams | 原值保留 + WARNING；同上，未造 wrapper 或改夢流程 |

tracked morning_briefing 原本只有一般 allowed_tools，現在增加 news-aggregator 引用並保留該宣告，新增的專屬 scope 從技能取得。live registry 沒有啟用的 event trigger scopes；註解中的 daily_journal 不算執行項目。

這是**最小相容方案**，不是全域退役完成：無 skills 的舊項目仍能維持原權限，並明示尚未遷移；它們原有廣泛權限的風險沒有被本片修掉。只有明確加入 skills 後，才嚴格改由 allows 作活動 scope 權威，入口只准收窄、錯誤拒收，不能回退 legacy。後續移除相容路徑須先處理表內尚不受 S1 契約表達的 CLI、path:read 與 policy-writing 活動，本片不擴 allows 語言或新增政策平台。**DK 不必為套 S2 同時重做所有舊活動；只需完成 dawn/pet/morning 的上述手動片段。**

## guard 比對與保留決定

真實 middleware/ScopeRequest 測試涵蓋 `&&`、`||`、`;`、pipe、newline、subshell、backtick、redirect、env prefix、其他 script、interpreter options、子命令前綴逃逸、絕對路徑參數。合成 script 不會執行，executor 是 stub；通過代表授權可達，不代表 live 天氣 API 或旅程本身成功。

**不移除任何 checks.py/precondition_checks。** 即使這些 unattended exec shapes 對 shell syntax 已較窄，allows 目前只由活動載入器產生 ceiling，對「有人對話中 load_skill」沒有自動授權/限制作用；因此不能宣稱在所有起點與 restrict_to_journey_script 等價。原 guard 對其他起點的限制仍有用途。也不修改 gitignored SKILL.md 內的既有 `cd ... && ...` 教學；活動執行應使用上述 workspace-relative plain argv。

`load_skill` 本身 SAFE，先載技能不需其 exec grant；測試使用真實 ProceduralMemory 與 load_skill executor，確認載入後 grants/mode 不變，其他技能不能擴本次 ceiling。一般對話不因載入 allows 自動取得 grant；YOLO 無 ceiling 時本來可執行的呼叫依舊由 mode 決定。

## 待驗證

DK 套用後的 live 技能 guard 相容性、完整晨報產出/媒體發佈、旅程位置/天氣 API、daemon 實際排程觸發，以及其他私人活動遷移，均未在本片執行驗證。先完成手動遷移，再由 DK 安排載入新程式；本 PR 不重啟服務。
