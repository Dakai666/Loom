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
---

# 新聞聚合技能（News Aggregator Skill）

從 28+ 個資訊源抓取即時熱門新聞，並以繁體中文生成深度分析報告。

---

## 📁 檔案結構

```
Loom/
├── news/                         ← 新聞報告存放目錄
│   └── YYYY-MM-DD/              ← 依日期分組
│       ├── tech_briefing.md        → 科技晨報（Hacker News + GitHub）
│       ├── international_briefing.md → 國際晨報
│       ├── medical_briefing.md     → 醫學晨報
│       ├── astronomy_briefing.md    → 天文晨報
│       └── daily_briefing.md       → 四合一總摘要（含跨域關聯分析）
└── skills/news-aggregator/
    ├── SKILL.md                  ← 本技能定義
    └── scripts/
        ├── fetch_news.py         ← 新聞抓取主腳本
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

```
1. list_dir("news/")                          # 建立 news/ 的 Legitimacy Guard 上下文
2. run_bash("mkdir -p news/YYYY-MM-DD")       # 創建當日目錄（不走 fetch_news.py）
3. list_dir("news/YYYY-MM-DD/")                # ★ 關鍵：建立日期目錄的上下文
```

> ⚠️ **為什麼步驟 3 不可省略**：`run_bash mkdir` 創建新目錄後，Loom 的 Legitimacy Guard
> 需要該目錄的 `list_dir` 記錄才能允許後續 `write_file` 寫入該目錄內的檔案。
> 只做 `list_dir("news/")` 是不夠的——那建立了 `news/` 的上下文，不是 `news/YYYY-MM-DD/` 的。
> **過去連續 20+ 天的 LEGITIMACY_GUARD blocked 就是因為漏掉了這一步。**

### 步驟 2：抓取（fetch）
```bash
cd skills/news-aggregator/scripts

# 四軌一次抓（晨報用）
python fetch_news.py --all-tracks --limit 8

# 單一軌
python fetch_news.py --source tech --limit 8 --no-save

