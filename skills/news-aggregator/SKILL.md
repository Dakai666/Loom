---
name: news-aggregator
description: 新聞聚合技能。從 28+ 個資訊源抓取即時熱門新聞，並以繁體中文生成深度分析報告。當使用者說「今天有什麼新聞」「晨報」「跑一下新聞」「看看最新消息」「新聞彙整」「每日晨報」或排程觸發時使用。
tags:
  - news
  - research
  - daily-workflow
model_tier: 1
precondition_checks:
  - ref: checks.scripts_only_bash
    applies_to:
      - run_bash
    description: 限制 run_bash 只能在 skills/news-aggregator/scripts/ 目錄內執行，防止脫離新聞抓取範圍。
allows:
  exec:
    - python3 skills/news-aggregator/scripts/fetch_news.py
    - python3 skills/news-aggregator/scripts/archive_news.py
  write:
    - news/current
---

# 新聞聚合技能（News Aggregator Skill）

從 28+ 個資訊源抓取即時熱門新聞，並以繁體中文生成深度分析報告。

---

## 📁 檔案結構

```
Loom/
├── news/                         ← 新聞報告根目錄（歷史日期資料夾原樣保留，不刪資料）
│   ├── current/                  ← 當期晨報工作區（allows.write 邊界，維持近 30 天）
│   │   └── YYYY-MM-DD/           ← 依日期分組
│   │       ├── tech_briefing.md          → 科技晨報（Hacker News + GitHub）
│   │       ├── international_briefing.md → 國際晨報
│   │       ├── medical_briefing.md       → 醫學晨報
│   │       ├── astronomy_briefing.md     → 天文晨報
│   │       └── daily_briefing.md         → 四合一總摘要（含跨域關聯分析）
│   └── archive/                  ← 超過 30 天之自動歸檔區（由 archive_news.py 搬移，絕不刪除）
└── skills/news-aggregator/
    ├── SKILL.md                  ← 本技能定義
    └── scripts/
        ├── fetch_news.py         ← 新聞抓取主腳本
        ├── archive_news.py       ← 舊當期晨報歸檔腳本（news/current → news/archive，只搬不刪）
        ├── rss_parser.py         ← RSS 解析（依賴）+ 來源健康狀態記錄
        ├── opencli_fetch.py      ← CLI 備援抓取
        └── archive-2026-06/      ← 2026-06 的暫存 JSON 殘留（已歸檔，歷史 briefing 仍引用）
```

> **來源健康狀態**：`rss_parser.py` 會記錄每個來源的抓取結果（ok / empty / stale / failed），
> 由 `fetch_news.py --all-tracks` 以 `_source_health` 鍵附在 JSON 裡一起輸出。
> **寫 briefing 的健康狀態表時引用它，不要憑印象寫。**

---

## 🔄 工作流程

### 步驟 1：建立寫入上下文（先後次序重要）

先 `list_dir("news/current/")` 建立父目錄上下文；今日目錄 `news/current/YYYY-MM-DD/` 若已存在，再列出並讀取既有檔案，避免覆寫。
今日目錄尚不存在時，使用 `write_file` 寫第一份正式報告至 `news/current/YYYY-MM-DD/`（工具會建立父目錄），再列出今日目錄確認。
第一次寫新檔前，先對該路徑 `probe_file`，或先 `read_file` 一次（失敗也會標記）；`list_dir` 日期目錄不算對檔案的探測，guard 仍會擋。
不要另下 shell 建目錄；guard 若拒絕就回報，不繞過。

### 步驟 2：抓取（fetch）
```bash
# 四軌一次抓（晨報用）
python3 skills/news-aggregator/scripts/fetch_news.py --all-tracks --limit 8

# 單一軌
python3 skills/news-aggregator/scripts/fetch_news.py --source international --limit 8 --no-save

# 單一來源
python3 skills/news-aggregator/scripts/fetch_news.py --source hackernews --no-save
```

> **`--limit` 是每個來源的配額**：國際、醫學、天文各有三個來源，
> `--all-tracks --limit 8` 每軌最多 24 則；科技為 HN + GitHub，每軌最多 16 則。
> 來源各自取限額，不在合併後截斷；一次抓齊，不需手動補抓。

> **workspace 指令與 guard**：使用 `python3 skills/news-aggregator/scripts/fetch_news.py ...`。
> guard 已接受完整 scripts 路徑，不需切換目錄；guard 不是授權機制。
> 每次 fetch 分別呼叫，不串接、重導向、shell 展開或額外 Python 後處理。
> `--all-tracks` 將 JSON 輸出至 stdout，不自行保存；單源保存目錄從腳本位置解析。
> 驗檔使用 `list_dir`／`read_file`；所需呼叫不在 scope 時回報，不繞過。

### 步驟 3：讀取來源健康狀態（不要靠猜）

`--all-tracks` 的 JSON 裡有一個 `_source_health` 鍵，是**腳本產生的證據**：

```json
"_source_health": [
  {"source": "CNA International", "status": "ok", "detail": "", "item_count": 8},
  {"source": "NASA News", "status": "ok", "detail": "", "item_count": 8},
  {"source": "Astronomy.com", "status": "empty", "detail": "feed parsed but contained zero items", "item_count": 0}
]
```

