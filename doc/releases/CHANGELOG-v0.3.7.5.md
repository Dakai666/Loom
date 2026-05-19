# Changelog — v0.3.7.5

> Release Date: 2026-05-19
> Previous: v0.3.7.4 (ff6e26c, 2026-05-17)
> Commits: 21

## Highlights

### 🏖️ Sandbox Runtime 就緒（Quest A Phase 4 完成）
`run_bash` 現在跑在 `srt` sandbox（macOS: `sandbox-exec`）下，隔離寫入與網路。
CLI profile 機制讓你視需要擴充允許清單，不用每次全部開放。

### ⏱️ Context Budget 維度擴充
新增 **block-count** 維度，並在抵達 80%/90% threshold 時注入 drift hooks，
不再是只有 token count 的單一監控視角。

### 🕐 時區全域一致性（Issue #388）
memory 工具、Discord 通知、ledger 時間戳現在都跟隨 `Asia/Taipei` 走，
不再出現 UTC/Taipei 混亂。

### 📖 Doc Integrity CI Layer 1 + Layer 3
文件保麗龍系統 Phase 5 完成，新增雙層 CI 檢查，文件一致性追蹤自動化。

### 🧵 Ledger 敘事召回
`ledger_recall` 現在用 narrative 方式重現 agent 思維過程，
不再只是冰冷的事件列表。

## Features

- **feat(sandbox)** `c828d87` `#396` — add CLI sandbox profiles + request + apply authorized profiles
- **feat(skill)** `ec975df` — news-aggregator checks.py 新增 `scripts_only_bash` precondition
- **feat(skill)** `40b1967` `#366` — renovate meta-skill-engineer 三通道文件（doc/54）
- **feat(ledger)** `3197e33` `#385` — ledger_recall narrative recall
- **feat(security)** `005bb1d` `#29` — srt sandbox-runtime backend for run_bash
- **feat(docs)** `7953abb` `#313` — Doc Integrity CI Layer 1 + Layer 3（Phase 5 / Quest E）
- **feat(docs)** `578eda2` `#314` — Capability Sheet auto-projection（與 #365 同邏輯）

## Fixes

- **fix(budget)** `c828d87` `#396` — add block-count dimension + drift hooks for context %
- **fix(harness)** `0452137` — make scope once approvals single-use
- **fix(skill_review)** `5e2b420` `#393` — cross-turn activation window + inferred unload boundary
- **fix(timezone)** `e5fa602` `#388` — honor user zone across memory tools, Discord, ledger

## Tests

- **test(sandbox)** `808e7a9` — cover profile grant flows

## Docs & Chores

- **docs(skills)** `e55365a` — update code_weaver v3
- **docs(sandbox)** `352d154` — document CLI profile grants
- **docs(sandbox)** `c3e08ea` — plan profile scope grants
- **docs(sandbox)** `701738d` — design profile scope grants
- **docs** `6618a16` — remove stale memory health draft footer
- **docs** `26fb632` — rewrite stale integrity debt
- **docs** `675c953` — `#390` 迴紋針問題修復文件

## Breaking Changes

無。

## Verification

```bash
git tag --list 'v0.3.7*'   # v0.3.7.5 存在
git ls-remote --tags origin 'v0.3.7.5'   # 遠端同步
gh release view v0.3.7.5 --repo Dakai666/Loom   # GitHub Release 可見
```