# #708 S4b：設定漂移偵測、legacy WARNING 降級與 `news/` 容量最小解法

本文件記錄 Loom `#708 S4b`（清理後半部：項目 4–6）的實作、驗證結果，以及供 DK 手動套用到 live workspace（`/Users/tsaidakai/Loom/Loom/`）的精確替換片段。

> 本分支全程未更動任何 live 檔案、未重啟 daemon、未執行任何生活工具，亦未引入任何 secret 或私人內容。

---

## 一、三項清理讓 DK 與 Loom Agent 少操哪份心

| 項目 | 原本的痛點 | S4b 解法 | 少操哪份心 |
|---|---|---|---|
| **項目 4**：名稱漂移修正＋載入期偵測 | `schedules.toml` 的 `morning_briefing.allowed_tools` 殘留技能名 `async_jobs` 與已退役 MCP 工具 `openai__text_to_image`，只有在 runtime 跑 `check_permission_names()` 才看得到，載入排程／節律表時靜默放行。 | 於 `loom/autonomy/permission_names.py` 建立完整原生工具目錄 `KNOWN_NATIVE_TOOLS`（核心＋條件工具，由契約測試鎖定與所有 `ToolDefinition` 同步）與 `warn_allowed_tool_drift()`，在 `load_schedules()`／`load_rhythm()` 載入期對照原生工具、已設定 MCP server 前綴（自動讀 `<ws>/loom.toml` 或由呼叫端傳入 `mcp_servers`）與 `<ws>/skills/` 技能名發出明確 `WARNING`（不阻斷載入、向後相容）。 | **Loom Agent** 在改排程或節律表後，一載入就能立刻看到「把技能名誤寫進 `allowed_tools`」或「用到已退役／拼錯的工具名」，不用等到隔天早晨才發現權限白寫；**DK** 也不用人工對照工具清冊抓拼字或退役殘留。 |
| **項目 5**：legacy WARNING 降級 | 5 個刻意保留 legacy `scope_grants` 的項目每次載入都刷 `WARNING`，與真正漏遷移的條目混在一起，長期造成告警疲勞。 | 於 `activity_permission_fields()` 新增選填欄位 `legacy_reason`（非空字串）：無 `skills` 且有合法 `legacy_reason` 時把 legacy 訊息降級為 `INFO`（附名稱與理由）；沒寫 `legacy_reason` 的真正漏遷項目維持 `WARNING`；若 `legacy_reason` 非字串／空白、或與 `skills` 同時出現（語意矛盾），以 `ERROR` 拒載該條目且不影響健康同儕。同步更新 tracked `schedules.example.toml` 與 `rhythm.example.toml` 示範。 | **DK 與 Loom Agent** 看開機與重載 log 時，只會在「真的漏遷移」或「設定寫錯」時看到 `WARNING`/`ERROR`，刻意保留的 5 項清楚以 `INFO` 交代理由，不再被固定雜訊淹沒。 |
| **項目 6**：`news/` 容量（1,325 / 2,000）與 Finder `.DS_Store` | live `news/` 已有 225 個日期資料夾、1,325 筆條目（離 `_SCAN_LIMIT = 2000` 剩約 109 天），且一旦用 Finder 打開 `news/` 產生 `.DS_Store`，`skill_allows._check_write` 就會直接拒載 `morning_briefing`。 | **不放寬 `_check_write`、不調大 `_SCAN_LIMIT`、不刪任何歷史新聞**：將 `news-aggregator` 的 `allows.write` 固定收窄為當期工作目錄 `news/current`，並新增極簡安全的 `skills/news-aggregator/scripts/archive_news.py`（只搬不刪，將超過 `--keep-days` 預設 30 天的日期資料夾原子搬移至 `news/archive/`；CLI 不開 `--workspace`/`--src`/`--dst` 旗標、拒隨 symlink、不覆蓋衝突目標），同步更新 tracked `SKILL.md` 與 `checks.py`（放行 `news/current/YYYY-MM-DD` 並相容舊路徑 `news/YYYY-MM-DD`）。 | **DK** 可以隨時用 Finder 瀏覽 `news/` 與歷史晨報而不怕 `.DS_Store` 弄壞隔天晨報，也不必每隔幾個月手動改 `SKILL.md` 邊界或擔心歷史新聞被刪；**Loom Agent** 每天自動維持當期目錄清爽（約 30 天／~180 筆，遠低於 2,000 上限），晨報不會在 109 天後突然停擺。 |