四種狀態的意義：

| status | 意思 | 該怎麼寫進 briefing |
|---|---|---|
| `ok` | 抓到且有內容 | ✅ 正常 |
| `empty` | feed 能抓，但沒有條目 | ⚠️ 來源空 — 不是壞掉，是沒東西 |
| `stale` | feed 能抓，但最新內容已經舊了（一般 ≥7 天，NEJM ≥14 天） | ⚠️ **feed 已停更** — 這不是新聞延遲，是來源停擺 |
| `failed` | 抓取本身失敗 | ❌ 抓取失敗 — 附上 detail 裡的原因 |

> ⚠️ **不要自己從內容推斷來源健康狀態。** 2026-10-24 之前這張表是手寫的，結果是「stale」與「empty」被混為一談（例如把停更舊聞寫成「feed 無更新」）。**現在腳本分得清楚了，就引用腳本的。**

### 步驟 4：深度整合（integrate）
讀取 JSON 資料後，以下的格式要求是強制性的——
**絲絲的整合想法比原始新聞更有價值**，因此每則新聞都必須包含絲絲自己的深度分析，而非只是轉述。

**寫「來源健康狀態」表時，必須逐列對照 JSON 裡的 `_source_health`，不得憑印象寫。**

### 步驟 5：驗檔與投遞

使用 `list_dir`／`read_file` 確認今日五份 Markdown briefing 已寫入且可讀，
再以 `create_discord_forum_post` 投遞晨報導讀與五份文件。
每日晨報不產生或下載封面圖，附件只包含 briefing 文件。
投遞後用 `read_discord_forum`（list 模式）確認貼文出現在清單。

### 步驟 6：歸檔與收尾
投遞完成後，執行歸檔腳本將 `news/current/` 中超過 30 天的舊日期資料夾搬移至 `news/archive/`（只搬不刪，避免當期目錄累積觸頂 `_SCAN_LIMIT`）：
```bash
python3 skills/news-aggregator/scripts/archive_news.py --keep-days 30
```
接著呼叫 `unload_skill("news-aggregator")` 釋放技能。

---

## 📰 分軌 briefing 格式（每則新聞）

```markdown
#### N. [標題（原文）](https://url)

- **來源**: 來源名 | **熱度**: 🔥 XXX points/stars | **時間**: 時間
- **摘要**: 一句話繁體中文摘要（獨立成段）
- **深度分析**: 💡 背景分析 + 影響範圍 + 趨勢判讀，三者缺一不可
```

---

## 📋 總摘要（daily_briefing）格式

