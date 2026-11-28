# #708 S2b：剩餘七項活動的遷移與保留

**2026-11-28 後續契約**：本頁保留 S2b 歷史脈絡。無 skills 的 rhythm anchor 已定案為正式[開放時段](open-slots.md)，不再保留自己的 allowed_tools／scope_grants，也不需 legacy_reason；以下關於 rhythm 的 legacy 保留／待遷移敘述由新契約取代。`docs/examples/708-s2b/rhythm.toml` 已同步移除這些欄位。Schedule／trigger 的授權語義不變，legacy 訊息改為同 process、同來源一次；merge 後的 live rhythm 遷移由艾芮思執行。

本片由蘇菲亞 · Codex 實作，對象是 Loom Agent 的七項既有活動。基準為 master 的 #715 merge `57902d5`。交付是 tracked 安全技能模板、可解析的 TOML 替換參考與合成測試；**沒有套用 live**、改私人技能、搬人格、重啟 daemon 或執行生活 CLI/API。不包含 weather、window、input_trust 的 S3。

## 七項 outcome

下表描述本 PR 的交付結果，不是 live 遷移完成證明。**2 項提供技能遷移、3 項保留原一般工具與 legacy 寫入 scope、2 項仍為契約不支援的 legacy CLI（另收窄 exec）**；沒有宣稱七項都完成技能遷移。

| 活動 | 真實用途與效果 | 本片 outcome | 保留／改變的權限 |
|---|---|---|---|
| daily_mood_tarot | 自有腳本按日期抽牌／特殊日期套 mood，先備份，再寫人格與 JSON log | **技能遷移模板已提供，待人工套用**；live 沒有 SKILL.md，不能假設已存在 | DK 選 exec-only：allows.exec 唯一 `python3 skills/sisi_mood_tarot/draw_and_write.py`，無 allows.write/tools；skills 引用、allowed_tools=[]；撤除舊 root／錯位人格 path grants 與 date |
| shared_learning | 從既有晨報分享段接續聊天，必要才搜尋／讀原文；不重跑晨報 | **一般工具與 legacy write 保留**，未遷移 | 原 fetch/search/memorize 宣告及 path:write `news` 原樣保留；不是「只有一般 tools」 |
| curiosity | 依 program 自主探索，可能搜尋、子代理、存記憶、寫探索成果 | **一般工具與 legacy write 保留**，未遷移 | 原一般工具宣告及 path:write `outputs` 原樣保留 |
| deep_weave | 依 program 深入一個問題，成果可為 note/draft，也可不產出 | **一般工具與 legacy write 保留**，未遷移 | 原一般工具宣告及 path:write `outputs` 原樣保留 |
| memory_housekeeping | 先 census／快照，再 preview/prune，最後維護週報 | **技能遷移模板已提供，待人工套用**；有真實操作順序與報告位置 | allows.write 僅 `outputs/self_check`，無 exec/tools；入口原 allowed_tools=[write_file] 保留，舊 path grant 由技能承接 |
| skill_weekly_review | CLI 查 ledger、掃 workspace／~/.loom 技能結構，內部寫週報，再 read_file 回報 | **契約不支援，仍 legacy；exec 收窄** | S1 不表達 `loom` CLI／path:read；保留 read `outputs` 及一般宣告，workspace exec 改為 exact `loom skill weekly` |
| monthly_big_dream | CLI 讀設定／DB，全庫整理、模型與 embedding 呼叫、備份／DB 寫入／夢報告；有內建 execute gates | **契約不支援，仍 legacy；exec 收窄** | 保留 read `autonomy/circadian/dreams`；workspace exec 改 exact `loom memory big-dream --execute --yes`，timeout=10800 規程不變 |

三個研究錨點沒有觀察到必須另立自有腳本或固定多步產出的規程。共享讀取、搜尋、記憶與可選研究成果，不構成必須造一份技能的理由；也不引用 news-aggregator 偷增 fetch script 執行權。既有 write scopes 有實際輸出用途，保留能避免 unattended 行為被默默切斷。`outputs` 仍較廣、legacy WARNING 仍出現，這個風險沒有因盤點而消失。memorize 會寫記憶、fetch/search 有 network、spawn_agent 的效果依工具與子代理邊界判斷，**不按名稱一律說 SAFE**。

