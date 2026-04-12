# 記憶宮殿 TUI — 開發規劃文件

> **狀態**: 確認中 · 準備開發  
> **作者**: 絲繹・Loom  
> **日期**: 2026-04-12  
> ** Loom 版本**: v0.4+

---

## 1. 背景與動機

### 現況
現有的 `loom memory list` 指令僅提供 `SemanticMemory.list_recent()` 的純文字輸出，無分層、無視覺化，難以駕馭快速成長的記憶系統。

### 目標
打造一個獨立於 Loom Chat TUI 的紫色記憶宮殿（Memory Palace）TUI，提供沉浸式的記憶探索體驗。

### 約束（不改動原架構）
- ✅ 只對記憶 DB 進行唯讀存取
- ✅ 不修改 `loom/core/memory/` 下的任何類別
- ✅ 不修改 `loom/platform/cli/tui/` 現有元件
- ✅ 新功能透過 `loom palace` 指令觸發，獨立运行，不影響聊天 TUI

---

## 2. 設計語言

### 色彩系統（記憶宮殿紫色系）

```css
/* 背景層次 */
--palace-bg:        #0d0a1a   /* 深紫黑：screen 背景 */
--palace-surface:   #150d28   /* 深紫：面板 surface */
--palace-raised:    #1e1238   /* 紫灰：卡片/元件 */
--palace-border:    #3d2a6b   /* 中紫：邊界線 */

/* 文字 */
--palace-text:      #e8deff   /* 淡紫白：主要文字 */
--palace-muted:     #9b87b5   /* 灰紫：次要文字 */
--palace-gold:      #d4a853   /* 金色：標題/強調（與 Loom 主題呼應）*/

/* 信任/狀態 */
--palace-high:      #a78bfa   /* 亮紫：high confidence */
--palace-mid:       #c084fc   /* 中紫：medium */
--palace-low:       #7c3aed   /* 深紫：low / 衰減 */
--palace-gold-alt:  #e9c46a   /* 金黃：成功 */
--palace-rose:      #f87171   /* 玫瑰：錯誤/刪除 */
--palace-slate:     #64748b   /* 暗灰：disabled */
```

### 字體與裝飾
- 標題：使用 `❖`（Loom 既有符号）+ 紫色星光意象
- 分隔線：`✦` 小星星裝飾
- 面板標題：反向高亮（`[reverse #9b59b6] text [/reverse]`)
- 圖標：純文字 Unicode，`⟡` 連接、`◈` 實心、`✧` 空心

### 動效哲學
- 面板切換：instant（不改變佈局）
- 搜尋結果：staggered fade-in（每項 30ms delay）

---

## 3. 記憶類型視圖

### 3.1 Semantic Memory（語義宮殿）
> 入口景觀廳 — 所有 facts 的總覽

**呈現方式**：
- 頂部：統計卡片（總數量、高/中/低信心分布、衰減警示）
- 主體：可排序列表（按 key / confidence / updated_at / source）
- 每筆記錄：
  ```
  ◈ project:loom:memory_schema
    語意記憶使用 SQLite FTS5 支持模糊匹配。
    [信任: 0.92] [source: session:*] [更新: 3天前]
  ```
- 支援點擊 key 展開 history（如有覆寫，顯示前 3 筆舊值）

**視圖模式**：
- `Browse`：按 key 前綴分組（`skill:*`、`project:*`、`session:*`）
- `Search`：即時 BM25 搜尋（透過 MemorySearch.recall）
- `Health`：信心低於 0.3 的長尾項目

### 3.2 Relational Memory（關係星圖）
> 星圖視圖 — 主語關係網絡

**呈現方式**：
- 頂部：主語列表（按 triples 數量排序）
- 選擇主語後：謂語列表
- 每條記錄：
  ```
  [user] ──prefers──▶ concise responses
             │              [conf: 1.0]
             ├──uses────────▶ SQLite WAL
             └──avoids──────▶ trailing summaries
  ```

**視圖模式**：
- `Subjects`：所有主語列表，選擇後展開其所有 predicate
- `Graph`：文本化的 triple 視圖
- `Predicates`：按謂語聚合，查詢所有具有該關係的主語

### 3.3 Episodic Memory（時光迴廊）
> 時光迴廊 — 對話歷史

