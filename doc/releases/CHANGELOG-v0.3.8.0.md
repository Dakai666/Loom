# Changelog — v0.3.8.0

> Release Date: 2026-05-21
> Previous: v0.3.7.5 (06d4a1f, 2026-05-19)
> Commits: 39
> Range: v0.3.7.5..HEAD

## Highlights

### 🌟 Interaction Language — UI/UX 全面翻新
最大幅度的視覺改造。Envelope intent contract layer 上線，CLI runtime heartbeat 即時反馈，Discord 完整 render envelope intent 與 outcome，stalled-status proxy 自動暫停。設計文件見 `doc/00-Interaction-Language-Framework.md`。

### ✅ TaskList v2 — done_when Acceptance Criterion
每個 task_write todo 現在可選 `done_when` 字串——agent 自己寫的驗收標準。CLI floating panel、Discord reminder、self-check message 同步 render completed rows 隱藏 criterion，空值顯示 `—` 製造視覺對比壓力。無 validator：纪律來自重讀自己寫的東西。

### 🧹 三輪系統審計完成
Audit-C（確認-dead imports）、Audit-A r2（閒置 feature 殘留）、Audit-B（cli/tools.py 重構 + self_reflection layer fix）。Textual TUI _subsystem 完全退役，資源集中 CLI + Discord。

### 🧹 Memory Hygiene — 批量清理
新增 batch dedup script，執行後 semantic facts 從 6.6k 降至 5.7k。Governance 層加入 semantic-dup detector + time-window admission gate，寫入時即時偵測近似重複。

## Features

- **feat(cli)** `7a45978` `#434` — lift tool justification into prose（justification 非同步顯示為 dedicated line）
- **feat(cli)** `00712a8` `#418+#419` — CLI runtime heartbeat 即時反馈
- **feat(cli)** `d7e02c1` `#416+#417` — interaction-language foundation
- **feat(cli)** `bebe042` `#411` — heartbeat min-dwell so short tools don't flash
- **feat(cli)** `bebe042` `#412` — force-clear hard-boundary heartbeats
- **feat(discord)** `3278a5f` `#431` — wire envelope metadata producers
- **feat(discord)** `145eb88` `#422+#429` — stalled-status proxy with pause suppression
- **feat(discord)** `05bd937` `#420+#427` — render envelope intent and outcome
- **feat(ui)** `6e05a03` `#421+#428` — derive envelope outcomes
- **feat(tasks)** `7d7d22c` `#437` — add done_when acceptance criterion to TaskList
- **feat(prompt)** `b730aa9` `#423+#430` — envelope intent contract layer
- **feat(memory)** `dd69bfa` `#411` — semantic-dup + time-window admission gate
- **feat(memory)** `a71319f` `#398+#409` — surface near-duplicate hint at write time
- **feat(memory)** `5c8cfb7` `#410+#413` — batch dedup script + 6.6k→5.7k cleanup
- **feat(governance)** `dd69bfa` `#411` — time-window admission gate
- **feat(config)** `0e90e7d` `#306+#397` — extract load_loom_config to public API
- **feat(sandbox)** `c67550c` `#402+#405` — bypass_sandbox profile flag for TLS-incompatible CLIs
- **feat(skill)** `13c5fb1` `#410+#413` — memory_hygiene dedup script
- **feat(skills)** `skills/memory_hygiene/` — new skill: memory_hygiene with checks.py
- **feat(skills)** `loom/platform/cli/skill_tools.py` — new: skill tool generation layer
- **refactor(tui)** `6e7033a` `#404` — retire Textual TUI subsystem, focus on CLI + Discord
- **refactor(audit-A)** `04e8833` `#401` — retire residue: record_outcome, stale TODO, test name

## Fixes

- **fix(cli)** `bebe042` `#412` — heartbeat min-dwell prevents flash on fast tools
- **fix(cli)** `bebe042` `#411` — force-clear hard-boundary heartbeats
- **fix(sandbox)** `c67550c` `#402+#405` — TLS-incompatible CLI bypass via profile flag
- **fix(memory)** `13c5fb1` `#410+#413` — align dedup default cosine to 0.85

## Tests

- **test(cli)** `tests/test_app.py` +335 lines — TaskList panel + done_when render coverage
- **test(cli)** `tests/test_tasklist.py` +66 lines — done_when round-trip, whitespace strip
- **test(cli)** `tests/test_tool_begin_line.py` +52 lines — tool justification prose
- **test(discord)** `tests/test_discord_interaction_language.py` +530 lines — envelope intent/outcome render
- **test(governance)** `tests/test_governance.py` +203 lines — admission gate + semantic-dup
- **test(ledger)** `tests/test_ledger_envelope_projector.py` +392 lines — envelope projector update
- **test(memory)** `tests/test_memory_search.py` +120 lines — search regression
- **test(sandbox)** `tests/test_run_bash_sandbox_profiles.py` +58 lines — profile grant flows
- **test(sandbox)** `tests/test_sandbox_runtime.py` +25 lines — sandbox runtime coverage

## Docs & Chores

- **docs** `924118c` `#424+#432` — close UI interaction language implementation decisions
- **docs** `8ec8eeb` `#415` — remove 3 confirmed-dead imports
- **docs** `f721bf9` `#407` — audit-B: cli/tools.py reorg + self_reflection layer fix
- **chore(config)** `0e90e7d` `#306+#397` — load_loom_config public API extraction

## Breaking Changes

- Textual TUI (`loom/platform/cli/tui/`) 已移除。CLI 聚焦 prompt_toolkit，Discord 平台獨立。
- `record_outcome` 已移除（audit-A）。
- 少數測試因架構調整而重構（test_confirm_parity, test_prompt_stack 等）。

## Verification

```bash
git tag --list 'v0.3.8*'                  # v0.3.8.0 存在
git ls-remote --tags origin 'v0.3.8.0'    # 遠端同步
gh release view v0.3.8.0 --repo Dakai666/Loom  # GitHub Release 可見
```