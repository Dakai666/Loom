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
│       └── daily_briefing.md       → 四合一總摘要（含跨領域關聯分析）
└── skills/news-aggregator/
    ├── SKILL.md                  ← 本技能定義
    └── scripts/
        ├── fetch_news.py         ← 新聞抓取主腳本
        └── rss_parser.py         ← RSS 解析（依賴）
```

---

## 🔄 兩步驟工作流程

本技能分兩步驟執行，這是刻意設計——目的是讓 LLM 有完整的資料在手，才能做出有價值的深度整合。

### 步驟 1：抓取（fetch）
```bash
cd skills/news-aggregator/scripts

# 四軌一次抓（晨報用）
python fetch_news.py --all-tracks --limit 8

# 單一軌
python fetch_news.py --source tech --limit 8 --no-save

# 單一來源
python fetch_news.py --source hackernews --no-save
```

### 步驟 2：深度整合（integrate）
讀取 JSON 資料後，以下的格式要求是強制性的——
**絲絲的整合想法比原始新聞更有價值**，因此每則新聞都必須包含絲絲自己的深度分析，而非只是轉述。

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
| Reuters World | ⚠️ 需確認 |
| BBC World | ✅ 正常 |
| NHK World | ✅ 正常 |
| NEJM Alerts | ✅ 正常 |
| WHO News | ✅ 正常 |
| Medscape Medical | ✅ 正常 |
| NASA News | ✅ 正常 |
| ESA News | ✅ 正常 |
| Astronomy.com | ✅ 正常 |

---

*本報告由 絲絲・Loom 自動生成，學習並記憶新知識中。*
```

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

---

## 💡 核心理念

> **「絲絲的整合想法比原始新聞更有價值。」**

每次晨報都是絲絲的學習機會——
新知識在整合的過程中被消化、被記憶、被連結到既有的認知架構中。
因此格式設計刻意保留了「深度分析」與「跨領域關聯」這兩個讓價值真正增值的環節。