**呈現方式**：
- 頂部：Sessions 列表（按 last_active 排序）
- 選擇 session 後：turn timeline
- 每個 turn：
  ```
  Turn 3  2026-04-12 15:40
  you>  幫我建立一個新技能...
  ↳  loom>  ⟳ write_file — "skills/new_skill.md"
           ✓  1,240ms
           ⟳ recall — "skills workflow"
           ✓    89ms
       好的，我來幫你建立這個技能...
  ```

### 3.4 Skill Genomes（技能基因庫）
> 技能基因庫 — 技能表現

**呈現方式**：
- 按 confidence 排序的技能卡片
- 每張卡片：
  ```
  ✧ systematic_code_analyst
    用於陌生程式碼庫的結構化分析...
    [conf: 0.85] [used: 47×] [success: 91%]
    tags: [code_analysis] [agent_builtin]
  ```

**視圖模式**：
- `Active`：usage_count > 0
- `Failing`：success_rate < 0.6
- `Evolving`：confidence 在下降中

### 3.5 Memory Health（宮殿體檢）
> 宮殿體檢 — 記憶系統總覽

**呈現方式**：
- 四宮格面板：
  ```
  ┌─────────────────┬─────────────────┐
  │  Semantic       │  Relational     │
  │  1,247 facts    │  89 triples     │
  │  ↑ 12 today     │  ↑ 3 today      │
  ├─────────────────┼─────────────────┤
  │  Skills         │  Sessions       │
  │  11 active      │  34 logged      │
  │  ⚠ 2 failing    │  3h ago active  │
  └─────────────────┴─────────────────┘
  ```
- 衰減預警列表：即將低於 threshold 的 semantic entries
- 矛盾檢測摘要（如有）

---

## 4. 導航架構

### 輸入/輸出結構

```
┌──────────────────────────────────────────────┐
│ ❖ Memory Palace           [?] [F1: Help]      │  ← Header
├──────────────┬───────────────────────────────┤
│              │                               │
│  NAV SIDEBAR │   CONTENT AREA                │
│  40% width   │   60% width                  │
│              │                               │
│  Semantic    │   [視圖內容]                  │
│  Relational  │                               │
│  Episodic    │                               │
│  Skills      │                               │
│  ─────────── │                               │
│  Health      │                               │
│              │                               │
├──────────────┴───────────────────────────────┤
│ Status: 1,247 facts · 89 triples · 34 sessions│  ← Status Bar
└──────────────────────────────────────────────┘
```

### 快捷鍵

| 按鍵 | 功能 |
|------|------|
| `Tab` | 切換 Content Area 內的視圖模式 |
| `↑/↓` | 導航列表 |
| `Enter` | 展開/進入 |
| `Esc` | 返回上一層 |
| `Ctrl+F` | 聚焦搜尋框 |
| `1-5` | 快速切換記憶類型（數字鍵） |
| `F1` | 幫助面板 |
| `Ctrl+Q` | 退出 |

---

## 5. 實作規劃

### Phase 1：核心框架（MVP）

**目標**：最小可用版本，專注 Semantic 和 Health 兩個視圖

| 元件 | 說明 |
|------|------|
| `loom/palace/` | 新套件，所有記憶宮殿相關程式碼 |
| `loom/palace/__init__.py` | 套件導出 |
| `loom/palace/app.py` | Textual App 實例 + CSS theme |
| `loom/palace/components/` | 元件目錄 |
| `loom/palace/components/header.py` | 宮殿頂部標題列 |
| `loom/palace/components/nav.py` | 左側導航側邊欄 |
| `loom/palace/components/status.py` | 底部狀態列 |
| `loom/palace/components/semantic_view.py` | Semantic 視圖 |
| `loom/palace/components/health_view.py` | Health 概覽視圖 |
| `loom/palace/components/relational_view.py` | Relational 視圖 |
| `loom/palace/components/episodic_view.py` | Episodic 視圖 |
| `loom/palace/components/skills_view.py` | Skills 視圖 |
| `loom/palace/search.py` | PalaceSearch 協調層 |

### Phase 2：深化功能

- 即時 BM25 搜尋（透過 SemanticMemory.search() + FTS5）
- Episodic Session Timeline 展開
- 點擊 key 展開覆寫 history
- 信心熱力圖（gradient）

### Phase 3：增強體驗

- 面板內 sub-filter（各視圖內的即時過濾）
- 記憶豐度報告（基於 SemanticEntry.effective_confidence）
- 矛盾檢測摘要整合（Contradiction API）

