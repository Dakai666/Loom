# #708 S3：共用天氣、窗戶與外部家具邊界

Loom Agent 使用的共用能力是原生 `weather` 工具（`loom/weather.py`），技能是
tracked `skills/weather/SKILL.md`，窗戶是既有 room tick 呼叫的唯讀命令。
沒有新增 provider、plugin、adapter、排程或批准平台。具體少操心的是：旅行與窗戶
不用各自維護 WMO 表、API 錯誤處理與外部文字清理；也不用替窗戶再授予整支 journey.py。

## 副作用與資料契約

本片只唯讀查過主樹 gitignored Python source，沒有讀生活 state／私人 ledger／key，
沒有執行 journey.py、pet.py、fetch_news.py 或 live peek，也沒有呼叫真天氣 API。

| 入口 | 查詢／讀取 | 寫入 |
| --- | --- | --- |
| 舊 journey/weather.py | Open-Meteo hourly、JsonCache.get | cache miss 會 JsonCache.set |
| 舊 engine.now | 在家讀位置；旅途中呼叫 sync；呼叫 weather 與 provider.quota | sync 可推進 clock、抵達 position/status/leg |
| 舊 journey.py now | load_config/load_state、provider/quota | CLI 結尾 save_state；SerpApi quota cache miss 也寫 account cache |
| 新 query_weather／原生 weather | 固定 Open-Meteo GET、10 秒 timeout、最多 256 KiB、不跟 redirect | 無 cache／state／檔案寫入 |
| 新 window 命令／parse | 同一 query + 有限 schema；不讀旅程或房間 state | 無 |
| room_tick | 將新觀察與 RoomState.weather 前值比較 | 原本 room 單一 writer 保存基線、queue、seen、budget、log |
| 人工 journey replacement | 既有 JsonCache.get；未命中呼叫共用 query | 明確 cache.set 只存驗證成功的 snapshot；原 CLI/state 行為保留 |

`now` 仍是有副作用的旅行活動。沒有把它改成 SAFE，也沒有把旅行 state 更新搬進窗戶。
`docs/examples/708-journey-weather.py` 是整份 weather.py 的 tracked 公開替換來源；
不是已套用 live 的宣告。`make_weather(cache, fetch=...)` 回傳的 callable 保留
`(lat, lng, dt, day=False)` 契約，cache 使用 `weather:v2:` 新 key，不誤讀舊 raw API 格式。
命中只讀 cache；未命中 query 與明確 cache.set 分開。失敗不快取、顯示「天氣未知」。

結構化結果：

```json
{"status":"ok","at":"2026-11-27T10:00:00+00:00","latitude":25.03,"longitude":121.56,"code":61,"temperature_c":17,"rain_probability":80}
```

失敗是 `{"status":"unavailable"}`；未知 code、null、NaN、錯誤型別、缺欄位等是
`{"status":"unknown"}`。`at` 必須是有 timezone 的可解析時間；經緯度在合法範圍，
溫度限制 -100..70°C、降雨機率 0..100、code 是明確 WMO integer enum（bool 不算 int）。
不傳 API description／place／HTML／error body；at 重新由 datetime 序列化。
旅行 `day=True` 額外有驗證過的 day.min_c/max_c/rain_max，保留 IANA 當地日摘要；
沒有 IANA 名稱的 fixed offset 使用 UTC。窗戶及原生工具使用 UTC 當小時。

