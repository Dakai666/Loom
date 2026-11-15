---
name: wiki_maintenance
description: "Research Library 維護技能——把 wiki 當作 semantic memory 的詳細附件來維護（不是獨立圖書館）。當使用者說「蒸餾這個進 wiki」、「整理 outputs/ 文件」、「跑一下 wiki lint」、「檢查無宿主頁」、「回訪這頁」、「更新 index」、「每週維護」時使用。"
tags: [research, file_organization, knowledge_management, maintenance]
---

# Wiki Maintenance — Research Library 維護技能（v0.4）

> **v0.3（2026-11-04）重新定位**：Research Library 不是「研究作品圖書館」，是 **semantic memory 的詳細附件**。
> v0.2 把它當成一條獨立流水線（研究 → 蒸餾 → 歸檔），結果 57 頁裡 49% 沒有任何其他頁連到它、86% 建立後從未修改。
> **流水線跑完就結案，附件沒有終點。**
>
> **v0.4（2026-11-15）對齊 #650 S2**：`recall` 現在直接搜得到 `outputs/doc/` 每一頁（文件路徑，每個 `#`～`###` 標題一個篇章）。
> 「讓頁面被找到」不再靠指標記憶；指標記憶的理由換成「**用意思找到頁面的入口**」（見 A 機制）。

---

## 三層架構（先讀這段，它決定一切）

| 層 | 職能 | 載體 | 誰讀它 | 更新節奏 |
|---|---|---|---|---|
| **memory** | 單點事實 + 長期關聯 | `memorize` + relation triples | `recall`，每 session | 隨時 |
| **pursuit** | 主題的時序整合 | `~/.loom/pursuits/*.md` | check_in / DK 問起 | 有新事件就 append |
| **wiki 頁**（本技能） | **詳細附件**（memory 一格放不下的長篇論證） | `outputs/doc/**` | `recall` 字面命中，或被 memory / pursuit **指標指到**時 | 被新案例 append |

**一句話**：memory 記「我判斷什麼」，pursuit 記「這條線發生了什麼」，wiki 記「這件事我想到哪裡」。

### A 機制：指標（雙向，但只有兩個方向）

1. **memory → wiki**：事實 value 的**第一行**必須是 `→ 附件：doc:wiki/<路徑>#<篇章標題路徑>`（不加 `@版本`）。
   - 這個寫法可以直接 `recall(ref=...)` 打開**現版**篇章；不加 `@版本`，頁面改寫後指標也不會過期。
   - 不要手打：用 `recall` 查到該篇章，複製結果裡的 ref，刪掉 `@` 以後的部分。
   - ⚠️ **必須放最前面**——`recall` 只顯示 value 前約 300 字，寫在結尾等於不存在。
2. **wiki → memory**：頁面頂部的「宿主」欄位列住養它的 memory key。

**為什麼 recall 已經搜得到 wiki，還要指標（v0.4）**

兩條路徑不對稱：

| 路徑 | 比對方式 | 能接住的問法 |
|---|---|---|
| 文件（wiki 頁本身） | **只有字面**（trigram／中文 bigram） | 用字接近原文、標題詞、檔名 |
| 記憶（指標所在） | 字面 **+ embedding** | 只帶「意思」、用字不同的問法 |

所以問題用字貼近原文時，文件路徑直接命中，不需要指標；**問題只有意思、用字不同時，唯一的入口是 embedding 找到那則記憶，記憶第一行再指向頁面**。

> 這仍是推論：「有／無指標」的對照實驗等 #669（短詞排序修正）落地後再跑（#670）。實驗推翻這條，就降級本節。

**讓頁面本身容易被字面找到**

- **標題用會被拿來查的詞。** 篇章標題的權重是正文的 10 倍；「累積案例 3」不如「案例：指標寫在結尾被截斷」。
- **檔名用主題詞**（例：`measurement-boundaries-2026-11-04.md`），因為檔名也可以搜尋。

---

## 成功定義

- **產出**：蒸餾 / 回訪 / lint 完成，寫進正確位置並更新 `_meta/`
- **品質指標（三項，缺一不算成功）**：
  1. **有宿主** — 頁面掛在某個 pursuit id 或 memory key 底下
  2. **有「目前結論」** — 頂部 ≤200 字的覆寫式摘要
  3. **被指到** — 至少一則記憶事實的第一行指向它
- **驗收方式**：`recall` 用頁內獨有的詞查得到、`log.md` 有 entry、**且該頁曾被 append 過（不是只被建立）**

