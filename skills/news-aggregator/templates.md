# 🗞️ 新聞聚合技能 — 快速參照

本技能由 Loom 的 **ProceduralMemory** 管理，可透過提及「新聞」、「AI 簡報」、「科技動態」等關鍵詞觸發。

## 使用方式

```bash
# 請在 extensions/news-aggregator/scripts/ 目錄下執行
cd extensions/news-aggregator/scripts

# 單一來源
python fetch_news.py --source hackernews --no-save

# 多來源（逗號分隔）
python fetch_news.py --source hackernews,github --no-save

# 關鍵字過濾
python fetch_news.py --source hackernews --keyword "AI,LLM,Agent" --no-save

# 全部來源
python fetch_news.py --source all --limit 15 --no-save
```

## 輸出格式

JSON 格式，包含以下欄位：

| 欄位 | 說明 |
|------|------|
| `source` | 來源名稱 |
| `title` | 文章標題 |
| `url` | 原始連結 |
| `hn_url` | Hacker News 討論區連結（僅 HN）|
| `heat` | 熱度（points / stars / replies）|
| `time` | 發布時間 |
| `content` | 文章內容摘要（`--deep` 模式）|

## 快速場景

| 需求 | 指令 |
|------|------|
| 科技早報 | `--source hackernews,github,producthunt` |
| 財經快報 | `--source wallstreetcn,weibo --keyword 經濟,股市` |
| AI 日報 | `--source hackernews,latentspace_ainews --keyword AI,LLM,Agent` |
| 開源動態 | `--source github --limit 10` |
