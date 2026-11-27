# #717 R2 修正與可重播 RED/GREEN

基準 `ccd83b09f7a0b82c72f93ffe014a9cc7a9237f60`；只 append commits，沒有改寫、squash、
rebase 或 force-push 已 review 歷史。四項 owner 要求與 weather nits 都保留真 RED 測試 commit。
所有下列 pytest 命令前綴為：

```sh
PYTHONPATH=. /Users/tsaidakai/Loom/Loom/.venv/bin/python -m pytest
```

| 項目 | RED commit／命令（接上方前綴） | 實際 RED 原因 | GREEN commit／驗證 |
| --- | --- | --- | --- |
| 1 可信 journal base | `b4fcff3`；`tests/test_s3_r2_journal_708.py -q` | 2 failed/4 passed；journal 與 system report 都在可信祖先 alias 得 NotADirectoryError | `35fe0ae`；該檔 + `tests/test_s3_boundaries_708.py tests/test_external_furniture_708.py tests/test_circadian_journal.py -q`：61 passed |
| 2 真 filesystem closure root | `ace9c80`；`tests/test_s3_r2_closure_708.py -q` | 4 failed/4 passed；真 first-party read/list factory 綁另一 root、缺 cell 仍回 True | `77841c7`；該檔 + `tests/test_external_furniture_708.py tests/test_s3_boundaries_708.py -q`：44 passed |
| 3 queue 單一 trust 交付 | `1e5f6dc`；`tests/test_s3_r2_queue_708.py -q` | 4 failed；mixed intent 包含 local、external ambient 也壓住 local，沒有保留獨立交付 | `e5c7faa`；該檔 + `tests/test_circadian_room.py tests/test_external_furniture_708.py tests/test_skill_allows_708.py -q`：196 passed；後加三 mode 12 組驗證 |
| 4 ChimeRequest 預設 | `83b46f8`；`tests/test_s3_r2_chime_708.py -q` | 1 failed/4 passed；省略 input_trust 是 local | `e1f1dc6`；該檔 + `tests/test_chime.py tests/test_activity_skills_708.py tests/test_activity_skills_708_r2.py tests/test_chime_unattended_668.py tests/test_legacy_activities_708_s2b.py -q`：215 passed |
| weather deadline／大小 | `cd44357`；`tests/test_s3_r2_weather_708.py -q` | 4 failed/1 passed；滴水超時仍 ok、超大回 unknown、工具 deadline/cancel 沒有 kill/reap 邊界 | `c19b984`；該檔 + `tests/test_weather_window_708.py tests/test_s3_boundaries_708.py -q`：40 passed |
| weather 非 JSON 分類 | `6958fce`；同 weather 檔命令 | 1 failed/5 passed；HTML transport 錯誤被當 unknown | `54ace58`；weather 檔 + `tests/test_weather_window_708.py -q`：24 passed |
| weather 真 HTTP close 行為 | `870c732`；`tests/test_s3_r2_weather_http_708.py -q` | 1 failed；本機 HTTP/1.0 最後一塊關 socket 後，再 settimeout 得 EBADF，誤回 unavailable | `41117c7`；該檔 + weather 檔 + `tests/test_weather_window_708.py -q`：25 passed |

測試與邊界：

- journal/system report 共用可信 base 定位，只有 base resolve；內部 autonomy/circadian/journal
  與 leaf 四種 symlink 拒絕，hardlink/FIFO/regular/append/day/body 上限仍回歸。短錯誤不含
  private path/body。明確 custom writer directory 仍屬可信原生設定，不接受 tool args 改路徑。
- closure 使用真正 factory，不以 fake executor 代替 wrong-root 情境；正確 root／可信 alias
  可用，缺 cell／不同 root fail closed。原 trust/capabilities/resolver gates 不變。
- queue 在三 modes 經真 registry、ScopeGrant/resolver/middleware 驗兩回合，shell executor
  stub 不實跑。正常混合先 external，local 留 queue，下一個符合 gap 的 tick 可獨立交付；
  新 external 不能餓死已延後 local。只有 external ambient 時不升級它，純 local 可先交付。
  urgent 優先、原 budget/gap/TTL 不繞過，failure/restart 都保留未交付資料；成功才記 seen。
- source 搜尋 production constructors 僅 daemon schedule、circadian phase、room 三處；前兩個
  明確 local，room 結構推導；省略／unknown external。只修改 operator fixture 的明確 trust，
  通用 routing/format/default fixtures 留安全預設，不批次洗成 local。
- weather transport 是 fake clock／fake socket，HTTP/1.0 個案只連本機 server；native worker
  timeout/cancel 使用 fake process 驗 kill/wait 呼叫。沒有執行天氣 CLI／生活工具或真 API；
  這些測試不等於 live DNS/TLS/OS 回收實測。工具改可 kill/reap 程序，不留無限 IO thread；
  同步 query 每次 read1 限 4096 bytes、remaining timeout、256 KiB 邊界；原生工具總 deadline
  還涵蓋 DNS/header/parse，回收受 OS/event-loop 排程影響，不承諾硬即時。

中途失敗誠實記錄：closure 實作首次曾因函式內 Path import shadowing 得 9 failures，修正
後才 GREEN；Chime 預設改 external 初次有 46 個舊 operator fixtures 失敗，只替已知可信
活動明確 local；queue 初次有 4 個舊測試依賴跨 trust 合併，更新成單 cohort／缺 provenance
不升級。weather 本機 server 實測先發現 EBADF，才新增可重播 RED，而不是把 fake 通過當真 API。

最終 focused（全部 R2 + S1/S2/S2b／mode／room／chime／journal／architecture/name 回歸）
為 **886 passed, 1 known warning (5.43s)**；精確完整命令見 PR #717 的 R2 驗證欄。
最終全套命令 `PYTHONPATH=. /Users/tsaidakai/Loom/Loom/.venv/bin/python -m pytest tests/ -q`：
**5175 passed, 4 skipped, 2 known warnings (112.43s)**。中途第一輪全套為 5166 passed,
4 skipped, 2 warnings (110.58s)，尚未含追加的三 mode queue 與本機 HTTP/1.0 探針；
最後一輪已包含。全套包含 S1/S2/S2b 與原 hard guards。
未改主樹/live、未重啟、未讀 secrets/private ledger/state。喵吉 local 是新版 daemon
載入前、與 merge 同步的必要人工步驟，詳見 [套用指南](708-s3-weather-window.md)。
