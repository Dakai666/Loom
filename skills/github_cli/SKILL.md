---
name: github_cli
description: "GitHub CLI 工具技能。當使用者要求「建立 issue」、「建立 PR」、「用 gh」、「gh api」、「查 GitHub」、「發 issue 到 GitHub」時使用。"
tags: [github, cli, issues, pr, api, devtools, github-actions]
confidence: 0.85
first_applied: 2026-04-07
version: 1
---

# GitHub CLI 工具技能

透過 `gh` 命令與 GitHub API 互動，支援 issue/PR 建立、審查、API 查詢、repo 操作。

---

## 核心原則

1. **Scope 確認再執行** — GitHub 操作影響共享狀態，複雜操作（建立 PR、審查、多行 body）先說計畫再執行
2. **用 `--body-file` 代替 heredoc** — 複雜內容（多行、特殊字元、JSON）一律用 `--body-file`，避免 shell 展開造成格式破壞
3. **先讀再寫** — 建立 issue/PR 前先查現有狀態，避免重複
4. **Scope 內最小改動** — 只操作指定 repo/issue/PR，不擴大範圍
5. **失敗要透傳** — `gh` 命令失敗時，error message 完整保留給使用者，不截斷

---

## 觸發時機

使用以下關鍵句時主動載入：
- 「發 issue」
- 「建立 issue 到 GitHub」
- 「用 gh create pr」
- 「gh api 查一下」
- 「幫我 review 這個 PR」
- 「列出這個 repo 的 open issues」
- 「用 GitHub CLI」

---

## 常用操作指南

### 建立 Issue

```bash
# 基本語法
gh issue create --repo OWNER/REPO --title "標題" --body "內容"

# 複雜內容 → 用 --body-file（絕對首選）
echo "## 描述\n\n詳細內容..." > /tmp/issue_body.md
gh issue create --repo OWNER/REPO --title "標題" --body-file /tmp/issue_body.md

# 加入 label
gh issue create --repo OWNER/REPO --title "標題" --body-file /tmp/body.md --label bug,help-wanted

# 加入 assignee
gh issue create --repo OWNER/REPO --title "標題" --body-file /tmp/body.md --assignee @me
```

### 查 Issue

```bash
# 列出 open issues
gh issue list --repo OWNER/REPO --state open --limit 20

# 查看特定 issue
gh issue view 123 --repo OWNER/REPO

# 搜尋
gh issue list --repo OWNER/REPO --search "keyword in:title" --state open
```

### 建立 PR

```bash
# 基本語法
gh pr create --repo OWNER/REPO --title "標題" --body "描述"

# 複雜內容 → --body-file
gh pr create --repo OWNER/REPO --title "標題" --body-file /tmp/pr_body.md --base main

# 指定 reviewer
gh pr create --repo OWNER/REPO --title "標題" --body-file /tmp/pr_body.md --reviewer username1,username2

# Draft PR
gh pr create --repo OWNER/REPO --title "標題" --body-file /tmp/pr_body.md --draft
```

### Review PR

```bash
# 審查 PR
gh pr review 123 --repo OWNER/REPO --comment --body "審查意見"

# Approve / Request changes / Comment
gh pr review 123 --repo OWNER/REPO --approve
gh pr review 123 --repo OWNER/REPO --request-changes --body "需要修改的原因"

# 列出已審查的 PR
gh pr list --repo OWNER/REPO --state open --review=required
```

### gh api（直接呼叫 GitHub API）

```bash
# GET 查詢
gh api repos/OWNER/REPO
gh api repos/OWNER/REPO/issues --jq '.[] | "\(.number) \(.title)"'

# 分頁處理
gh api repos/OWNER/REPO/issues?state=open&per_page=100

# POST 建立資源
gh api repos/OWNER/REPO/issues --method POST --field title="標題" --field body="內容"
```

### Repo 操作

```bash
# 查 repo 資訊
gh repo view OWNER/REPO

# 列出所有 branches
gh api repos/OWNER/REPO/branches --jq '.[].name'

# Fork
gh repo fork OWNER/REPO --clone
```

---

## 輸出格式約束

### Issue 建立成功時

回報格式：
```
✅ Issue 已建立
URL: https://github.com/OWNER/REPO/issues/編號
標題: <title>
```

### PR 建立成功時

```
✅ PR 已建立
URL: https://github.com/OWNER/REPO/pull/編號
標題: <title>
狀態: <open|draft>
```

### 查詢類操作

以 Markdown 表格呈現，欄位：
- `編號` / `狀態` / `標題` / `標籤` / `負責人`

### 失敗時

直接輸出完整 `gh` 的 error message，不截斷，並說明：
- 失敗原因（若從 error 可推斷）
- 使用者可以怎麼處理

---

## 環境依賴

| 環境變數 | 用途 | 必要 |
|----------|------|------|
| `GH_TOKEN` 或 `GITHUB_TOKEN` | 認證 | 是 |
| `GH_REPO` | 預設 repo（`owner/repo` 格式） | 建議設定 |

---

## 常見失敗處理

| 錯誤訊息 | 原因 | 處理方式 |
|----------|------|---------|
| `Authentication failed` | token 無效或未設定 | 提示使用者檢查 `gh auth status` |
| `Resource not found` | repo/issue 不存在 | 確認 repo 名稱或 issue 編號是否正確 |
| `Validation Failed` | 欄位格式錯誤 | 檢查 `--title` 長度或 `--label` 格式 |
| `conflict` | PR 目標 branch 有衝突 | 建議先 fetch 並 rebase |
| `network` | 網路問題 | 重試一次 |

---

## 限制與禁区

1. **不自動刪除任何資源** — 刪除 issue/PR/branch 需使用者明確授權
2. **不繞過付費牆** — GitHub API 有 rate limit，超過時誠實告知使用者
3. **不修改他人的 repo** — 除非 `--repo` 明確指定或已是 fork
4. **PR body 不做 markdown 截斷** — GitHub PR body 沒有長度限制