weekly/dream 的 CLI 確有副作用，但不是「技能自己目錄下的 script」；不能把 `loom` 塞入 allows.exec、不能用 allows.tools=[run_bash] 代替，也不造 wrapper 來繞過契約。既有 `skills/memory_big_dream/SKILL.md` 描述一般有人對話的彩排流程，並沒有可供 S1 引用的自有 script allows；本片不修改它，也不把它當作這個已獲 DK 直接寫入裁決的 schedule 新工作流程。

## 套用順序與精確替換範圍

所有 live 操作由 DK 另行套用，本 PR 沒執行。`allows` 和 `precondition_checks` 同屬既有 skill_guard；沒有新增批准平台。已有 guards/checks 一律保留。

1. 先建立／核對技能，再改引用。
   - 將 [Tarot 安全模板](examples/708-s2b/sisi_mood_tarot.SKILL.md) 建立為 `skills/sisi_mood_tarot/SKILL.md`。現場唯讀核對此檔不存在；**只新增這份文件，不複製或 force-add 私人技能樹**，原 scripts/moods/log/backups 不動。若套用前已由他人建立，先合併既有規程，保留 precondition_checks，只留唯一 frontmatter 和 allows。
   - 將 [housekeeping 安全模板](examples/708-s2b/memory-housekeeping.SKILL.md) 建立為 `skills/memory-housekeeping/SKILL.md`。不用新增 script；規程使用現有 memory_health、memory_prune、write_file。
2. [四個 schedule 替換參考](examples/708-s2b/schedules.toml) 直接由測試消費。**在既有同名 table 取代 keys，不追加第二份 table／intent／scope_grants。** 保留各自 cron、notify、notify_thread、mode、target、attach_outputs 與未列出的 keys；檔案中的這些值只描述已盤點的參考值，不要求把個人 routing 換成它。若有 `[schedules.target]`，skills 放在其前。
   - daily_mood_tarot：替換 intent；新增 `skills=["sisi_mood_tarot"]`；把 allowed_tools 改成 `[]`；**刪整段舊 scope_grants**，不寫 `scope_grants=[]`（後者會連技能 exec 也清掉）。不再使用 date、前置 ls/test/cd 或額外備份 shell。
   - memory_housekeeping：替換 intent 為先 load_skill／按規程執行；新增 `skills=["memory-housekeeping"]`；保留原 allowed_tools=[write_file]；刪整段舊 scope_grants，由技能提供同一條 `outputs/self_check` 寫入 scope。
   - skill_weekly_review／monthly_big_dream：替換 intent 的 plain CLI 用法與整段 scope_grants，**不加 skills**。path:read 原值保留，exec 用下列 exact shapes。
3. 三個研究錨點**不需套用改動**。[rhythm 保留參考](examples/708-s2b/rhythm.toml) 是可載入／測試的權限摘錄；meaning 是摘要，不是 DK 個人全文的替換片段。保留 live 原 meaning、program 指示、time/public 等全部欄位與原 tools/write scopes；不要以這份摘錄覆蓋 live rhythm。

這裡沒有新 `loom.toml` key 或 runtime schema，亦未改原 S2 的 dawn/pet/morning 邊界、buy 每 turn 限次與 guard。tracked `autonomy/schedules.example.toml` 同步 housekeeping 引用與 weekly 收窄；Tarot/dream 的完整新增參考集中在上述片段，不往一般 example 自動加私人活動。

### CLI 收窄是行為改變，仍不是技能遷移

```toml
# skill_weekly_review：整欄替換，保留 read scope
scope_grants = [
  { resource = "exec", action = "execute", selector = "loom skill weekly", constraints = { exact_argv = true, absolute_paths = "deny" } },
  { resource = "path", action = "read", selector = "outputs" },
]

# monthly_big_dream：位於另一個既有 table，勿在同一 table 重複 scope_grants
scope_grants = [
  { resource = "exec", action = "execute", selector = "loom memory big-dream --execute --yes", constraints = { exact_argv = true, absolute_paths = "deny" } },
  { resource = "path", action = "read", selector = "autonomy/circadian/dreams" },
]
```

原 workspace exec 甚至可以包含其他 CLI／shell chain；現在只覆蓋上列 argv，額外 flags（含 --db、--batch、--no-write）、重排 flags、少 --execute/--yes、date、redirect、pipe、cd/前置 shell、其他命令一律不由此 grant 覆蓋。這保留盤點到的原指令形狀，**不代表證明所有未來模型會選正確形狀**。read scope 不授權原生 write；CLI 自己寫週報／DB／backups／夢報告仍是可信內部效果。

