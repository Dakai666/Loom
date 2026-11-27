# v0.5.2.0 — 授權收斂 · 效果宣告化 · 外部閘門 🔒🧹🪟

**自 v0.5.1.0 以來 25 個 merge、149 個非 merge commit（連 merge 共 174）。** 這版的主題是**把授權收到三個模式、把工具的效果從寫死的特例改成設定裡的宣告、把外部文字關在牆外**。Loom 已經有一個能自己判斷「這件事要不要問人」的分類器（#690），也有能查記憶、能自己動手的工具；缺的是**每一層只留一個決定點**、**每一個工具的效果都說得出來**。於是這版把散在程式裡的特例搬進設定、把過寬的放行收窄、把不該由外部文字決定的事擋下來。

## 重點

### 🔒 權限模式收斂為 安全／自理／YOLO（#704）

- **單一模式**：權限只剩 安全／自理／YOLO，預設自理（#705）。
- **單一決定點**：所有受管制的呼叫走同一個決定點；`/auto` 與「工具名就算授權」退役；自理的未知操作交給 Luna 判斷（#707）。
- **人工確認只剩「允許這次」**：按下去不留任何 grant、lease 或 auto；每批暫停的 HITL 與 penalty box 退役；連續三次拒絕以 `operator_denials` 結束這一回合（#709）。
- **自理可以委託**：自理的委託開 issue、委託發文不經 shell，也把 Luna 的行為量測下來（#706）。
- **先讀後寫回到契約**：新建檔案不用先讀；覆寫要在本 session 讀過、而且之後沒被改過（比對 sha256），真正寫入那一刻再檢查一次；守門原因統一成一行「守門・<層>：<原因>」，CLI 與 Discord 同一格式，模式常駐顯示（#711）。
- **唯讀的 ledger 視圖**：新增 SAFE 的 `ledger_query`，讓 Loom Agent 查自己的 ledger 時不必再跑 shell——輸出遮罩、每格 2 KB／整份 24 KB 上限、5 秒 deadline；未宣告或查核不過一律 fail closed，而且只有主 session 拿得到（#709）。

### 🤖 自動核准分類器：先規則、再模型（#690）

- 分類器本體落地：規則先判，模型只處理規則沒蓋到的（#698）。
- 唯讀規則放寬、唯讀 shell 不再問 DK、確認畫面看得到分類器的判斷（#703、#699）。
- 工具名永遠不授權 shell（#695）；失敗的呼叫有一致的 `[tool_failure]` 契約（#696）。
- ledger 每個回傳路徑都寫 END、runtime 帶 turn origin 與 fired_at、取消與例外也收尾（#691、#692）。
- autonomy 端：權限名稱會對照工具表檢查、每次 trigger 觸發都留檔、評估器對齊分鐘邊界（#693、#694、#697）。
- DK 手工同意過的技能檢查模組會被記住，不必每次重問（#701）。

### 🧹 技能與排程收斂（#708 S1–S2b）

- **喵吉直切**：家具指向技能，`SKILL.md` 的 allows 就是喚醒邊界（#714）。
- 節律與排程改為引用技能邊界，硬護欄原樣保留（#715）。
- 七項 legacy 活動完成盤點，附 S2b 遷移片段（#716）。

### 🌦️ 天氣、窗戶與外部文字護欄（#708 S3）

- **共用天氣**：原生 `weather` 工具與窗戶家具共用同一份查詢（免 key 的 Open-Meteo）；query 與 peek 都不寫快取、不寫生活狀態，回應有大小上限與整體 deadline（#717）。
- **窗戶只講結構化的事**：「窗外開始下雨。」與「窗外降溫至 X°C」由程式從驗證過的欄位產生，外部自由文字與錯誤內容不會進到訊號裡。
- **外部文字被關在牆外**：外部來源的喚醒只留下第一方唯讀工具（`read_file`／`list_dir`／`load_skill`／`weather`／`journal_append`）與一條有界的 append-only 日誌；policy 缺席、不是第一方實作、或查核時丟例外，一律 fail closed（該呼叫直接被跳過），外部喚醒裡也不能派發子代理（`spawn_agent`）。

### 🔌 MCP 效果宣告化、Plugin／Adapter 退役（#708 S4a–S4b）

