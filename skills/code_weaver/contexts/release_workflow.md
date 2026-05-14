# Release Workflow — 發佈流程

**觸發後自動載入本檔案。**

---

## 成功定義

- **產出**：完整 CHANGELOG + Git Tag + GitHub Release
- **品質指標**：Commit 範圍精準、亮點敘述清晰、版本號語義化
- **驗收方式**：Release 頁面可見、CHANGELOG 格式一致、Tag 已推送

---

## 工作流程（五階段）

### 階段 1：範圍確認

目標：確認發佈範圍與上一個版本的基準點。

**必要資訊：**
- 上一個版本 tag（例：`v0.3.7.2`）
- 或上一個版本的 commit hash（例：`e42c3d8`）
- 目標版本號（例：`v0.3.7.3`）

```bash
# 取得上一個版本的 commit hash
git describe --tags --abbrev=0
# 或直接指定
git log v0.3.7.2..HEAD --oneline | wc -l
```

產出：
1. 上一個版本的 tag/commit
2. 目標版本號
3. 詢問：「這次有特别想強調的功能亮點嗎？」

### 階段 2：Commit 分析

目標：系統化整理從上一版以來的所有變更。

```bash
# 取得所有 commits（依時間排序）
git log {上一版}..HEAD --format="%h %ad %s" --date=short

# 統計數量
git log {上一版}..HEAD --oneline | wc -l

# 分類統計
git log {上一版}..HEAD --format="%s" | \
  grep -E "^(feat|fix|refactor|chore|docs|test)" | \
  sed 's/:.*//' | sort | uniq -c | sort -rn
```

**分析重點：**
1. **核心亮點（Highlights）**：這次最重要的功能，用 1-2 句話說明價值
2. **功能分類**：依類型分組（feat / fix / refactor / chore / docs）
3. **Bug Fixes**：所有 fix 類型的 commit
4. **Breaking Changes**：確認是否需要 major version bump

產出：結構化的變更摘要

### 階段 3：CHANGELOG 撰寫

目標：產出符合格式的 changelog 檔案。

**CHANGELOG 格式模板：**

```markdown
# Changelog — {VERSION}

> Release Date: {YYYY-MM-DD}
> Previous: {上一版} ({上一版 commit})
> Commits: {數量}

---

## 🌟 Highlights

### {功能名稱} — {一句話描述}
[2-3 句話說明這個功能解决了什麼問題、带来什麼價值]

**使用方式（若適用）：**
```bash
# CLI
loom {command}

# Config
[[autonomy.schedules]]
name = "..."
```
```

**撰寫原則：**
1. **亮點功能要詳細**：包含價值說明、使用方式、程式碼層面的組件說明
2. **一般功能用表格**：快速羅列，節省篇幅
3. **Bug Fix 要說修复了什麼**：不只是「修復」，要說「修復了 X 在 Y 情况下 Z 的問題」
4. **保持一致性**：對照上一次發佈的 CHANGELOG 格式

**檔案命名：**
```bash
CHANGELOG-{VERSION}.md  # 例如：CHANGELOG-v0.3.7.3.md
```

產出：
1. `CHANGELOG-{VERSION}.md` 檔案
2. 向使用者展示草稿，確認內容正確

### 階段 4：Release 執行

目標：建立 Git Tag + GitHub Release。

**步驟：**

```bash
# 1. 建立 Annotated Tag
git tag -a {VERSION} -m "Release {VERSION}

🌟 Highlights:
• {功能 1} — {一句話}
• {功能 2} — {一句話}

📦 Full Changelog:
https://github.com/{owner}/{repo}/blob/main/CHANGELOG-{VERSION}.md"

# 2. Push Tag
git push origin {VERSION}

# 3. 建立 GitHub Release
gh release create {VERSION} \
  --title "{VERSION} — {版本標題}" \
  --notes-file CHANGELOG-{VERSION}.md

# 4. Verify
gh release view {VERSION} --repo {owner}/{repo}
```

產出：
1. ✅ Tag 已建立
2. ✅ Tag 已推送
3. ✅ GitHub Release 已建立
4. ✅ Release URL

### 階段 5：事后記錄

目標：將發佈流程經驗沉澱為長期記憶。

```markdown
# 發佈記憶
relate(subject='loom', predicate='last_release', object='{VERSION}')
relate(subject='loom', predicate='release_commits', object='{COUNT}')
```

產出：持久化的發佈記錄

---

## 版本號規範（Semantic Versioning）

| 類型 | 情境 | 範例 |
|------|------|------|
| **Patch** `x.y.Z` | Bug Fixes only | v0.3.7.2 → v0.3.7.3 |
| **Minor** `x.Y.z` | 新功能（向後相容） | v0.3.7 → v0.4.0 |
| **Major** `X.y.z` | Breaking Changes | v0.3.7 → v1.0.0 |

---

## 觸發關鍵詞

- 「發佈」「release」「tag」「v0.x.x」
- 「更新版本」「新版本」「上線」
- 「整理 changelog」「幫我發佈」
- 「幫我 tag」「幫我 release」

---

## 紀律提醒

- **亮點功能要詳細說明**：不只是功能名稱，要說解决了什麼問題
- **Tag message 要簡潔**：GitHub Release 會顯示，這是給使用者的第一印象
- **所有變更都要列入 CHANGELOG**：即使是 chore 或 test
- **成功後必然 Verify**：確認 Release URL 可訪問
