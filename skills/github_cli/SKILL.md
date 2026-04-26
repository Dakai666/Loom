---
name: github_cli
description: "GitHub CLI 工具技能。當使用者要求「建立 issue」、「建立 PR」、「用 gh」、「gh api」、「查 GitHub」、「發 issue 到 GitHub」時使用。"
tags: [github, cli, issues, pr, api, devtools, github-actions]
confidence: 0.95
first_applied: 2026-04-07
version: 4
---

# GitHub CLI 工具技能

透過 `gh` 命令與 GitHub API 互動，支援 issue/PR 建立、審查、API 查詢、repo 操作。

---

## 核心原則

1. **Scope 確認再執行** — 複雜操作先說計畫再執行，不邊做邊改。執行前必須說明具體操作步驟，格式如：「我將執行：1. comment, 2. close, 3. verify」
2. **所有 body 一律用 `--body-file`** — 先 write_file 寫入 `outputs/doc/`（workspace 內路徑，觸發 LegitimacyGuard），再用 `--body-file` 讀取。禁止 heredoc 或 `--body "文字"` 內嵌。適用於所有將結構化文字寫入 GitHub 的操作，包括 `gh issue create`、`gh pr create`、`gh pr comment`、`gh pr review --request-changes` 等。
3. **查 Label 再建 Issue** — 未知 label 先用 `gh api` 確認存在；不存在時先 `gh api POST` 建立，再建 Issue
4. **成功後必然 Verify** — `gh issue create` / `gh pr create` 成功後，立即執行 `gh issue view` / `gh pr view` 確認資源確實建立
5. **失敗要透傳** — error message 完整保留給使用者，不截斷
6. **`--jq` 不進 chain** — `gh api --jq` 在無匹配時 exit 5 會中斷 pipe chain；單獨執行不用 chain，或用 `|| true` 包裹

---

## 觾發時機

- 「發 issue」「建立 issue 到 GitHub」
- 「用 gh create pr」「gh api 查一下」
- 「幫我 review 這個 PR」「列出這個 repo 的 open issues」

---

## 常用操作指南

### 建立 Issue（含 Label）

**Step 1 — 確認 Label 存在（必要）**
```bash
# 查現有 labels，取出所有名稱
gh api repos/OWNER/REPO/labels --jq '.[].name' 2>/dev/null | grep -Fx "bug" || true
```
若不存在則建立（单独一条，不 chain）：
```bash
gh api repos/OWNER/REPO/labels --method POST \
  --field name="session" \
  --field color="fc2929" \
  --field description="Session-related issues" 2>&1
```
> **注意**：Label 颜色必须为 6 位 hex（不含 `#`）。不确定时用 `fc2929`（红色）即可。

**Step 2 — 寫 body 檔案**
```bash
# write_file 寫到 outputs/doc/（非 tmp/，避免 LegitimacyGuard 阻擋）
# 內容寫入後執行：
gh issue create --repo OWNER/REPO \
  --title "標題" \
  --body-file outputs/doc/issue_body.md \
  --label bug
```

**Step 3 — Verify（必要）**
```bash
# 從建立命令的輸出 URL 中取出編號，執行 view 確認
gh issue view 131 --repo OWNER/REPO --json number,title,labels 2>&1
```

### Label 建立與查詢

```bash
# 查所有 label（含 description，輸出乾淨）
gh api repos/OWNER/REPO/labels --jq '.[] | "\(.name)\t\(.description)"'

# 批量檢查多個 label（逐條，fail 不阻斷）
for label in bug session authorization circuit-breaker; do
  gh api repos/OWNER/REPO/labels --jq ".[] | select(.name == \"$label\") | .name" 2>/dev/null
done

# 建立單個 label
gh api repos/OWNER/REPO/labels --method POST \
  --field name="session" \
  --field color="0070c0" \
  --field description="Session-related issues"
```

### PR Comment / 審查

```bash
# PR 留言（body 用 --body-file）
gh pr comment 123 --repo OWNER/REPO --body-file outputs/doc/pr_comment.md

# 審查（comment / approve / request-changes）
gh pr review 123 --repo OWNER/REPO --approve
gh pr review 123 --repo OWNER/REPO --request-changes --body-file outputs/doc/review.md
```