沿既有無 key 的 [Open-Meteo 官方 API](https://open-meteo.com/en/docs)：hourly
temperature_2m、precipitation_probability、weather_code，攝氏預設、ISO 時間與 WMO 表。
使用公開服務須符合[官方使用條件](https://open-meteo.com/en/terms)與 attribution，
非商業公開 endpoint 不需要 key；此 PR 沒有新增或讀取任何 key。

## 窗戶的觸發與保存

選 **normal**：開始降雨或至少降溫 3°C 可以主動叫醒，但仍使用 room 原本的 daily_budget、
min_gap、睡眠規則、排隊、去重；沒有第二套排程。不是每五分鐘報一次天氣。

- 初次觀察只建立基線；開始雨／降溫用程式模板，key 是驗證後的觀察小時。
- room_tick 每次重建 RoomState，重啟後保留前值；相同或較舊時間不產生新訊號。
- 未知／失敗／超過兩小時／未來觀察不產生晴雨訊號；未知會清基線，恢復後先安靜建立基線。
- 同地點才比較；換座標安靜建立新基線。每次成功觀察取代前值，所以降溫比較相鄰觀察，
  不是整天累積降溫計數。
- peek 回傳的 weather snapshot 只經有限欄位建構訊號；一般 external 家具的文字仍可影響
  Loom Agent 的想法，但不能改 provenance、skill 或 permission grants。
- 是否回應／告訴 DK 由 Loom Agent 選擇，沿既有 chime renderer；沒有強制通知或新 DM。

## input_trust 與實際 enforcement

家具只接受 `input_trust = "local" | "external"`，省略為 external；錯拼／非字串整件拒載。
**相容性影響**：原本未標記的家具現在都走 external，其技能的 exec／write grants 會在
load 時過濾，並 WARNING；已知本地喵吉必須明確 local 才保留既有照顧活動。
技能引用本身仍經 S1 嚴格驗證，壞引用／腳本／allows／非法收窄照樣 ERROR 拒載，
不是先過濾掉錯誤再悄悄放行。

沒有在 config load 時假定能取得完整 live registry。採最小 **過濾** 策略：只保留已查
first-party source 的 read_file、list_dir、load_skill、weather、journal_append 名稱，
移除全部技能推導的 EXEC、任意 path write、對外寫／發文與未知工具。
實際呼叫前 middleware 重新查 **真 registry executor 的 code object 身分**、trust、
capabilities 與 resolver；同名假 SAFE／MCP description 都不能取得例外。
不認識的 executor 或被換掉的 resolver 一律 fail closed，回 external_input skip，
不跑模型審查、不詢問無人值守的人、不由宣告或 YOLO 放行。

唯一自記錄例外是既有 journal_append：固定 workspace/autonomy/circadian/journal 的
當日檔名、append-only、最多 4000 字，不接受指定 path／date／覆寫；不授予 memorize 或
write_file。目錄與 leaf 用 dirfd/O_NOFOLLOW；leaf 要求單一 hard link 的 regular file，
O_NONBLOCK 防止 FIFO 阻塞。這不是通用 filesystem grant。journal 的系統報告 caller 也
共用該檔案安全檢查，原本 symlink/hardlink journal 會拒寫，需 DK 人工決定保留方式。

信任鏈是 room config → parse 的 Signal.input_trust → queue 保存 → normal/ambient 合併 →
ChimeRequest.input_trust → daemon 原樣傳遞 → Discord PermissionContext.external_inputs →
BlastRadiusMiddleware → turn finally 撤回。文字裡的 local／skill 等字串不參與這條鏈。
舊 queue 缺 provenance 也按 external；config 改 local 不會把已排隊的 external 訊號升級。
混合 local 喵吉與 external 窗戶（含 ambient）時，**整個 turn 採最嚴共同上限**，不帶
喵吉的 exec/write grants；runtime 還會擋借來的既有 grants／platform declarations。
代價是該混合 turn 不直接照顧喵吉；後續獨立 local wake 仍照原活動邊界運作。

load_skill 本身可先讀，沒有「先有 grant 才能載技能」循環；讀其他技能不建立新 grants，
也不解除 external 上限。原本 skill guard／checks 與 S1/S2 ceilings 保留。有人對話不套
external chime 上限；取消／例外 unwind 撤回，再交回 attended turn，沒有持久升權。

殘餘信任邊界：這是應用程式 enforcement，不是 OS sandbox；安裝的 first-party Python、
registry setup 與 SDK 是可信程式碼。能直接改 Python function/code object、closure、registry
或 OS 檔案的本機程式不在此隔離範圍。未將 metadata 或 prompt 標籤當成安全邊界。
本片沒有做 S4 MCP 特例退役，也沒有宣稱所有 MCP 工具的效果都已可驗證。

## DK 完整人工套用順序

所有以下指令／片段是 **DK／艾芮思操作**，worker 未執行。先完成 review、merge，再處理
下節主樹 news／housekeeping 與安全 fast-forward；如果該步驟停止，不要繼續套用 live。
保留所有既有 guard/checks/metadata；不要重建或覆蓋整棵私人 skills。

1. 在 **Loom repo 外**的獨立備份位置備份要變更的 journey/weather.py、pet SKILL.md、room.toml；備份不含
   .env／secrets。記下當前 revision，方便逐檔復原，不使用整棵 reset。
2. fast-forward 後確認 tracked `loom/weather.py` 與 `skills/weather/SKILL.md` 在位。
   這兩者直接來自 PR，不必 force-add gitignored 私人樹；執行環境須能 import 新版 loom
   （既有 editable install／DK 的正常安裝流程）。不要為了探測而呼叫真 API。
3. **先 script，再 room**：將 `docs/examples/708-journey-weather.py` 的完整內容複製到
   `skills/world-journey/journey/weather.py`。只替換這一檔；保留 journey.py、engine.py、
   JsonCache、state、config、checks.py、precondition_checks 與 S2 allows。原本的
   `from journey.weather import make_weather` 不變；不用在 journey.py 再接第二條權限。
4. weather 技能是 tracked 安裝來源，frontmatter 唯一一份 allows 已含
   `tools: [weather, journal_append]`；不再追加重複 YAML key，也不加 exec/write。
5. 喵吉既有 `[[furniture]]` table 加上或替換唯一一個 key：

   ```toml
   input_trust = "local"
   ```

   `skill = "pet-cat"` 與原本 priorities 保留；S1/S2 的 pet allows／buy 收窄不改。
   未套 S1/S2 者先依對應指南操作；不能藉本片刪 guard 或回到手寫大 grant。
6. 在 room.toml **新增一次**下列家具；若已有 name=window，修改其既有 table，別追加
   同名家具或重複 key。`[furniture.priorities]` 是前一件家具的子表，這裡用新的
   `[[furniture]]` 正確結束它；座標改成 DK 選定的固定窗外位置，不讀旅程 state。

   ```toml
   [[furniture]]
   name = "window"
   label = "窗戶"
   command = ["python3", "-m", "loom.weather", "--latitude", "25.03", "--longitude", "121.56"]
   default_priority = "normal"
   skill = "weather"
   input_trust = "external"
   ```

7. 其餘真正本地家具逐一由 DK 確認後在既有 table 設 local；外部新聞／行事曆等維持
   external。不要全域替換成 local。room budget/gap/priorities 原值保留，不新增 schedule。
8. 依 DK 正常流程檢查 TOML/skills loader、guard hashes；guard 有變更時沿既有批准程序，
   不改 hash 繞過。程式碼更新的生效／daemon 重啟由 DK 在方便時自行安排；本 PR 未重啟。
   套用後的真 API、實際旅程 state、天氣訊號與 daemon 共存仍 **待驗證**。

## 主樹 news dirty／untracked housekeeping：先比對、repo 外備份，再安全 fast-forward

本 PR 唯一納入主樹既有 dirty 的內容是以下 allows；正文、metadata、checks 都原封不動：

```yaml
allows:
  exec:
    - python3 skills/news-aggregator/scripts/fetch_news.py
  write:
    - news
```

另只收錄 `skills/memory-housekeeping/SKILL.md`，與 #716 的
`docs/examples/708-s2b/memory-housekeeping.SKILL.md` 逐 byte 相同。它是可重用規程，
不含私人 state／腳本／key；沿用已 review 的 `allows.write = [outputs/self_check]`，
沒有修改 SAFE 分類或維護工具。收錄後不用 DK 每次另外維護一份安裝副本，來源一致性由
測試釘住。只新增這一檔，沒有 force-add 私人 skills 樹。

worker 只唯讀主樹兩份已授權 SKILL.md／news diff，在自己的 worktree 收錄；未改主樹，
也未處理其他 tmp JSON。news 容量／掃描上限、五項 legacy 啟動 WARNING 降級留 S4，
本片沒有壓訊息，S2 guard 原限制不變。

merge 後在主樹由 DK／艾芮思：

1. `git fetch origin master`；確認 main HEAD 是 origin/master 的祖先、origin/master
   包含本 PR 的最終 head，且目前在預期 master。不要用 force/reset 修不同歷史。
2. 檢查 `git status --short`、`git diff --cached`、`git diff -- skills/news-aggregator/SKILL.md`；
   檢查 `skills/memory-housekeeping/` 是普通目錄、只含普通 SKILL.md，不是 symlink。
   **若有其他 tracked 差異／staged 修改，或 news diff 不是上述唯一 allows，停止這個
   清理流程交 DK 決定；housekeeping 有額外檔案／差異也停止，不搬整個目錄。
   不要 stash/drop/reset/checkout 覆蓋其他內容。** 未追蹤 tmp JSON
   原樣留下，不能加入本 PR 或當成可清垃圾。
3. 選一個新的 **Loom repo 外**備份目錄，例如 `/Users/tsaidakai/loom-migration-backups/708-s3-時間戳`。
   分別備份兩個 SKILL.md（保留原路徑對照與 metadata）、主樹 HEAD、完整 `git status --short`
   與 `git diff --binary -- skills/news-aggregator/SKILL.md`。不要在 repo 內放唯一備份。
   用下列兩個只讀命令輸出到備份目錄中不同名字的 merged 檔：

   ```sh
   git show origin/master:skills/news-aggregator/SKILL.md
   git show origin/master:skills/memory-housekeeping/SKILL.md
   ```

   分別 `cmp` live 與其 merge 目標，兩份必須 **逐 byte 相同**；news dirty 必須僅是上方
   allows，housekeeping 必須同 #716 模板，再人工確認兩份備份可讀。
4. **只在 1–3 全符合且無其他 tracked/staged 改動時**，將以下兩個檔案個別移到剛才
   repo 外備份目錄的新名字（不要覆蓋已有備份，不搬整個 skills 或 housekeeping 目錄）：

   ```text
   skills/news-aggregator/SKILL.md          -> moved-news.SKILL.md
   skills/memory-housekeeping/SKILL.md     -> moved-housekeeping.SKILL.md
   ```

   news 是 tracked：用 `git restore --source=HEAD --worktree -- skills/news-aggregator/SKILL.md`
   只恢復該單檔的舊 HEAD 版本；housekeeping 是 untracked，不執行 restore。
   然後 `git merge --ff-only origin/master`，Git 會放回合併後兩個 tracked 檔案。
   任一步失敗便停下；不強制 checkout/reset、不刪除其他檔案。repo 外 moved 檔與備份都保留，
   由 DK 對照 HEAD 決定逐檔復原，不盲目移回覆蓋新內容。
5. 分別 `cmp` 原 live 備份與合併後兩檔，並檢查 `git status --short`，確認兩檔一致、
   原有未追蹤 tmp JSON 還在。若某檔事前已 clean／已 tracked 合併，只跳過其移開/restore，
   仍要先驗證內容；不對其他 tracked dirty 套用此流程。

## 驗證範圍

合成測試使用 fake transport／stub shell／tmp database 與 tmp filesystem，真 registry、
scope resolver、permission middleware 與 daemon/Discord chime seam。沒有把測試的成功
當成 live API／旅行／發文成功。精確 focused／全套命令與結果由 PR 記錄；review 尚待
艾芮思安排跨引擎複驗，worker 不 merge／approve。