# 單一來源
python fetch_news.py --source hackernews --no-save
```

### 步驟 2.5：讀取來源健康狀態（不要靠猜）

`--all-tracks` 的 JSON 裡有一個 `_source_health` 鍵，是**腳本產生的證據**：

```json
"_source_health": [
  {"source": "NHK World", "status": "stale", "detail": "newest item is 76 days old (2026-08-08)", "item_count": 3},
  {"source": "NASA News", "status": "ok", "detail": "", "item_count": 8},
  {"source": "Astronomy.com", "status": "empty", "detail": "feed parsed but contained zero items", "item_count": 0}
]
```

四種狀態的意義：

| status | 意思 | 該怎麼寫進 briefing |
|---|---|---|
| `ok` | 抓到且有內容 | ✅ 正常 |
| `empty` | feed 能抓，但沒有條目 | ⚠️ 來源空 — 不是壞掉，是沒東西 |
| `stale` | feed 能抓，但最新內容已經舊了（≥7 天） | ⚠️ **feed 已停更** — 這不是新聞延遲，是來源停擺 |
| `failed` | 抓取本身失敗 | ❌ 抓取失敗 — 附上 detail 裡的原因 |

> ⚠️ **不要自己從內容推斷來源健康狀態。** 2026-10-24 之前這張表是手寫的，結果是「stale」與「empty」被混為一談（例如把 NHK 的 8 月舊聞寫成「feed 無更新」）。**現在腳本分得清楚了，就引用腳本的。**

### 步驟 3：深度整合（integrate）
讀取 JSON 資料後，以下的格式要求是強制性的——
**絲絲的整合想法比原始新聞更有價值**，因此每則新聞都必須包含絲絲自己的深度分析，而非只是轉述。

**寫「來源健康狀態」表時，必須逐列對照 JSON 裡的 `_source_health`，不得憑印象寫。**

### 步驟 4：配圖 + 投遞（2026-10-25 新增 — DK 指定）

**配圖用 minimax MCP 的 `text_to_image`，不要用 `image_generate`。**

```
minimax__text_to_image(
  prompt="<為今日四軌晨報寫的一段封面描述>",
  output_directory="<workspace 絕對路徑>/news/YYYY-MM-DD",
  aspect_ratio="16:9",
)
```

> ⚠️ **為什麼不能用 `image_generate`**：它是 Loom 內建的計費工具，在 autonomy（無人值守）排程下
> 不在既有 scope 內，權限判定會是 `deny: unattended-deny (autonomy, confirm)`，配圖這一步直接失敗。
> **2026-10-25 08:00 實測**：五份 briefing 都產出成功，**唯一被擋的就是 `image_generate`**，
> 結果晨報投遞時沒有配圖。minimax MCP 走 MCP server，不受 Loom scope 攔截。

> ⚠️ **`text_to_image` 只回傳圖片 URL，不會把檔案寫進 `output_directory`**
> （2026-10-25 實測：指定 `news/2026-10-25/` 後，該目錄裡沒有任何圖檔，Desktop 也沒有）。
> **所以必須自己抓回來**：
> ```bash
> curl -sL --max-time 30 -o news/YYYY-MM-DD/cover.jpg "<回傳的 URL>"
> ```
> 抓回來之後，`create_discord_forum_post(..., files=["news/YYYY-MM-DD/cover.jpg"])` 才真的會有配圖。
> **只貼 briefing 檔而不抓圖，等於沒配圖。**

> ⚠️ **落點只有一個：`news/YYYY-MM-DD/cover.jpg`**（跟五份 briefing 同目錄，檔名固定 `cover.jpg`）。
> **不要另開 `outputs/news/`，也不要自取檔名（`quote.jpg` 之類）。**
> 2026-10-26 08:00 實測：圖被抓到 `outputs/news/2026-10-26/quote.jpg`，dawn 只好事後搬回正確位置。
> 下載回來的資料其實是 JPEG，就算 URL 結尾是 `.jpeg`／`.png`，也直接存成 `cover.jpg`，不要事後再改名。

**規則**
- **一天最多一張**，不要為了「多一張比較好」重複呼叫（付費工具）。
- **產圖失敗不影響晨報主體**：五份 briefing 照常寫、照常投遞，配圖只是加分。
- prompt 裡要明寫 `no text, no letters, no numbers, no logos`——產圖模型很容易生出亂碼文字。

### 最後一步：收尾
整合完成後，呼叫 `unload_skill("news-aggregator")` 釋放技能。

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
| `ok` | 正常 | feed 抓到且有條目，最新內容在 7 天內 |
| `empty` | 來源空 | feed 能抓但零條目 — 源沒壞，是沒東西 |
| `stale` | **feed 已停更** | feed 能抓但最新內容 ≥7 天 — 這不是新聞延遲，是來源停擺 |
| `failed` | 抓取失敗 | 連線／DNS／解析錯誤，detail 裡有原因 |

**已知長期問題（狀態需每次現場確認）**：

| 來源 | 長期狀況 |
|------|---------|
| Reuters World | 在部分網路環境 DNS 解析失敗，以 BBC／NHK 為主 |
| NASA APOD | URL 格式為 `apod.nasa.gov/apod/apYYMMDD.html`，需主動拼接當天日期 |
| NEJM Alerts | feed 偶有數日不更新 |
| NHK World | 某些時段回傳舊聞（2026-10-24 實測：最新一則為 76 天前） |
| Ben's Bites | 受 Cloudflare 保護，需 Playwright |

> **重要**：`stale` 與 `empty` 是不同的故障。過去這兩者都被寫成「⚠️ 部分網路環境問題」，掩蓋了真正的原因。**2026-10-24 起腳本會分辨並直接標出。**

## 🗂️ 可用來源對照表

### 科技新聞

| 金鑰 | 名稱 |
|------|------|
| `hackernews` | 🦄 矽谷熱點 |
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
| `international` | 🌍 國際聚合（Reuters + BBC + NHK 三合一） |
| `reuters_world` | Reuters World RSS |
| `bbc_world` | BBC World RSS |
| `nhk_world` | NHK World RSS |

### 🏥 醫學新聞

| 金鑰 | 名稱 |
|------|------|
| `medical` | 🏥 醫學聚合（NEJM + WHO + Medscape 三合一） |
| `nejm_alerts` | NEJM Alerts RSS |
| `who_news` | WHO News RSS |
| `medscape_medical` | Medscape Medical RSS |

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
3. Reuters RSS 在部分網路環境可能 DNS 解析失敗，標記 ⚠️ 並以 BBC/NHK 為主
4. `--deep` 模式會增加抓取時間
5. **NASA APOD**：URL 格式已從 `apod.nasa.gov/apod/` 變更為 `apod.nasa.gov/apod/apYYMMDD.html`，
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