`run_bash` 的 `timeout=10800` 是 tool args，測試確認原樣到達 stub executor；**exact_argv 不約束 timeout**，也沒有實跑三小時、驗證子程序逾時或完成大夢。big-dream 仍按 DK 2026-11-19 裁決直接 `--execute --yes`，每階段備份、自審、loom.toml execute 開關照舊；不改成彩排／另一流程。失敗回報階段、不重試，保持原裁決。

## Tarot 真實路徑、備份與 B4

只讀主樹 `skills/sisi_mood_tarot/draw_and_write.py`、支援 Python 邏輯與路徑 metadata，**沒有讀私人人格、mood 範本內容、JSON log、備份或 special_dates 的內容**。source 的寫入是：

| 位置 | 實際行為 |
|---|---|
| `personalities/personality_sisi_tarot_mood.md` | 模板讀取、備份成功之後覆寫；舊 grant `loom/core/personalities` 目錄不存在，不能把它說成真實目的地 |
| `skills/sisi_mood_tarot/log/draw_log.json` | 讀舊 JSON、以當日 key 更新並重寫；舊 log 讀取／解析例外會從空 dict 建立，這是既有行為，沒有順手修腳本 |
| `skills/sisi_mood_tarot/backups/<date>/<原檔名>` | 先於人格/log 更新，只有原檔存在才備份；exclusive create 保留同日首份、不覆蓋，備份錯誤在更新前中止；不手動補 shell 備份 |
| 技能內 `__pycache__`（視 Python 環境） | 匯入支援模組可能建立 bytecode；exec shape 不是內部寫入隔離 |

寫人格和 log 不是 transaction；後段失敗可能已寫人格，所以規程要求照實回報階段、不自動重試。腳本與所有內部備份程式碼、檔案位置、guard 均不修改。

真實 metadata：技能 root、log、backups、personalities 與目標人格檔存在，非 symlink；SKILL.md 不存在；log 有 `.gitkeep`。以 59 個路徑 metadata 重建**全部 synthetic 內容**，用真正 `read_skill_allows` 驗證：

| allows.write 候選 | 真 loader 結果與意義 |
|---|---|
| `skills/sisi_mood_tarot` | 拒絕技能 root／guard 範圍，不能因此開放 script／SKILL.md 覆寫 |
| `skills/sisi_mood_tarot/log` | 因現存 `.gitkeep (hidden_path)` 拒載；不刪／忽略隱藏檔來通過 |
| `skills/sisi_mood_tarot/backups` | metadata clone 通過；加入控制檔／symlink 的合成反例仍拒載 |
| `personalities` | metadata clone 通過；這不是路徑本身被 B4 禁止，也不代表應授權整個人格目錄 |
| `loom/core/personalities` | 路徑本身通過，不存在也不會自動拒載；它仍是錯位 grant，不能靠 loader 接受當作正確 |

另用真 loader 測既有目錄中的 AGENTS.md／loom.toml／hidden／symlink 仍拒；與乾淨同名路徑本身可載入明確區分。DK 已透過 coordinator 選 **exec-only**，因此沒有任何 allows.write，不需要搬人格／刪 .gitkeep／放寬 B4／改 #702 儲存模型。撤除原生 write_file/path grants/date 是**有意、已裁決的收窄**；plain 腳本的內部三處寫入仍可進行，不依賴 path grant。沒有 OS 隔離、也不代表所有 mode 下新增了「全面禁止原生寫入」政策；撤除的是本活動的預授權，其他工具依原 mode/guards 判斷。

Tarot allows.exec 仍是既有 argv **prefix** 契約，會給 `absolute_paths="deny"`，沒有擴語言。規程要求不加參數；不可宣稱它已變成 exact_argv 或檔案隔離。執行中的活動 script 與 guarded frontmatter 仍受 skill_guard 保護。

## Housekeeping 的資料副作用與限制

source：`loom/platform/cli/tools.py::make_memory_health_tool` 的 corpus view 會 `save_snapshot`；`loom/core/memory/maintenance.py::make_memory_prune_tool` 呼叫 `SemanticMemory.prune_decayed`，其 lifecycle 會 archive，且可永久刪除已封存衰減資料。兩工具現有 trust 是 SAFE；**SAFE 不等於唯讀**。本片沒有更改工具安全分類、DB 維護政策或預設 threshold；這項既有風險應另案評估，不能把模板當成修復。

