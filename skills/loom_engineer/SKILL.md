---
name: loom_engineer
description: "Loom 框架的實作技能，覆蓋從 issue 分析到 PR 產出的完整實作迴圈。當使用者要求「幫我修這個 issue」、「這個 PR 要怎麼 review」、「幫我寫一個功能」、「debug 這個問題」時使用。是 systematic_code_analyst 的下游：分析完成後的實作、驗證與產出由本技能負責。"
precondition_checks:
  - ref: checks.require_git_repo
    applies_to: [run_bash, write_file]
    description: "必須在 git repository 內操作"
  - ref: checks.reject_force_push
    applies_to: [run_bash]
    description: "禁止 git push --force，防止歷史被覆寫"
---

# Loom Engineer

覆蓋「分析→實作→review」這段，閉合 Loom 自我改進迴圈。

---

## 📁 技能結構

```
skills/loom_engineer/
└── SKILL.md           ← 本技能定義
```

---

## 🎯 核心原則

1. **先確認意圖，再動手** — 搞清楚修 bug 還是新增功能，scope 完全不同
2. **說計畫，再實作** — 先說「我打算怎麼改」，讓使用者確認方向，節省來回時間
3. **最小改動原則** — 改最小的範圍達到目標，不做「順便重構一下」
4. **每個變動都有理由** — 沒有無緣無故的改動；沒有「順便」的改動
5. **測試跟著實作走** — 有實作就有測試，沒有測試的實作不完整
6. **review 先於 commit** — 自己先看一遍 diff，再當作最後防線

---

## 🔄 工作流程（六階段）

### 階段 1：理解意圖

**目標：搞清楚要什麼**

- 取得 issue 內容（URL → fetch_url；本地 → read_file）
- 識別 issue type：
  - `bug` — 有錯誤行為，修復它
  - `enhancement` — 改善現有行為
  - `refactor` — 重構，不改行為
  - `feature` — 新增功能
- 識別 scope：哪些 modules 需要動？
- 識別 constraint：有沒有破壞性變更（breaking change）？

**產出：** 一段「意圖說明書」（不超過 50 字）

### 階段 2：範圍確認

**目標：讓使用者在實作前確認範圍**

說清楚：
1. 我要改哪些檔案
2. 我不改哪些（即使它看起來也有問題）
3. 這個改動的邊界在哪裡

**產出：** 簡短的 scope 列表

### 階段 3：實作計畫

**目標：說計畫，不是埋下去**

在開始改 code 之前，用以下格式說明：

```markdown
## 實作計畫：{issue title}

### 改動策略
[我要怎麼做，2-3 句話]

### 具體步驟
1. [step 1 — 具體檔案和修改內容]
2. [step 2]
3. [step 3]

### 預期效果
[改完之後，系統會變成什麼樣]

### 潛在風險
[我担心什麼（如果有的話）]
```

**這個階段是必經的。** 使用者閱讀後確認方向，才進入下一階段。如果使用者說「方向對，開始」，才開始實作。

### 階段 4：實作

**目標：按計畫執行，不多不少**

- 讀取目標檔案
- 做指定改動
- 如有需要，同時寫測試
- 每個 commit 有一個明確的 change
- 過程中如果發現 scope 需要擴大，**停下來回報**，不等做完了才說

**產出：**乾淨的 git diff

### 階段 5：驗證

**目標：確認修乾淨了**

**第一步永遠是 `pytest --collect-only`** — 這條 0.5 秒，只 import + 收集，不跑任何 test 邏輯。它擋掉的是 **import-time 級別的炸**：dataclass 欄位順序錯、循環 import、模組級 NameError。LLM 寫 code 最容易踩這類雷，眼睛看 diff 看不出來，只有讓 Python 真的 import 一次才知道。

完整流程：
1. `pytest --collect-only` — import sanity（**必跑，必先跑**）
2. 跑相關測試：`python scripts/run_tests.py` 或 `pytest tests/test_<area>.py`
3. 確認沒有破壞其他功能（必要時跑全套）
4. UI / display 改動：寫死一組 sample input 跑一次 `print(...)`，眼睛看輸出長相
5. 確認所有 linter 通過（`ruff` / `ruff format`）
6. 確認類型檢查通過（如果有的話）

**驗證沒過 → 不能進入階段 6**。這是硬規則。

### 階段 6：產出

**目標：讓使用者可以立即行動**

根據需求產出：

**PR / commit 模式：**
```markdown
## {issue title}

**Fixes:** #{issue_number}
**Type:** {bug/enhancement/refactor/feature}

### 改了什麼
[1-2 句話]

### 怎麼改的
[具體描述，不只是「優化」或「修復」]

### 測試
- [測試名稱或描述]
- 結果：pass / fail
```

**直接 commit 模式：**
直接 commit，並在 commit message 引用 issue

---

## 🛠️ 工具使用策略

| 工具 | 使用時機 |
|------|---------|
| `read_file` | 讀取目標檔案 |
| `write_file` | 寫入修改後的內容 |
| `run_bash` | 跑 tests、linter、git diff |
| `list_dir` | 確認目錄結構 |
| `fetch_url` | 取得 issue 内容 |
| `spawn_agent` | 複雜功能需要 sub-agent 處理時（預先說明） |

**紀律：**
- read_file 不要超過 20 個檔案
- 寫入前要先說計畫（階段 3）
- 不要在沒有確認的情況下直接寫入

---

## 🐛 LLM 寫 code 易踩雷區