---

## 長度天花板（v0.3 新增，最重要的一節）

**問題不是「會不會太長」，是「長了之後會怎樣」。** 沒有天花板的文件，注意力與 token 都會被攤薄。

| 對象 | 上限 | 超過時怎麼辦 |
|---|---|---|
| 頁面「目前結論」 | **≤ 200 字** | 強制壓縮，不許貼長段落 |
| 單一案例 / 快照 | **≤ 300 字** | 細節移去附件或拆頁 |
| 頁面累積案例數 | **≤ 15 則** | 做一次「合併同類」，不是繼續加 |
| 單一 wiki 頁總行數 | **≤ 300 行** | **拆成兩個檔案**（主頁 + `-detail` 或 `-archive`） |
| memory 事實 value | 前 240 字內必須講完重點 | 指標 + 結論放最前，細節往後堆 |

> **DK 2026-11-04**：「其實就是要限制文件長度的天花板就好。以後真的遇到太長的文件就分兩個檔案，有這樣的心理準備就不會文字大爆炸。」
> **同一個道理適用於 recall 的截斷上限**：不只是省 token，是讓注意力好好放在重點上。

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

### 情境 A：新增內容（蒸餾進 wiki 或續寫既有頁）

```
0. 找宿主 ← 新增的必經步驟
   問：這件事養在哪個 pursuit／哪則記憶底下？
   ├─ 有宿主 → 續寫或新建頁
   └─ 沒有宿主 → 這是研究報告，不是 wiki 頁 → 不進 wiki（留 outputs/ 或進 archive）
1. 判斷是「新頁」還是「append 既有頁」
   → 預設 append。要開新頁，必須回答「為什麼不能併進 X」
2. 讀取目標頁（Legitimacy Guard 要求），確認「目前結論」與案例結構
3. 寫入：
   - 新案例 append 到「累積案例」，每則 ≤300 字，帶日期
   - 若新案例改變了判斷 → 改寫「目前結論」
   - 若推翻了舊結論 → 舊結論移到「已推翻」，寫明被什麼推翻
4. 更新宿主：對應的 memory 事實第一行加/更新 `→ 附件：doc:wiki/...` 指標
5. 更新 `_meta/log.md`；新頁也登記到 `_meta/index.md`（給人看的目錄）
6. 檢查長度天花板，超過就拆檔
7. **驗證被找到**：`recall` 用頁內獨有的詞查一次，確認新篇章出現（索引在 recall 時同步，不用等）
8. `unload_skill("wiki_maintenance")`
```

### 情境 B：Wiki Lint（健康檢查）

> ⚠️ 不再使用「orphan pages」這個詞。Wikipedia 的 orphan 定義是 **in-degree = 0 的連結圖節點**；Loom 的 Research Library 幾乎沒有連結圖，量不到那個量。（2026-11-04 修正）

1. 讀 `_meta/index.md`、`_meta/log.md`；找特定頁面用 `recall`，不用翻 index
2. 檢查五項：
   - **無宿主頁**：沒有掛在任何 pursuit / memory key 底下的頁面
   - **斷鏈**：index 指向但檔案不存在（用 `[[wikilink]]` 名稱比對，注意日期格式漂移）
   - **結論過期**：有更新案例進來但「目前結論」沒改
   - **被推翻未標記**：新案例明顯否定舊結論，但沒進「已推翻」段
   - **超長**：超過 300 行 / 案例超過 15 則
3. 發現問題寫入 `_meta/lint_report_YYYYMMDD.md`（嚴重度低的可口報）
4. `unload_skill("wiki_maintenance")`

### 情境 C：更新 index / 整理結構

> v0.4：`_meta/index.md` 是**給人看的目錄**（DK／Claude Code 瀏覽用）。Loom Agent 自己找頁面用 `recall`，不必為了「被找到」而維護 index。

1. 讀 `_meta/index.md`
2. 檢查新頁未列入、分類錯誤、版本註記過期
3. 修正並 append log entry
4. `unload_skill("wiki_maintenance")`

### 情境 D：回訪（v0.3 新增——這是把死檔案變活的那一步）

> 觸發詞：「回訪」「這頁接上」「append 進去」「這跟上次那頁有關」

1. **打開一頁**（不是建立一頁）
2. 問三件事：
   - 今天發生的事，**推翻或細化了這頁的哪個結論**？
   - 這頁的「未解」段，有沒有哪一條今天有材料了？
   - 「目前結論」還成立嗎？
