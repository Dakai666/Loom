---
name: news-aggregator-skill
description: "全網科技/金融/AI 新聞聚合技能。支援 28+ 個資訊源，可抓取並分析即時內容。當使用者要求「每日快報」、「科技新聞」、「財經更新」、「AI 簡報」、「深度分析」時使用。"
---

# 新聞聚合技能（News Aggregator Skill）

從 28 個資訊源抓取即時熱門新聞，並以繁體中文生成深度分析報告。

---

## 📁 檔案結構

```
Project_Next/
├── news/                         ← 新聞報告存放目錄
│   └── YYYY-MM-DD/              ← 依日期分組
│       ├── hackernews.md
│       ├── github.md
│       └── briefing.md           ← 綜合簡報
└── skills/news-aggregator/
    ├── SKILL.md                  ← 本技能定義
    └── scripts/
        ├── fetch_news.py         ← 新聞抓取腳本
        └── rss_parser.py         ← RSS 解析（依賴）
```

---

## 🔄 通用工作流程（3 步驟）

**所有新聞請求都遵循相同流程，無論來源或組合：**

### 步驟 1：抓取資料
```bash
# 請在 Project_Next 根目錄執行
cd skills/news-aggregator/scripts

# 單一來源
python fetch_news.py --source <source_key> --no-save

# 多個來源（逗號分隔）
python fetch_news.py --source hackernews,github,wallstreetcn --no-save

# 全源掃描（廣泛）
python fetch_news.py --source all --limit 15 --no-save

# 關鍵字過濾（自動擴展：「AI」→「AI,LLM,GPT,Claude,Agent,RAG」）
python fetch_news.py --source hackernews --keyword "AI,LLM,GPT" --no-save
```

### 步驟 2：生成報告
讀取輸出的 JSON，並使用**統一報告模板**格式化**每一則**項目。所有內容翻譯為**繁體中文**。

### 步驟 3：儲存與呈現
將報告儲存至 `news/YYYY-MM-DD/<source>_report.md`，然後將完整內容展示給使用者。

---

## 📰 統一報告模板

**所有來源使用同一模板。**根據資料可用性顯示/隱藏可選欄位。

```markdown
#### N. [標題（英文原文）](https://original-url.com)
- **來源**: 來源名 | **時間**: 時間 | **熱度**: 🔥 熱度值
- **連結**: [討論區](hn_url) | [GitHub](gh_url)     ← 仅在資料存在時顯示
- **摘要**: 一句話繁體中文摘要。
- **深度分析**: 💡 **洞察**：背景分析、影響範圍、技術價值。
```

### 各來源適配差異

| 來源 | 適配調整 |
|------|---------|
| **Hacker News** | **必須**包含 `[討論區](hn_url)` 連結 |
| **GitHub** | **必須**包含 `[[GitHub]](url)` 連結 |
| **WallStreetCN** | 標註為「付費牆」如適用 |
| **微博熱搜** | **必須**翻譯為繁體中文標題 |
| **HuggingFace Papers** | **必須**包含 `[[論文]](url)` 和 `[[程式碼]](github_url)` |
| **Newsletter** | **必須**標註作者與發佈日期 |
| **Podcast** | **必須**標註節目名稱與來賓（如有）|

---

## 🗂️ 可用來源對照表

### 核心新聞源

| 金鑰 | 名稱 | 說明 |
|------|------|------|
| `hackernews` | 🦄 矽谷熱點 | Hacker News 首頁 |
| `github` | 🐙 開源趨勢 | GitHub Trending |
| `36kr` | 🚀 創投快訊 | 36Kr 新聞快報 |
| `producthunt` | 🐱 產品獵人 | Product Hunt |
| `v2ex` | 🤓 極客社區 | V2EX 熱門 |
| `tencent` | 🐧 騰訊科技 | 騰訊新聞 |
| `wallstreetcn` | 📈 華爾街見聞 | Wall Street CN |
| `weibo` | 🔴 微博熱搜 | 微博即時熱搜 |
| `huggingface` | 🤗 HuggingFace Papers | 每日論文（需 Playwright）|

### AI 行業內參

| 金鑰 | 名稱 |
|------|------|
| `latentspace_ainews` | 🧪 Latent Space AINews (swyx) |
| `chinai` | ChinAI (Jeffrey Ding) |
| `memia` | Memia (Ben Reid) |
| `bensbites` | Ben's Bites |
| `oneusefulthing` | One Useful Thing (Ethan Mollick) |
| `interconnects` | Interconnects (Nathan Lambert) |
| `ai_newsletters` | 🧠 全部 AI 內參聚合（取前 3 篇）|

### 深度思考與 Podcast

| 金鑰 | 名稱 |
|------|------|
| `paulgraham` | Paul Graham Essays |
| `waitbutwhy` | Wait But Why |
| `jamesclear` | James Clear |
| `farnamstreet` | Farnam Street |
| `scottyoung` | Scott Young |
| `dankoe` | Dan Koe |
| `essays` | 📚 全部文章聚合（取前 3 篇）|
| `lexfridman` | Lex Fridman Podcast |
| `latentspace` | Latent Space (swyx) |
| `80000hours` | 80,000 Hours Podcast |
| `podcasts` | 🎧 全部 Podcast 聚合（取前 3 篇）|

---

## 📋 常見使用情境

### 科技早報
```bash
python fetch_news.py --source hackernews,github,producthunt --limit 10 --no-save
# 輸出至 news/YYYY-MM-DD/hackernews_report.md
```

### 財經快報
```bash
python fetch_news.py --source wallstreetcn,weibo --limit 10 --keyword "經濟,股市,比特幣" --no-save
```

### AI 深度日報
```bash
python fetch_news.py --source hackernews,latentspace_ainews,huggingface --limit 8 --keyword "AI,LLM,Agent,GPT,Claude" --no-save
```

---

## ⚠️ 已知限制

1. **HuggingFace Papers** 需要 Playwright（`pip install playwright && playwright install`）
2. **Ben's Bites** 受 Cloudflare 保護，需要 Playwright
3. `--deep` 模式會增加抓取時間（每篇文章額外請求）
4. 部分來源可能因網站 Anti-bot 機制而失敗，這是正常現象

---

## 💡 Loom 整合提示

- 此技能由 Loom 的 **ProceduralMemory** 管理
- 觸發時，Loom 會呼叫 `run_bash` 執行 `fetch_news.py`
- JSON 結果由 LLM 處理並轉換為繁體中文報告
- 產出建議儲存至 `news/YYYY-MM-DD/` 目錄
