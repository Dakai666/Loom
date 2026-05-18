# Release Workflow — 發佈流程

**觸發後自動載入本檔案。**

適用於 release、tag、changelog、GitHub Release。此情境偏操作風險高；
每個外部寫入動作都必須先產生 body/notes file，成功後立即 verify。

---

## 鐵律

```text
NO RELEASE WRITE WITHOUT RANGE CONFIRMATION
NO GITHUB WRITE WITHOUT BODY-FILE AND POST-WRITE VERIFY
NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE
```

---

## 成功定義

- **產出**：確認過的 commit range + changelog/release notes + tag + GitHub Release + verification evidence
- **品質指標**：版本號語義清楚；release notes 說明價值；所有外部狀態可查
- **驗收方式**：tag 與 release 頁面可見，內容對應本次 range

---

## 工作流程（五階段）

### 階段 1：範圍確認

必要資訊：
- previous tag 或 base commit
- target version
- 是否有必須強調的 highlights

命令：
```bash
git describe --tags --abbrev=0
git log {previous}..HEAD --oneline
```

產出：
```markdown
## Release Scope

**Previous:** [tag/commit]
**Target:** [version]
**Commit count:** [count]
**Range:** `{previous}..HEAD`
**Needs confirmation:** [任何不確定點]
```

### 階段 2：Commit 分析

```bash
git log {previous}..HEAD --format="%h %ad %s" --date=short
git diff --stat {previous}..HEAD
```

分類：
- Highlights：使用者會感受到的核心變化
- Features
- Fixes
- Docs / Chores / Tests
- Breaking changes（若有）

### 階段 3：撰寫 Changelog / Release Notes

先寫檔案，不直接塞 CLI 參數。

檔名建議：
```text
CHANGELOG-{VERSION}.md
```

格式：
```markdown
# Changelog — {VERSION}

> Release Date: {YYYY-MM-DD}
> Previous: {PREVIOUS}
> Commits: {COUNT}

## Highlights

### {Title}
[說明解決什麼問題、使用者得到什麼]

## Changes

- [type] [commit] [human-readable summary]

## Fixes

- [bug and impact]

## Verification

- [commands run before release]
```

### 階段 4：Release 執行

執行前確認：
- changelog 已寫入檔案
- working tree 狀態符合預期
- target version 沒有已存在 tag/release

命令：
```bash
git tag -a {VERSION} -m "Release {VERSION}"
git push origin {VERSION}
gh release create {VERSION} --title "{VERSION}" --notes-file CHANGELOG-{VERSION}.md
```

### 階段 5：Verification Gate

讀 `contexts/verification.md`，至少跑：

```bash
git tag --list {VERSION}
git ls-remote --tags origin {VERSION}
gh release view {VERSION} --repo {owner}/{repo}
```

只有三者都確認後，才可宣稱 release 完成。

---

## 交付格式

```markdown
## Release Result

**Version:** {VERSION}
**Range:** `{previous}..HEAD`
**Changelog:** `CHANGELOG-{VERSION}.md`
**Verification:**
- `git tag --list {VERSION}` — [result]
- `git ls-remote --tags origin {VERSION}` — [result]
- `gh release view {VERSION}` — [result]
**URL:** [release URL]
```

---

## 紀律提醒

- 不確認 range 不 tag。
- 不用 inline release notes；一律 notes file。
- 成功後必查遠端 tag 與 GitHub Release。
- 如果 verification 失敗，只能說「本地動作完成但遠端狀態未確認」。

---

*Code_Weaver 發佈流程情境 · v3.0 — 2026-05-19*