3. 有話可說 → append 案例 + 必要時改寫目前結論
4. **沒話可說也要留痕**：在「未解」段標 `（YYYY-MM-DD 檢查，無新材料）`
5. `unload_skill("wiki_maintenance")`

---

## Wiki 頁面標準格式（v0.3）

```markdown
---
title: 標題
date: YYYY-MM-DD
category: cognitive / ai / system / philosophy
tags: [tag1, tag2]
status: active / superseded / archived
---

# 頁面標題

> **宿主**：`pursuit-id` 或 `memory:key1`、`memory:key2`
> **本頁的邊界**：只收什麼、不收什麼（≤3 行）

## 目前結論（≤200 字，覆寫式）

## 累積案例（每則 ≤300 字，按日期 append，上限 15 則）

## 已推翻（舊結論 + 日期 + 被什麼推翻）

## 未解 / 下次回來改
```

**沒有 frontmatter 的舊頁不強制補**；新頁與被回訪的頁才套用。

---

## Taxonomy 分類

```
outputs/doc/research/      （recall 收的是整個 outputs/doc/，未登記 index 的散檔也收）
├── cognitive/     ← 認知、記憶、間接測量、思維模型
├── ai/            ← LLM、AI系統、Agent架構
├── system/        ← Loom框架技術、工具開發
├── philosophy/    ← 哲學、宗教、抽象概念
├── history/       ← 歷史研究
└── meta/          ← 關於 wiki 自身的記錄
```

---

## 原始檔案處理流程

```
研究完成
    ↓
找宿主（有 → 繼續；沒有 → 進 archive，不進 wiki）
    ↓
蒸餾進 wiki（append 既有頁優先）
    ↓
更新 _meta/index.md + _meta/log.md + 宿主記憶的指標
    ↓
原始檔案 move archive/distilled/{YYYYMMDD}/
```

`archive/distilled/` 是冷儲存，不是垃圾筒。

---

## _meta/ 結構

```
_meta/
├── taxonomy.md       ← 分類規範、命名慣例
├── index.md          ← 給人看的頁面目錄（應在頂部記錄「上次 lint」日期）
├── log.md            ← 維護日誌（append-only）
├── lint_report_*.md  ← 歷次 lint 報告
└── audit.md          ← 待處理問題（可選）
```

---

## 不在範圍

- `task_list` 的便利貼管理
- `pursuit` 的長期追蹤（但**可以引用 pursuit，也可能以其為宿主**）
- 框架代碼修改
- 沒有宿主、也沒有交付對象的一次性報告

---

## 觸發關鍵詞

- 「蒸餾」「寫進 wiki」「整理進 library」
- 「lint」「健康檢查」「無宿主」
- 「回訪」「這頁接上」「append 進去」
- 「更新 index」「整理 outputs/」「每週維護」

---

## 與其他技能 / 機制的關係

| 機制 | 分工 |
|------|------|
| `deep_researcher` | 執行研究（並行抓資料、寫報告） |
| 收斂夢 + `loom memory health` | Semantic Memory 健康（去重/矛盾） |
| **pursuit** | 主題的時序整合，也是 wiki 頁最常見的宿主 |
| `wiki_maintenance` | 維護附件層（宿主、回訪、天花板、lint） |
| `recall` | 找頁面的入口：字面直接命中篇章；用意思找時經由指標記憶 |

**它們不是一條流水線，是三層。** v0.2 之所以讓圖書館變成倉庫，就是把它寫成一條有終點的流水線。

---

## 修訂記錄

- **v0.4（2026-11-15）**：對齊 #650 S2（recall 直接搜得到 `outputs/doc/`）。指標記憶保留，理由改為「用意思找到頁面的入口」（文件路徑只有字面比對）；指標改寫成不帶版本的 `doc:` ref；`_meta/index.md` 降級為給人看的目錄；新增「標題與檔名用主題詞」與寫完後的 recall 驗證；觸發詞移除 orphan。依據 Loom Agent 的 S2 使用回饋（#650）。
- **v0.3（2026-11-04）**：重新定位為「semantic memory 的詳細附件」。新增三層架構、A 機制（指標必須置頂，因為 recall 截斷）、長度天花板表、宿主規則、情境 D 回訪。移除 orphan 一詞（量不到）。備份 `SKILL.md.bak-20261104`。
- **v0.2（2026-05-22）**：首次建立（參照 Karpathy LLM Wiki 模式）。

---

*wiki_maintenance v0.4 — 2026-11-15*