舊 intent 要 archived/deleted 分拆，但目前工具輸出只有 examined/pruned/retained。模板改為記實際回傳、有缺就明說，避免編造或為補數字另查私人資料。工具內部 DB 效果不由 `outputs/self_check` 限制；此 scope 只限制原生報告寫入。

該目錄 metadata 唯讀核對為現存非 symlink、101 條目、未見 B4 控制／hidden／symlink 阻斷項（2026-11-27 當下），沒有讀報告內容。套用時仍由 loader 重掃；未來增控制檔、隱藏檔或超過 2000 條目時會 ERROR。不得為讓載入成功去整理 live 資料。

## 驗證範圍

測試消費本 PR 真模板與 TOML：schedule loader→真 planner／daemon `_run_agent` 宣告、grant、撤回→真 resolver／middleware；anchor loader→phase chime→bot 權限→真 resolver／middleware。executor／模型為 stub，script 絕不執行。另用真 ProceduralMemory/load_skill 證實先載技能可行，沒有新增 grant／改 mode。SAFE/self/YOLO、autonomy/chime、合法腳本／原 CLI、shell 前置與串接、原生寫 scope 越界、活動 script 覆寫、控制/hidden/symlink 載入均有合成覆蓋。

沒有 live 排程觸發、真抽牌／清記憶／weekly／big-dream、模型選指令品質、完整備份／還原、API 或 daemon 重啟驗證。私人腳本副作用來自 source 核對，不冒充合成 stub 已執行效果。

GitNexus MCP 本 session 未提供；CLI 的索引是指向主樹的 symlink。`analyze` 回 Already up to date，但 query 仍警告 stale／read-only FTS、回空 flow，新增測試 impact UNKNOWN，不能把它當無影響證據。後以 `gitnexus index <本 worktree>` 僅註冊此路徑，沒有重建／改主樹索引；本 worktree 的 staged detect-changes 回報 9 files、2 個文件標題符號、0 affected processes、LOW。新測試未被舊索引收錄，故仍以 diff 與實際測試補查，沒有改 production symbols。測試指令與最終結果見 PR，跨引擎 review 由 coordinator 安排。

### 本機驗證紀錄

環境沿用既有 `/Users/tsaidakai/Loom/Loom/.venv/bin/python`（未安裝／更改 shared venv），一律 `PYTHONPATH=.`：

```text
python -m pytest tests/test_legacy_activities_708_s2b.py -q
# 49 passed, 1 warning, 0.49s
python -m pytest tests/test_legacy_activities_708_s2b.py tests/test_activity_skills_708_r2.py tests/test_activity_skills_708.py tests/test_skill_allows_708.py tests/test_single_decision_704_s3.py tests/test_permission_mode_704.py tests/test_unknown_ops_704_s3b.py -q
# 667 passed, 1 warning, 3.46s
python -m pytest tests/ -q
# 5082 passed, 4 skipped, 2 warnings, 111.09s
```

上面 `python` 代表該完整 venv 路徑。全套含隔離 HOME 的整套重播，沒有測試失敗；warnings 為既有 audioop 與 Starlette/httpx deprecation。四份 TOML（含兩份 tracked examples）、兩份 YAML frontmatter 與 synthetic loader JSON roundtrip 都通過，`git diff --check` 通過。

RED 與中途測試設定錯誤如實保留：初次 18 fail、補 Tarot/B4 後 34 fail，均因交付模板缺少；補模板後 12 fail（6 個錯用不存在的 is_declared API，6 個把 outputs 下未來新建 SKILL.md 誤當現有 runtime skill_guard 的保護對象）。改用真 declared_by API，runtime 斷言聚焦實際技能 script/frontmatter、policy/hidden/ceiling，既有目錄控制檔則留給真 loader 測試，不擴 production guard。接線新增測試初次 9 fail 因 rhythm 摘錄缺少；補完後 49 pass。其後強化 daemon 結果斷言，另捕獲 4 個 fixture 缺 session.workspace 的失敗，補正 fixture 後最終 49/667/5082 都綠。早期失敗有 async ledger teardown 的 closed/no active connection 次生 log，不當作產品 regression 或隱藏實際失敗斷言。

Refs #708