```markdown
## 📰 絲絲晨報 — YYYY-MM-DD

> 四軌並行 · 科技 · 國際 · 醫學 · 天文

---

## 🚀 科技

### 今日最大亮點
一句話描述本軌最重要的新聞及其意義。

### 主要新聞（3-5則）
每則格式同分軌 briefing。

---

## 🌐 國際

[同上]

## 🏥 醫學

[同上]

## 🔭 天文

[同上]

---

## 🔗 跨領域關聯分析

分析四個軌道之間的相互影響，例如：
- 地緣政治 → 科技供應鏈
- 能源政策 → 醫療研究方向
- 天文發現 → 產業技術轉移

---

## 💡 今日金句

> 「引述內容」——從當日新聞提炼出的核心洞察

---

## ⚠️ 來源健康狀態（由腳本產生，不要手寫）

> 這張表**每次跑都從 JSON 的 `_source_health` 讀**，不要從這裡抄。
> 下面的靜態表只是**已知長期問題**的說明，不是現況。

| 狀態 | 意義 | 判斷依據 |
|---|---|---|
| `ok` | 正常 | feed 抓到且有條目，最新內容未達 stale 門檻（一般 7 天；NEJM 14 天） |
| `empty` | 來源空 | feed 能抓但零條目 — 源沒壞，是沒東西 |
| `stale` | **feed 已停更** | feed 能抓但最新內容達門檻（一般 ≥7 天；NEJM ≥14 天） — 這不是新聞延遲，是來源停擺 |
| `failed` | 抓取失敗 | HTTP／Content-Type／連線／DNS／解析錯誤，detail 裡有原因 |

**已知長期問題（狀態需每次現場確認）**：

| 來源 | 長期狀況 |
|------|---------|
| NASA APOD | URL 格式為 `apod.nasa.gov/apod/apYYMMDD.html`，需主動拼接當天日期 |
| NEJM Alerts | feed 偶有數日不更新 |
| Ben's Bites | 受 Cloudflare 保護，需 Playwright |

> **重要**：`stale` 與 `empty` 是不同的故障。過去這兩者都被寫成「⚠️ 部分網路環境問題」，掩蓋了真正的原因。**2026-10-24 起腳本會分辨並直接標出。**

## 🗂️ 可用來源對照表

### 科技新聞

| 金鑰 | 名稱 |
|------|------|
| `hackernews` | 🦄 矽谷熱點（官方 HN API，含討論連結及 UTC 時間） |
| `github` | 🐙 開源趨勢 |
| `36kr` | 🚀 創投快訊 |
| `producthunt` | 🐱 產品獵人 |
| `v2ex` | 🤓 極客社區 |
| `tencent` | 🐧 騰訊科技 |
| `wallstreetcn` | 📈 華爾街見聞 |
| `weibo` | 🔴 微博熱搜 |
| `huggingface` | 🤗 HuggingFace Papers |
| `latentspace_ainews` | 🧪 Latent Space AINews |

### 🌍 國際新聞

| 金鑰 | 名稱 |
|------|------|
| `international` | 🌍 國際聚合（Al Jazeera + BBC + CNA 三合一） |
| `bbc_world` | BBC World RSS |
| `cna_international` | CNA 中央社國際 RSS |
| `al_jazeera` | Al Jazeera RSS |

### 🏥 醫學新聞

| 金鑰 | 名稱 |
|------|------|
| `medical` | 🏥 醫學聚合（NEJM + Nature Medicine + STAT News 三合一） |
| `nejm_alerts` | NEJM Alerts RSS |
| `nature_medicine` | Nature Medicine RSS（https://www.nature.com/nm.rss） |
| `stat_news` | STAT News RSS |

### 🔭 天文新聞

| 金鑰 | 名稱 |
|------|------|
| `astronomy` | 🔭 天文聚合（NASA + ESA + Astronomy.com 三合一） |
| `nasa_news` | NASA News RSS |
| `esa_news` | ESA News RSS |
| `astronomy.com` | Astronomy.com RSS |

### AI 行業內參

| 金鑰 | 名稱 |
|------|------|
| `ai_newsletters` | 🧠 全部 AI 內參聚合 |
| `chinai` | ChinAI |
| `memia` | Memia |
| `bensbites` | Ben's Bites |
| `oneusefulthing` | One Useful Thing |
| `interconnects` | Interconnects |
| `kdnuggets` | KDnuggets |

### Podcast 與 Essays

| 金鑰 | 名稱 |
|------|------|
| `lexfridman` | Lex Fridman Podcast |
| `80000hours` | 80,000 Hours Podcast |
| `latentspace` | Latent Space Podcast |
| `paulgraham` | Paul Graham Essays |
| `waitbutwhy` | Wait But Why |
| `essays` | 📚 全部文章聚合 |

---

## ⚠️ 已知限制

1. **HuggingFace Papers** 需要 Playwright
2. **Ben's Bites** 受 Cloudflare 保護，需要 Playwright
3. `--deep` 模式會增加抓取時間
4. **NASA APOD**：URL 格式已從 `apod.nasa.gov/apod/` 變更為 `apod.nasa.gov/apod/apYYMMDD.html`，
   需要主動拼接當天日期才能抓取；Astronomy.com 可作為穩定的備援來源

---

## 🔐 完整性保護的邊界（2026-10-24 查證）

本 skill 有一條 `precondition_checks` guard（`checks.scripts_only_bash`）限制 `run_bash` 只能在
`scripts/` 內執行。這條 guard 走的是 Loom 的 hash + approval 機制（`~/.loom/skill_hashes/news-aggregator_checks.sha256`）。

**但保護範圍只到 `checks.py`：**

| 檔案 | 完整性檢查 | 說明 |
|------|-----------|------|
| `checks.py` | ✅ SHA-256 + 使用者核准 | 由 `SkillCheckManager.resolve_callable` 執行 |
| `SKILL.md` | ❌ | 開機自動匯入，無簽章無 hash |
| `scripts/fetch_news.py` | ❌ | 直接 `python3` 執行，無完整性檢查 |
| `scripts/rss_parser.py` | ❌ | 同上 |

**所以「skill 的主腳本被改掉」這件事，目前沒有任何機制會發現。** 這是 2026-10-24
skill 信任模型報告（`outputs/doc/skill-trust-model-2026-10-24.md`）的具體延伸。

> 這不代表有問題被利用過——只代表**這個邊界目前是開著的**。

---

## 💡 核心理念

> **「絲絲的整合想法比原始新聞更有價值。」**

每次晨報都是絲絲的學習機會——
新知識在整合的過程中被消化、被記憶、被連結到既有的認知架構中。
因此格式設計刻意保留了「深度分析」與「跨領域關聯」這兩個讓價值真正增值的環節。

---

## 🔍 與其他技能的區別

| 維度 | news-aggregator | deep_researcher |
|------|----------------|-----------------|
| 用途 | 每日晨報、28+ 源快速掃描 | 深度專題研究、單一題目徹底挖掘 |
| 觸發 | 「今天有什麼新聞」「晨報」「排程觸發」 | 「研究這個」「深入分析」「做研究報告」 |
| 輸出 | 四軌 briefing + 總摘要（15-30分鐘） | 結構化專題報告（1-2小時） |
| 來源 | 28+ 個 RSS/API 即時源 | 主動爬蟲 + 文獻 + 多輪搜尋 |
| 適合場景 | 早晨例行、新聞追蹤 | 不熟悉的領域、需要完整調研 |

*本技能由 絲絲・Loom 自動生成，學習並記憶新知識中。*