> 這些坑都不是「想得不夠深」造成的，是「沒實際跑過 code」造成的。
> 看 diff 看不出來，眼睛掃過去都很自然。但是 Python interpreter 一加載就炸。
> 動手前掃一眼這份清單，動手後 `pytest --collect-only` 兜底。

### 1. dataclass 加欄位 → 先讀全 class 確認 default 順序

Python 規則：**non-default 欄位必須在 default 欄位之前**。新加帶 default 的欄位，**插在所有 non-default 之後**。

```python
# ❌ 整個 module 不能 import：TypeError: non-default argument 'elapsed_ms' follows default argument
@dataclass
class TurnDone:
    tool_count: int
    cache_read_input_tokens: int = 0   # ← 帶 default
    elapsed_ms: float                   # ← non-default 跟在 default 後面，炸
    stop_reason: str = "complete"

# ✅
@dataclass
class TurnDone:
    tool_count: int
    elapsed_ms: float                   # ← 所有 non-default 先列
    cache_read_input_tokens: int = 0    # ← 帶 default 都放後面
    stop_reason: str = "complete"
```

### 2. f-string 字面量之間不要塞條件式

Python 的 implicit string concat 在 lex 階段就把相鄰字串黏起來，`if/else` 是後來才解析。所以 `"A" "B" if cond else ""` 會被吃成 `("A" "B") if cond else ""`，把左邊兩個字串都當條件式的 true 分支。

```python
# ❌ cond=True 時剩 "context |  cache 80%  |  "（沒 in/out、elapsed、tools）
#     cond=False 時剩 "{input}in / ..." 之後（沒 context bar）
text = (
    f"context {pct}%  |  "
    f"cache {x}%  |  " if cond else ""
    f"{input}in / {output}out  |  "
    f"{elapsed}s"
)

# ✅ 抽變數
cache_seg = f"cache {x}%  |  " if cond else ""
text = (
    f"context {pct}%  |  {cache_seg}"
    f"{input}in / {output}out  |  "
    f"{elapsed}s"
)
```

### 3. 跨 branch 引用的變數，定義要在引用之前

LLM 常常先寫 if-block 才回頭定義變數。Python 不會在 parse 時抓這個，要 runtime 跑到那條 branch 才會 NameError。

```python
# ❌ branch A 取到時 cache_tag 還沒定義
if detail_mode:
    embed.set_footer(f"...{cache_tag}")   # NameError 待爆
else:
    print(f"...{cache_tag}")
cache_tag = f"cache {pct}%" if pct > 0 else ""   # 太遲

# ✅ 把 cache_tag 計算搬到所有引用之前
cache_tag = f"cache {pct}%" if pct > 0 else ""
if detail_mode:
    embed.set_footer(f"...{cache_tag}")
else:
    print(f"...{cache_tag}")
```

### 4. 加新欄位的 accumulate vs replace 對齊鄰居

加一個跟既有欄位語義相關的新欄位（cache_read 跟 input_tokens、retry_count 跟 turn_count），**先看鄰居用 `=` 還是 `+=`**，跟著走。混用會讓分子分母不同基底，計算出的指標對不上人類直覺。

```python
# 既有 convention: input_tokens 用 replace（每次 LLM call 的 usage 已是累積值）
input_tokens = response.input_tokens   # replace

# ❌ 新欄位用 += 會在 multi-tool-call turn 累加，3 次 call 各 12k 變成 36k
cache_read_input_tokens += response.cache_read_input_tokens

# ✅ 對齊 input_tokens 的 replace 語意
cache_read_input_tokens = response.cache_read_input_tokens
```

CLAUDE.md 裡通常會註明這類 convention，動手前先 grep 一下 `replace semantics` / `accumulate`。

### 5. UI / display 改動先 render 一次

寫死一組 sample input 跑一次，眼睛看一秒，比解析 diff 快 100 倍。

```python
# 改完 status_bar 後：
python -c "from loom.platform.cli.ui import status_bar; \
  print(status_bar(0.5, 100, 50, 1234.0, 2, cache_hit_pct=80).plain); \
  print(status_bar(0.5, 100, 50, 1234.0, 2, cache_hit_pct=0).plain)"
```

少了哪個區段、多了哪個符號，眼睛比 unit test 還快。

### 6. 動手前 30 秒 — 強迫掃一遍鄰居

加新欄位前：搜尋同一個 class / 函式中**已存在的相關欄位**，看它們的順序、語意、命名 convention。新東西跟著做，不要自己另起爐灶。

```bash
grep -B2 -A5 "class TurnDone" loom/core/events.py
grep -B1 -A2 "input_tokens\b" loom/core/session.py | head -30
```

---

## ⚠️ 紀律提醒

- **先計畫後實作** — 跳過階段 3 是最大的浪費時間行為
- **Scope 是鐵律** — 只做約定好的範圍，不做「順便」
- **停下來比做完再說好** — 過程中發現 scope 需要擴大，立即回報
- **沒有測試的實作不完整** — 如果原本沒有測試，改動後要補充
- **自己先 review diff 再當作最後防線** — 每一次產出都先自己看一遍

---

## 💡 Skill Genome 整合提示

- 此技能由 Loom 的 **ProceduralMemory** 管理
- 觸發關鍵詞：「修」「改」「幫我寫」「debug」「review」「PR」「commit」「issue」
- 與 `systematic_code_analyst` 的搭配：
  - `systematic_code_analyst` 發現問題，說「這個模組有 design issue」
  - `loom_engineer` 接手，說「我來修這個 design issue」
- 每次成功產出 PR/commit，技能 confidence 提升
- 失敗的實作（bug 没修好、測試 fail）需要如實記錄