### 建立 PR

```bash
# body 同樣用 --body-file
gh pr create --repo OWNER/REPO \
  --title "標題" \
  --body-file outputs/doc/pr_body.md \
  --base main

# Verify
gh pr view 456 --repo OWNER/REPO --json number,title,state
```

### 查 Issue

```bash
# 列出 open issues（不用 --jq pipe，單獨執行即可）
gh issue list --repo OWNER/REPO --state open --limit 20

# 查看特定 issue
gh issue view 131 --repo OWNER/REPO

# 搜尋
gh issue list --repo OWNER/REPO --search "keyword in:title" --state open
```

### gh api（直接呼叫 GitHub API）

```bash
# GET — --jq 单独执行，不 pipe（避免 exit 5 中斷链）
gh api repos/OWNER/REPO/labels

# GET + jq 过滤（单条执行，不用 pipe chain）
gh api repos/OWNER/REPO/issues --jq '.[] | "\(.number) \(.title)"' 2>&1 || true

# 分頁
gh api repos/OWNER/REPO/issues?state=open\&per_page=100

# POST 建立资源
gh api repos/OWNER/REPO/issues --method POST \
  --field title="標題" \
  --field body="內容"
```

### Repo 操作

```bash
# 查 repo
gh repo view OWNER/REPO

# 列出 branches
gh api repos/OWNER/REPO/branches --jq '.[].name' 2>&1 || true

# Fork
gh api repos/OWNER/REPO/forks --method POST 2>&1
```

---

## 輸出格式約束

### Issue / PR 建立成功後（Verify 前先口報）

```
✅ 正在建立…
URL: https://github.com/OWNER/REPO/issues/131
```

### Verify 完成後

```
✅ 已確認建立
#131 標題
標籤: bug, session
```

### PR 建立成功

```
✅ PR 已建立
URL: https://github.com/OWNER/REPO/pull/456
標題: <title>
狀態: <open|draft>
```

### 查詢類操作

Markdown 表格：編號 / 狀態 / 標題 / 標籤 / 負責人

### 失敗時

完整輸出 `gh` 的 error message，說明原因與處理方式。

---

## 環境依賴

| 環境變數 | 用途 | 必要 |
|----------|------|------|
| `GH_TOKEN` / `GITHUB_TOKEN` | 認證 | 是 |
| `GH_REPO` | 預設 repo | 建議 |

---

## 常見失敗處理

| 錯誤訊息 | 原因 | 處理方式 |
|----------|------|---------|
| `Authentication failed` | token 無效 | 檢查 `gh auth status` |
| `'xxx' not found`（label） | label 尚未建立 | 先 `gh api POST /labels` 建立 |
| `Validation Failed` | 欄位格式錯誤 | 檢查 `--title` 長度、label 格式 |
| `conflict` | PR branch 衝突 | fetch + rebase |
| `network` | 網路問題 | 重試一次 |
| `gh: API cannot be queried` | 未授權 | `gh auth refresh` |

---

## 限制與禁区

1. **不自動刪除資源** — 刪除需使用者明確授權
2. **不繞過付費牆** — rate limit 超出時誠實告知
3. **PR body 不截斷** — GitHub PR body 無長度限制

---

## 環境已知問題（v3 新增）

### Exit Code 127 啞巴成功（Loom 特定）

`bash -lc gh ...` 在成功時可能仍回 exit code 127，造成 retry 迴圈重複發送。

緩解：
- `--body-file` 写文件后立即执行一次，成功即停
- 不依賴 exit code 判斷，用 `gh issue view` / `gh pr view` 驗證

### `--jq` 無匹配時 Exit 5 中斷 Chain

`gh api --jq '...'` 在無匹配輸出時回 exit 5，無 error message，直接中斷 pipe chain。

緩解：
- `gh api --jq` 单独执行，不用 pipe chain
- 或加 `|| true` 包裹，但这样会掩盖真实错误
- 查列表類用 `gh api repos/.../labels` + Python/手動掃描，不用 `--jq` pipe

---

*v4（2026-04-25）：Core Principles 新增第1條操作計畫格式；原則 #2 適用範圍拓寬至所有 body 寫入操作（issue/PR 建立、comment、review），不再只限於建立類指令。
