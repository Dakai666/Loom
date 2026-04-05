# Skill Genome

Skill Genome 是 Loom 的「程序記憶」單元——它儲存的不是「事實」，而是「如何做」。

---

## 為什麼叫 Genome？

基因組（Genome）是攜帶遺傳訊息的分子，包含發展成完整生物體的所有指令。Skill Genome 類似——它是技能的「基因」，攜帶這個技能的所有重要特徵：成功率、使用次數、信心分數、標籤、以及技能本體。

就像基因會突變（版本更新）和被淘汰（confidence 過低），Skill Genome 也有生命週期。

---

## 完整資料結構

```python
@dataclass
class SkillGenome:
    # 識別
    id: str                     # "refactor_extract_function"，唯一
    version: int = 1            # 版本號

    # 成效追蹤
    confidence: float = 0.5    # EMA 信心分數，0.0-1.0
    usage_count: int = 0        # 累積使用次數
    success_rate: float = 0.0   # 歷史成功率（0.0-1.0）

    # 繼承
    parent_skill: str | None = None  # 父技能 ID（用於技能衍生）

    # 廢棄閾值
    deprecation_threshold: float = 0.3  # 低於此值自動廢棄

    # 內容
    tags: list[str] = field(default_factory=list)
    body: str = ""              # 技能的具體內容（有效 prompt 或描述）

    # 時間戳
    created_at: str = ""
    updated_at: str = ""

    # 元資料
    metadata: dict = field(default_factory=dict)
```

---

## EMA Confidence 機制

### 指數移動平均（Exponential Moving Average）

傳統的簡單平均會讓新數據被歷史數據稀釋：

```python
# 簡單平均的問題
new_confidence = (old_confidence * (usage_count - 1) + new_outcome) / usage_count
# 當 usage_count=100 時，new_outcome 只佔 1/100 的權重
```

EMA 解決這個問題——賦予新數據固定的權重 α：

```python
alpha = 0.3  # 可調整的衰減因子

new_confidence = alpha * new_outcome + (1 - alpha) * old_confidence
# new_outcome 永遠佔 30% 權重，old_confidence 佔 70%
```

### 更新邏輯

```python
def record_outcome(self, success: bool) -> None:
    """
    每次 Skill 被使用並得到結果後呼叫。
    success=True → outcome = 1.0
    success=False → outcome = 0.0
    """
    new_outcome = 1.0 if success else 0.0
    alpha = 0.3

    # EMA 更新 confidence
    self.confidence = alpha * new_outcome + (1 - alpha) * self.confidence

    # 更新統計
    self.usage_count += 1
    self.success_rate = (
        (self.success_rate * (self.usage_count - 1) + new_outcome)
        / self.usage_count
    )

    self.updated_at = datetime.now().isoformat()

    # 廢棄檢查
    self._check_deprecation()
```

### EMA 的直覺解釋

```
confidence = 0.7（當前）
使用一次，失敗了 → new_confidence = 0.3 * 0.0 + 0.7 * 0.7 = 0.49
使用一次，成功了 → new_confidence = 0.3 * 1.0 + 0.7 * 0.49 = 0.643

連續失敗 3 次 → confidence ≈ 0.3 * 0 + 0.7 * (0.7 * 0.7 * 0.7) ≈ 0.24 < 0.3 → 廢棄
```

---

## 版本管理

每次 Skill 有實質性更新時，版本號 +1：

```python
def update_body(self, new_body: str, reason: str = "") -> None:
    """更新技能本體（版本 +1）"""
    self.version += 1
    self.body = new_body
    self.metadata["update_reason"] = reason
    self.metadata["previous_version"] = self.version - 1
    self.updated_at = datetime.now().isoformat()
```

---

## 自動廢棄

當 `confidence ≤ deprecation_threshold` 時，Skill Genome 會被標記為廢棄：

```python
def _check_deprecation(self) -> None:
    if self.confidence <= self.deprecation_threshold and not self.is_deprecated():
        self.deprecate()

def deprecate(self) -> None:
    self.body = f"[DEPRECATED] {self.body}"
    self.metadata["deprecated"] = True
    self.metadata["deprecated_at"] = datetime.now().isoformat()
    # version 不變，保留歷史紀錄
```

廢棄的技能：
- 不再被 `list_active()` 返回
- 仍保留在 DB 中（可供人工審查）
- 可以手動恢復（重新設定 confidence）

---

## 父子技能關係

技能可以衍生：

```
Skill A (parent): "Python 重構基礎"
    ├── Skill B: "提取函數"
    └── Skill C: "合併重複代碼"
```

```python
# 建立子技能
await procedural.create_skill(
    id="refactor_extract_function",
    parent_skill="refactor_base",
    body="當函數超過 30 行且有明顯可分離邏輯時...",
    tags=["refactor", "python", "function"],
)
```

好處：
- 繼承標籤（除非明確覆寫）
- 可以在 `parent_skill` 死亡時預警所有子技能
- 技能家族的集體健康報告

---

## 技能評估回路

Skill Genome 的價值在於它形成了一個閉環：

```
Agent 執行任務
    ↓
Middleware Pipeline（TraceMiddleware）
    ↓
工具執行成功 / 失敗
    ↓
SkillGenome.record_outcome(success)
    ↓
confidence EMA 更新
    ↓
upsert 至 ProceduralMemory（持久化）
    ↓
下次任務 → recall → 選擇高 confidence 技能 → 使用
```

這個回路讓 Loom 能「從經驗中學習」——成功的技能越來越可信，失敗的技能逐漸被淘汰。

---

## 與其他記憶的區別

| 維度 | Semantic Memory | Procedural Memory（Skill Genome）|
|------|----------------|----------------------------------|
| **儲存內容** | 事實 | 過程/方法 |
| **更新觸發** | Agent 主動 memorize | 工具執行結果被動反饋 |
| **Confidence 意義** | 事實的可靠程度 | 技能的歷史成功率 |
| **典型內容** | 「專案使用 SQLite WAL」| 「當有大重構需求時，優先分析模組邊界」|
| **驗證方式** | 可檢索驗證 | 實踐檢驗（使用結果）|

---

## 實驗性：技能健康儀表板

以下是 Reflection API 提供的技能健康報告格式：

```
Skill Genome Health Report
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ID                  V   Confidence   Uses   Rate    Status
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
refactor_extract    3   0.87 ██████  14     92%     ✅ active
python_unittest     2   0.71 █████   8     75%     ✅ active
bash_deploy         1   0.31 ▂      3     33%     ⚠️ warning
legacy_script       2   0.22 ▁      5     40%     🔴 deprecated
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Total: 4 skills, 2 active, 1 warning, 1 deprecated
```