---

## 二、待 DK 手動套用到 Live 的精確片段

> 套用前請先確認目前 PR 已合併或手動同步 tracked `skills/news-aggregator/` 檔案；因 `skills/news-aggregator/SKILL.md` 的 `allows` 與 `checks.py` 受 `skill_guard` / SHA-256 保護，更新後請依既有流程核准 `skill_guard`。**既有 `news/2026-04-03`～`news/2026-11-27` 歷史新聞完全不需移動或刪除。**

### 1. Live `autonomy/schedules.toml` — 項目 4（移除名稱漂移）＋項目 6（晨報寫入路徑改至 `news/current/`）

在既有 `name = "morning_briefing"` 的 `[[schedules]]` table 中：

1. 將 `allowed_tools` 內的 `"async_jobs"` 與 `"openai__text_to_image"` 移除（其餘欄位與合法 MCP 工具不變）：

```toml
# 在 morning_briefing table 中取代原 allowed_tools：
allowed_tools = [
  "write_file",
  "load_skill",
  "web_search",
  "minimax_coding__web_search",
  "fetch_url",
  "send_discord_file",
  "create_discord_forum_post",
  "read_discord_forum",
  "minimax__text_to_audio",
  "minimax__generate_video",
  "minimax__text_to_image",
]
```

> 說明：`async_jobs` 是 workspace 技能名（如需背景工作工具請用 `jobs_list` / `jobs_status` / `jobs_await` / `jobs_cancel` 或 `load_skill("async_jobs")`）；`openai__text_to_image` 所屬的 `openai` MCP server 已不在 `loom.toml` 中。近 30 天 live ledger 實測這兩個名稱呼叫次數皆為 `0`。

2. （配合項目 6）在 `morning_briefing` 的 `intent` 中，將寫入與附檔路徑由 `news/YYYY-MM-DD/...` 改為 `news/current/YYYY-MM-DD/...`，並可在結尾加上自動歸檔指令：

```text
- 寫入與附檔路徑改為 news/current/YYYY-MM-DD/（例如 news/current/YYYY-MM-DD/daily_briefing.md 與 news/current/YYYY-MM-DD/cover.jpg）
- 投遞完成後執行：python3 skills/news-aggregator/scripts/archive_news.py --keep-days 30
```

### 2. Live `autonomy/schedules.toml` — 項目 5（2 筆刻意保留 legacy 標記）

在既有 `skill_weekly_review` 與 `monthly_big_dream` 兩個 `[[schedules]]` table 各自補上一行 `legacy_reason`（不新增第二份 table、不改其他欄位）：

```toml
# 加在 [[schedules]] name = "skill_weekly_review" 內：
legacy_reason = "S1 allows 不表達 loom CLI exec 與 path:read（outputs）"

# 加在 [[schedules]] name = "monthly_big_dream" 內：
legacy_reason = "S1 allows 不表達 loom CLI exec 與 path:read（autonomy/circadian/dreams）"
```

### 3. Live `autonomy/circadian/rhythm.toml` — 項目 5（3 筆刻意保留 legacy 標記）

在既有 `shared_learning`、`curiosity`、`deep_weave` 三個 `[[anchors]]` table 各自補上一行 `legacy_reason`（不改 `meaning`、`time`、`public`、`allowed_tools`、`scope_grants`）：

```toml
# 加在 [[anchors]] name = "shared_learning" 內：
legacy_reason = "接續晨報的開放對話時段，無單一技能腳本；保留既有 news 寫入邊界"

# 加在 [[anchors]] name = "curiosity" 內：
legacy_reason = "開放式好奇心探索時段，無固定技能腳本；保留 outputs 寫入邊界"

# 加在 [[anchors]] name = "deep_weave" 內：
legacy_reason = "深度研究時段，無固定技能腳本；保留 outputs 寫入邊界"
```

