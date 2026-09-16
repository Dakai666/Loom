---
name: news-aggregator
description: 新聞聚合技能。從 28+ 個資訊源抓取即時熱門新聞，並以繁體中文生成深度分析報告。當使用者說「今天有什麼新聞」「晨報」「跑一下新聞」「看看最新消息」「新聞彙整」「每日晨報」或排程觸發時使用。
tags:
  - news
  - research
  - daily-workflow
model_tier: 1
precondition_checks:
  - check: scripts_only_bash
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
        └── rss_parser.py         ← RSS 解析（依賴）
```

---

## 🔄 工作流程

### 步驟 1：前置準備（建立寫入上下文）
```bash
# 在任何 write_file 之前，先執行這行建立 news/ 目錄的 Legitimacy Guard 上下文
list_dir("news/")
```
> ⚠️ **重要性**：fetch_news.py 由 run_bash subprocess 執行，無法建立 Loom 的 Legitimacy Guard 上下文。
> 若跳過此步驟，後續 `write_file` 到 `news/YYYY-MM-DD/*.md` 會被 Guard 擋住（看似成功但實際被 block）。

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

### 步驟 3：深度整合（integrate）
讀取 JSON 資料後，以下的格式要求是強制性的——
**絲絲的整合想法比原始新聞更有價值**，因此每則新聞都必須包含絲絲自己的深度分析，而非只是轉述。

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

## ⚠️ 來源健康狀態

| 來源 | 狀態 |
|------|------|
| Hacker News | ✅ 正常 |
| GitHub Trending | ✅ 正常 |
| Reuters World | ⚠️ 部分網路環境 DNS 解析失敗，以 BBC/NHK 為主 |
| BBC World | ✅ 正常 |
| NHK World | ✅ 正常 |
| NEJM Alerts | ✅ 正常 |
| WHO News | ✅ 正常 |
| Medscape Medical | ✅ 正常 |
| NASA News (RSS) | ✅ 正常（NASA News RSS feed） |
| ESA News | ✅ 正常 |
| Astronomy.com | ✅ 正常 |
| NASA APOD | ⚠️ URL 格式已變更（`apod.nasa.gov/apod/apYYMMDD.html`），需更新 fetch 方式 |

---

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