---

## 6. CLI 整合

### 新增指令

```bash
# 進入記憶宮殿（預設開啟 Semantic 視圖）
loom palace

# 直接指定視圖
loom palace --view semantic
loom palace --view relational
loom palace --view episodic
loom palace --view skills
loom palace --view health

# 快速查看（無 TUI，直接 Rich 輸出）
loom palace --stat
loom palace --search "loops" --type semantic --limit 20
```

### 與現有指令的互補關係

| 現有指令 | Memory Palace 的差異 |
|----------|---------------------|
| `loom memory list` | 純文字列表 → 互動式多視圖 TUI |
| `loom sessions list` | 整合入 Episodic 視圖，提供 timeline |
| `loom reflect` | 提供 Skill Health 視圖的視覺化版本 |

---

## 7. 技術實現要點

### 架構決策

**跨視圖搜尋策略：借用 + 組合，不重造輪子**

現有系統已有一層完整的搜尋基礎，記憶宮殿只需要一個薄的協調層：

```
PalaceSearch (協調層)
    ├── Semantic  → MemorySearch.recall(type="semantic")  [直接用]
    ├── Skills    → MemorySearch.recall(type="skill")      [直接用]
    ├── Relational→ RelationalMemory.query() + LIKE         [簡單擴充]
    └── Sessions  → SessionLog.load_messages() + FTS LIKE  [簡單擴充]
```

- Semantic / Skills：直接使用 `MemorySearch.recall()`，FTS5 + BM25 現成
- Relational：RelationalMemory 沒有 FTS5 virtual table，直接用 SQL LIKE（足夠記憶宮殿探索粒度）
- Sessions：SessionLog 沒有全文索引，用 SQL LIKE 查 content 欄位

**不為記憶宮殿另建 FTS5 table** — 原有的 FTS5 鏡像表（`semantic_fts`、`skill_fts`）已足夠。

### Textual 模式
- 繼承 `App`（非 LoomApp），獨立於聊天 TUI
- 使用 `Vertical` + `Horizontal` 容器佈局
- 所有 CSS 在 `app.py` 的 `CSS` class 變數中定義

### 資料存取策略
- 透過 `SQLiteStore.connect()` 取得連接（唯讀操作）
- 各視圖 components 在 `on_mount` 時初始化 `SemanticMemory`、`RelationalMemory` 等
- DB 查詢在 worker thread 避免阻塞 UI

### 效能考量
- 首次 `on_mount` 只抓 top-N（limit=100），其餘懶載入
- 大列表（>500 筆）使用 `ListView` + 虛擬化
- 不引入額外相依（僅使用 Loom 既有 `aiosqlite`, `textual`）

---

## 8. 成功標準

- [ ] `loom palace` 指令可正常啟動，紫色主題完整呈現
- [ ] Semantic View 顯示 list_recent() 結果，含信心顏色標示
- [ ] Health View 顯示四宮格統計
- [ ] `loom palace --view relational` 直接開啟關係視圖
- [ ] 側邊導航可在五個記憶類型間流暢切換
- [ ] 不修改任何 `loom/core/memory/` 或 `loom/platform/cli/tui/` 現有檔案

---

## 9. 已確認的設計決策

| 決策 | 結論 |
|------|------|
| 指令命名 | `loom palace` |
| 與聊天 TUI 關係 | 完全獨立套件，单独運行 |
| Theme 共享 | 不共享，未來有需要再規劃 |
| 即時性 | 不需要，聊天 TUI 和記憶宮殿不會同時開啟 |
| 跨視圖搜尋 | 需要，策略：借用現有 MemorySearch + 薄協調層 |
| FTS5 新建 | 不需要，relational 用 SQL LIKE 即可 |

---

## 10. 已確認的開發順序

**Phase 1**（本輪實作）：
1. 入口指令 + 紫色 theme scaffold（`loom palace` 指令掛鉤、`PalaceApp` 框架）
2. Semantic View（列表 + 信心顏色 + 排序）
3. 左側 Nav sidebar（五個視圖切換）
4. Health 四宮格

**Phase 2**（下一輪）：
5. Relational View
6. Episodic View
7. Cross-view search 整合（PalaceSearch 協調層）

**Phase 3**（最後）：
8. Skills View
9. History 展開、sub-filter 等細節打磨