> 註：`shared_learning` 的 `meaning` 若要讀今日晨報，可一併把範例路徑從 `news/YYYY-MM-DD/daily_briefing.md` 更新為 `news/current/YYYY-MM-DD/daily_briefing.md`（它既有的 `path:write` selector `news` 仍涵蓋 `news/current`，既有行為不受影響）。

### 4. Live `skills/news-aggregator/` — 項目 6（同步 tracked 更新）

PR 合併拉取至主樹後即具備以下三處更新（若手動套用可對照本分支 diff）：

1. 新增 `skills/news-aggregator/scripts/archive_news.py`
2. 更新 `skills/news-aggregator/checks.py`：`_ALLOWED_OUTSIDE` 接受 `news/(?:current/)?\d{4}-\d{2}-\d{2}`
3. 更新 `skills/news-aggregator/SKILL.md` frontmatter 的 `allows`（與內文當期路徑 `news/current/YYYY-MM-DD/`）：

```yaml
allows:
  exec:
    - python3 skills/news-aggregator/scripts/fetch_news.py
    - python3 skills/news-aggregator/scripts/archive_news.py
  write:
    - news/current
```

---

## 三、本機驗證與近 30 天 Live Ledger 重播摘要

### 1. Live 設定檔載入 log 對照（唯讀讀取，零修改）

- **套用 S4b 片段前（live as-is）**：
  - `schedules.toml`：載入 5 筆，`4 WARNING`（2 筆漂移：`async_jobs` 技能名誤植、`openai__text_to_image` 未設定 MCP server；2 筆 legacy：`skill_weekly_review`、`monthly_big_dream`），**零誤報**。
  - `rhythm.toml`：載入 8 筆（含 `pet` 雙時段），`3 WARNING`（3 筆 legacy：`shared_learning`、`curiosity`、`deep_weave`），**零誤報**。
- **在隔離 `tmp` 副本套用全部 S4b 片段後（含模擬 1,325+ 歷史 `news/` 條目與 `news/.DS_Store`）**：
  - `schedules.toml`：載入 5 筆，**`0 WARNING`**、`2 INFO`（`skill_weekly_review`、`monthly_big_dream` 附理由）。
  - `rhythm.toml`：載入 8 筆，**`0 WARNING`**、`3 INFO`（`shared_learning`、`curiosity`、`deep_weave` 附理由）。
  - `read_skill_allows("news-aggregator")` 通過，寫入邊界固定為 `news/current`，完全不受 `news/` 歷史檔與 `news/.DS_Store` 影響；同時測試證實 `news/current/.DS_Store`、symlink 與 >2,000 條目仍被嚴格拒載（`_SCAN_LIMIT = 2000` 未放寬）。

### 2. 近 30 天 Live Ledger 重播（唯讀副本 `?mode=ro`）

- 涵蓋範圍：近 30 天共 **3,415** 筆獨立工具呼叫（`tool_lifecycle`）、**3,386** 筆權限裁決（`permission_decision`）、**1,242** 筆排程／觸發紀錄（`trigger_fire`），含 `autonomy` 111 筆與 `chime` 128 筆權限裁決。
- `async_jobs` 與 `openai__text_to_image` 近 30 天呼叫次數：**0 次**；移除後不影響任何實際工具呼叫。
- `master`（`353271d`）與 S4b as-is 對 5 筆排程與 8 筆節律錨點之 `allowed_tools` 與 `scope_grants` **100% 一致**，既有排程、chime、autonomy 合法操作無任何退步。

### 3. 全套測試與隔離檢查

- `PYTHONPATH=. /Users/tsaidakai/Loom/Loom/.venv/bin/python -m pytest tests/ -q`：**5227 passed, 4 skipped, 2 warnings**（含 `tests/test_suite_isolation.py` 全套 `$HOME/.loom` 隔離驗證）。
- `skills/news-aggregator/tests/test_checks.py`：全數通過。
- Live `/Users/tsaidakai/Loom/Loom/` 與 `~/.loom/` 相關設定／雜湊檔 mtime 驗證：**零變動**。

Refs #708
