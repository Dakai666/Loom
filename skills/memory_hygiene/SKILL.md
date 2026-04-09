---
name: memory_hygiene
description: "記憶系統衛生維護技能。整理、驗證、清理 Loom 自身的記憶系統，包括 semantic memory 去重、episodic 過期清理、relational 一致性檢查、skill confidence 漂移偵測。當使用者要求「整理記憶」、「清理過期記憶」、「記憶體健康檢查」、「memory cleanup」、「記憶診斷」時使用。"
tags: [memory, maintenance, hygiene, cleanup, diagnosis, governance]
precondition_checks:
  - ref: checks.require_memory_backup
    applies_to: [run_bash]
    description: "執行清理操作前必須確認 memory.db 有備份"
  - ref: checks.reject_direct_db_mutation
    applies_to: [run_bash]
    description: "禁止直接 SQL 修改記憶體資料庫，必須透過框架 API"
---

# Memory Hygiene

Loom 記憶系統的衛生維護技能。作為 memory-native 框架，記憶品質直接影響決策品質。

---

## 核心原則

1. **先診斷後治療** — 不做「預防性清理」，先有數據證明需要清理再動手
2. **備份先行** — 任何修改性操作前必須確認備份存在
3. **透過框架操作** — 永遠不直接操作 SQLite，使用 recall/memorize/relate API
4. **報告每一步** — 使用者必須看到「清理了什麼、為什麼、影響多少筆」

---

## 工作流程（四階段）

### 階段 1：健康診斷

**目標：建立記憶系統的健康快照**

使用 `recall` 和 `run_bash` 收集以下指標：

- **Semantic memory**：總筆數、confidence 分布、最舊條目日期
- **Episodic memory**：未壓縮 session 數、平均每 session 條目數
- **Relational memory**：總三元組數、孤立節點數
- **Skill genomes**：各技能 confidence、是否有技能低於 deprecation_threshold
- **資料庫大小**：memory.db 檔案大小、WAL 大小

**輸出格式：**
```markdown
## Memory Health Report

| 指標 | 數值 | 狀態 |
|------|------|------|
| Semantic 條目數 | {N} | {OK/WARNING/CRITICAL} |
| 低 confidence 條目 (<0.3) | {N} | {OK/WARNING} |
| 未壓縮 episodic sessions | {N} | {OK/WARNING} |
| Relational 孤立節點 | {N} | {OK/WARNING} |
| Skill 低信心 (<0.5) | {names} | {OK/WARNING} |
| DB 大小 | {MB} | {OK/WARNING} |
```

### 階段 2：問題識別

**目標：從健康快照中識別具體問題**

常見問題模式：

| 問題 | 偵測方式 | 嚴重度 |
|------|---------|--------|
| 語義重複 | 相似度 > 0.9 的 key pairs | MEDIUM |
| 信心衰減 | confidence < 0.1 且超過 90 天未更新 | LOW |
| 矛盾事實 | 同 key 不同 value 且 metadata.history > 3 | HIGH |
| 技能漂移 | skill confidence 持續下降但 usage_count 增加 | HIGH |
| 記憶膨脹 | semantic 條目超過 1000 | MEDIUM |
| 孤立關係 | relational entry 的 subject/object 在其他表中不存在 | LOW |

### 階段 3：清理執行

**目標：針對識別的問題執行修復**

每個清理操作必須：
1. 說明要清理什麼、為什麼
2. 確認使用者同意
3. 使用框架 API 執行（memorize 覆寫、relate 更新）
4. 報告清理結果

**清理策略：**

- **語義去重**：保留 confidence 較高的條目，刪除重複
- **過期清理**：confidence 衰減到 < 0.05 的自動刪除
- **矛盾解決**：呈現矛盾雙方，讓使用者決定保留哪個
- **技能復健**：低信心技能建議觸發 meta-skill-engineer 重新評估

### 階段 4：報告與建議

**目標：輸出清理報告並給出長期建議**

```markdown
## Memory Hygiene Report

### 執行摘要
- 清理 {N} 筆過期 semantic 條目
- 合併 {N} 組重複條目
- 解決 {N} 個矛盾
- 標記 {N} 個技能需要復健

### 詳細變更
[每個操作的 before/after]

### 長期建議
- [基於觀察到的模式給出建議]
```

---

## 工具使用策略

| 工具 | 使用時機 |
|------|---------|
| `recall` | 查詢記憶內容、偵測重複和矛盾 |
| `memorize` | 覆寫過期或重複的條目 |
| `relate` | 更新或清理關係三元組 |
| `query_relations` | 偵測孤立節點和循環引用 |
| `run_bash` | 查看 DB 大小、WAL 狀態、備份確認 |
| `read_file` | 讀取 memory.db 的 schema 或設定 |

---

## 紀律提醒

- **不做盲目清理** — 「記憶太多了所以刪一半」不是策略
- **不跳過備份** — precondition check 會強制確認，但心態上也要重視
- **不直接 SQL** — `sqlite3 memory.db "DELETE FROM ..."` 絕對禁止
- **矛盾讓使用者決定** — 兩個矛盾的事實，agent 不應該自行判定誰對
- **清理後驗證** — 清理完再跑一次診斷，確認沒有副作用

---

## Skill Genome 整合提示

- 此技能由 ProceduralMemory 管理
- 觸發關鍵詞：「記憶清理」「memory cleanup」「記憶健康」「整理記憶」「記憶診斷」
- 每次成功清理後 confidence 提升
- 若清理導致資料遺失（使用者回報），confidence 大幅下降
