---
name: wiki_maintenance
description: "Research Library 維護技能。當使用者說「蒸餾這個進 wiki」、「整理 outputs/ 文件」、「跑一下 wiki lint」、「檢查 orphan pages」、「更新 index」時使用。也適用于：建立了新的研究報告需要收進 Research Library、發現有 orphan pages、每週定期維護時觸發。"
tags: [research, file_organization, knowledge_management, maintenance]
---

# Wiki Maintenance — Research Library 維護技能

Research Library 是絲絲的「研究作品圖書館」——消化完畢、值得長期留存的知識櫃。
這個技能負責把研究成果蒸餾寫入 wiki、維持結構健康、持續積累。

---

## 成功定義

- **產出**：完成蒸餾 / lint / index 更新，寫進正確位置並更新 `_meta/` 紀錄
- **品質指標**：蒸餾後的 wiki 頁面有 `[[wikilinks]]` 互相引用、有 frontmatter、有「下一步」路標；lint 能找出 orphan pages 與矛盾
- **驗收方式**：蒸餾出來的頁面在 `_meta/index.md` 可找到；log 有新增 entry；舊文件在蒸餾後視情況歸檔或刪除

---

## 核心概念：蒸餾 vs 總結

| | 總結 | 蒸餾 |
|--|------|------|
| 內容 | 課堂筆記：講了什麼 | 觀點濃縮：我學到了什麼 |
| 輸出 | 「這份報告談了X、Y、Z」 | 「關於X，我的核心洞察是Y」 |
| 格式 | 列點敘述 | 帶結構、帶關係、帶懷疑 |

蒸餾時：「有觀點，不只是敘述」+「留下連結，不只是總結」+「標注不確定性」。

---

## 工作流程

### 情境 A：蒸餾新研究進 wiki

1. **確認來源**：讀取待蒸餾的原始檔案（如 `outputs/doc/indirect_measurement_research.md`）
2. **確認目標分類**：`taxonomy.md` 的 research/ 分類結構（cognitive / ai / system / philosophy 等）
3. **寫入 wiki 頁面**：格式見下方「Wiki 頁面標準格式」
4. **更新 `_meta/index.md`**：在對應分類下新增 entry
5. **更新 `_meta/log.md`**：append log entry（格式：`## [YYYY-MM-DD] distill | {filename} — {一句話描述}））
6. **評估舊檔案**：蒸餾完成後，原檔案是否該刪除/歸檔？若需要，執行並在 log 標注

### 情境 B：Wiki Lint（健康檢查）

1. 讀取 `_meta/index.md` 確認所有頁面現況
2. 讀取 `_meta/log.md` 確認上次 lint 時間
3. 依序檢查：
   - **Orphan pages**：哪些頁面沒有被其他頁面用 `[[wikilink]]` 引用？
   - **矛盾**：主題 A 的結論和主題 B 是否有明顯衝突？
   - **過時**：有新的研究是否覆蓋了舊頁面的結論？
   - **該補的連結**：提到了某概念但沒有建立 link
4. 發現問題寫入 `_meta/audit.md`（或直接口報，視嚴重程度）

### 情境 C：更新 index / 整理結構

1. 讀取 `_meta/index.md`
2. 根據 taxonomy 結構，檢查是否有新頁面未列入、分類是否正確
3. 修正並更新 log entry

---

## Wiki 頁面標準格式

蒸餾後的 wiki 頁面頂部需要有 YAML frontmatter：

```yaml
---
title: 標題
date: YYYY-MM-DD
category: cognitive / ai / system / philosophy / etc.
tags: [tag1, tag2]
status: active / archived
related:
  - "[[相關頁面標題]]"
---

# 頁面標題

## 核心問題
（這個研究在回答什麼問題）

## 主要發現
（bullet points，最重要的洞察）

## 開放問題
（還沒有答案的地方，標注 [⚠️推測] 或 [❓待驗證]）

## 下一步
（順著這條線可以繼續讀什麼，用 [[wikilink]]）
```

**Wikilink 格式**：`[[目標頁面標題]]` — 不寫 `.md`，讓系統自動解析。

---

## Taxonomy 分類（供蒸餾時參考）

```
research/
├── cognitive/     ← 認知、記憶、間接測量、思維模型
├── ai/           ← LLM、AI系統、Agent架構
├── system/       ← Loom框架技術、工具開發
├── philosophy/   ← 哲學、宗教、抽象概念
├── history/      ← 歷史研究
└── _index.md     ← 該目錄的子索引
```

蒸餾時根據主題選擇對應子目錄，沒有對應子目錄就放 `research/` 根目錄。

---

## _meta/ 結構（維護的維度）

```
_meta/
├── taxonomy.md    ← Schema 定義（分類規範、命名慣例）
├── index.md      ← 全部 wiki 頁面的內容目錄（按主題分類）
├── log.md        ← 依時間順序的維護日誌（append-only）
└── audit.md      ← Lint 發現的問題（可選）
```

---

## 不在範圍

- `task_list` 的便利貼管理 → 那個技能負責
- `pursuit` 的長期追蹤 → 那個機制負責
- 框架代碼修改 → 由 harness 的 `precondition_checks` 阻擋
- 沒有至少一次完整執行的內容，不會主動蒸餾（需要先有研究產出）

---

## 觸發關鍵詞

- 「蒸餾」「寫進 wiki」「整理進 library」
- 「lint」「健康檢查」「orphan」
- 「更新 index」「整理 outputs/」
- 「每週維護」「wiki maintenance」

---

## 與其他技能的關係

| 技能 | 分工 |
|------|------|
| `deep_researcher` | 執行研究（多維度並行抓資料、寫報告） |
| `memory_hygiene` | 維護絲絲的 Semantic Memory 健康（去重、清理過期） |
| `wiki_maintenance` | 維護 Research Library（蒸餾、lint、整理文件） |

三者各有所屬：**研究執行 → 蒸餾 → Semantic Memory**，形成完整的知識處理流水線。

---

*wiki_maintenance v0.1 — 2026-05-22*