- **Python Plugin／Adapter 退役**：`@loom.tool`、`LoomPlugin`、`~/.loom/plugins` 的自動掃描全部移除，MCP 成為唯一的外部擴充通道（#718）。
- **MCP 工具效果改為宣告**：`loom.toml` 的 `[[mcp.servers]]` 用 `tool_effects` 宣告（封閉詞彙 `public_search`／`media_read`、per-server 隔離、遠端 annotations 永不採信、未宣告一律 fail closed、工具**名稱或描述**命中 mutating 關鍵字則拒絕降級）（#718）。
- **舊設定與名稱漂移會說清楚**：loom.toml 還留著已退役的 plugin／adapter 段會印 WARNING；排程與節律表把技能名寫成工具名、殘留已退役的 MCP 名稱，啟動時就印 WARNING；刻意保留的 legacy 訊息降為 INFO，真正的漏遷移仍是 WARNING（#718、#719）。
- **新聞容量有解**：寫入收窄到 `news/current`，新增只搬不刪的安全歸檔腳本（固定父目錄 fd、原子 no-replace rename）（#719）。
- **順手清掉一個空殼**：`RoomState.activity` 這個從沒被寫過的空欄位退役（舊 `room.json` 仍可相容載入），房間日誌的欄位也從 `activity` 改成 `awake`（#718）。

### 🤝 OpenCode Go 訂閱閘道（#712）

- 新增 `opencode-go` provider：OpenAI 相容、只從 `OPENCODE_API_KEY` 認證（錯誤 detail 裡的 key 會被遮掉）、每個請求帶 `loom-agent/<版本>` 的 User-Agent、timeout 180 秒。
- 額度用完時不在原地重試（`max_retries=0`），而是直接交給 Loom 現有的 fallback 機制切走。
- `loom.toml` 可以選它當主模型、當額度備援，或指定給自動核准分類器（`review_model`）；未設定時行為完全不變。

### 🧰 開發者與工具鏈

- 新開的 worktree 自動連結主樹的本地脈絡與共用的 Claude memory（#710）。
- `_archive/648-…` 移出版本控制（#713）。

### ✅ 測試

全套在乾淨 checkout 上是綠的（**5252 passed / 4 skipped**）。這版新增的守門都有紅燈先行：先讀後寫的契約、外部文字護欄、plugin／adapter 退役、MCP 宣告式效果、載入期漂移偵測。

## 升級說明

- **喵吉要先有技能邊界（#714、#717）**：在 `skills/pet-cat/SKILL.md` 加唯一一個 `allows`（沿用 14 個 `python3 skills/pet-cat/pet.py …` 子命令；`allows` 與 `precondition_checks` 同屬既有 `skill_guard`，需 DK 核准），並把 live `autonomy/circadian/room.toml` 的喵吉家具改成 `skill = "pet-cat"`（不能只留手寫 grant），再標 `input_trust = "local"`。省略 `input_trust` 時預設 external，該家具的 exec／write 會被移除，喚醒時她就沒辦法照顧喵吉。
- **節律與排程改引用技能（#715、#716）**：改成 `skills = [...]` 引用技能邊界；刻意保留的 legacy 項目要補 `legacy_reason`，否則載入時仍是 WARNING。遷移片段見 `docs/708-s2-skill-migration.md` 與 `docs/708-s2b-legacy-migration.md`。
- **MCP 工具要補 `tool_effects`（#718，必要的同步步驟）**：在對應的 `[[mcp.servers]]` 補上宣告，**新版 daemon 載入前完成**。沒有宣告的 MCP 工具一律 fail closed；排程與 chime 裡的搜尋會直接被跳過，日常看圖也會退回審查。
- **loom.toml 裡的舊段（#718）**：`[plugins]`／`[adapter]`／`[adapters]` 已退役，會印一行 WARNING 並被忽略，可以直接刪掉。
- **排程與節律表的名稱漂移（#719）**：把技能名寫成工具名、或殘留已退役的 MCP 名稱，載入期會印 WARNING 指出該改用 `skills = [...]` 或 `load_skill`。
- **新聞的路徑要一起換（#719）**：`morning_briefing` 的寫入與附檔路徑改成 `news/current/YYYY-MM-DD/`；`shared_learning` 的 `meaning` 裡**兩處**當日晨報讀取路徑**必須同步**改成 `news/current/YYYY-MM-DD/`（否則 09:00 會找不到 08:00 剛產出的晨報）；`skills/news-aggregator/` 的 `SKILL.md` 與 `checks.py` 受 `skill_guard` 保護、更新後要重新核准。歷史新聞不需要搬或刪。
- **房間日誌的欄位（#718）**：`room-*.jsonl` 的 `activity` 改成 `awake`，下游分析腳本要跟著改。
- **既有資料未受影響**：session、記憶與 ledger 的內容都不變；`review_model` 未設時仍是 `codex/gpt-6-luna`。

— 依 v0.5.1.0 的格式撰寫；內容取自 `v0.5.1.0..master` 的 25 個 merge（#691–#719）。
