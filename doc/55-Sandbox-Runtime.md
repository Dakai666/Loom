# Sandbox Runtime — Quest A Phase 4

> Issue #29 · doc/52 §1.2 · 採用 `anthropic-experimental/sandbox-runtime` (srt) 為 Layer 2 OS-level wall。
>
> 建立日期：2026-05-18

## 目的

把 Loom 從 policy/tripwire 級安全（`CommandScanner`）推進到 **OS kernel-level isolation**。`srt` 是 Anthropic 為 Claude Code 同類 agent 設計的 lightweight sandbox：

- macOS：`sandbox-exec`（Seatbelt profile）
- Linux：`bubblewrap` + network namespace
- 網路：proxy-based filtering（HTTP/HTTPS via HTTP proxy、TCP via SOCKS5）

不需要 Docker，預設 secure-by-default：寫入全擋、網路全擋，明確列才開洞。

## 為何選 srt（不選 Docker / Deno / firejail）

doc/52 §5.3 已鎖定。摘要：

| 選項 | 結論 |
|------|------|
| **srt** | ✅ 雙平台齊全、Anthropic 同類 agent 主推、active 維護、CLI + library 雙模式 |
| Docker | 重量級，per-call 啟容器太慢；對單機 dev 過度設計 |
| Deno `--allow-run` | 子程序逃逸是結構性問題，不適合包 bash |
| firejail | 僅 Linux，macOS 沒對應方案 |

## 安裝

```bash
npm install -g @anthropic-ai/sandbox-runtime
which srt   # 確認在 PATH 上
```

> Beta research preview，API 可能會變。若版本升級導致 schema 變動，調整
> `loom/core/security/sandbox_runtime.py` 中 `to_srt_json()` 即可，整合面只有這一處。

## 啟用方式

`loom.toml`：

```toml
[security.sandbox]
backend         = "srt"           # "none" | "srt"
allow_write     = [".", "/private/tmp"]
deny_read       = ["~/.ssh", "~/.aws", "~/.gnupg", ".env"]
allowed_domains = ["pypi.org", "files.pythonhosted.org", "api.github.com"]
```

預設 `backend = "none"` — 不裝 srt、不改設定，行為與舊版完全一致。

啟用後，session 啟動時若找不到 `srt` binary 會 **fail-closed** 並印出安裝指令，避免 silent downgrade。

## 路徑語意

| 寫法 | 展開 |
|------|------|
| `"~"` | 使用者家目錄 |
| `"."` | session workspace（self.workspace，通常是 cwd） |
| 絕對路徑 | 照原樣傳給 srt |

### macOS `/tmp` 坑 ⚠️

macOS 上 `/tmp` 是 `/private/tmp` 的 symlink。`allow_write = ["/tmp"]` 不會生效——要列 `/private/tmp`。

Spike 期實測：
```bash
# 設 allow_write = ["/tmp"]：
$ srt -c 'echo hi > /tmp/test.txt'
/bin/bash: /tmp/test.txt: Operation not permitted

# 設 allow_write = ["/private/tmp"]：
$ srt -c 'echo hi > /tmp/test.txt'   # 走 symlink，OK
hi
```

## Filesystem 優先順序

照 srt 原規則（README）：

| 操作 | 預設 | 規則 |
|------|------|------|
| **讀取** | 允許 | `allow_read` 覆蓋 `deny_read`（deny-then-allow） |
| **寫入** | 拒絕 | `allow_write` 是 allow-only（空陣列 = 完全禁寫） |

## 網路 allowlist

- `allowed_domains = []` ⇒ **完全沒網**
- 想開特定 domain：明確列上
- `denied_domains` 額外擋（在 allowlist 內挖洞）

`srt` 內部跑 HTTP proxy + SOCKS5 proxy，sandbox 內所有 TCP 都被導向 proxy；不在 allowlist 內會回 403 / Connection blocked。

## 層次分工

```
┌─────────────────────────────────────────────────┐
│ Layer 1  CommandScanner / SelfTermGuard         │  ← tripwire / audit signal
│          (loom/core/security/command_scanner.py)│     regex-based, fail open
├─────────────────────────────────────────────────┤
│ Layer 2  Sandbox Runtime (srt)                  │  ← OS-level wall (本文件)
│          (loom/core/security/sandbox_runtime.py)│     opt-in, fail closed
└─────────────────────────────────────────────────┘
```

Layer 1 永遠 on，留審計訊號；Layer 2 opt-in，啟用後是真實隔離。`#214 (CommandScanner whitelist mode)` 已被 srt 的 allow-only filesystem + 網路 allowlist 吸收，關閉不做。

## 效能

Spike 期實測 macOS（M-series）：

| 操作 | 延遲 |
|------|------|
| srt 冷啟動（`srt -c 'echo hi'`） | ~120ms |
| 設定檔渲染 + 讀取 | <1ms（per-session 寫一次） |

每次 `run_bash` 都會付 ~120ms 啟動稅。對互動使用沒感覺，對 batch / autonomy 大量呼叫要留意。

## 整合架構

```
loom.toml [security.sandbox]
        │
        ▼
SandboxSettings.from_config()     ← loom/core/security/sandbox_runtime.py
        │
        ▼
make_run_bash_tool(sandbox=...)   ← loom/platform/cli/tools.py
        │
        ├─ srt_available() check → 缺失就 raise RuntimeError
        │
        ├─ write_settings_file()  → 寫一份 JSON 到 $TMPDIR/loom-srt-<hash>.json
        │
        └─ _maybe_wrap(cmd)
              │
              ▼
       "srt -s <settings> -c <cmd>"  → asyncio.create_subprocess_shell
```

設定檔以 (workspace, pid) hash 為檔名，**session 內穩定**——不會每 call 散落新 tmp file。

## 不在本期 scope

| 項目 | 為何延後 |
|------|---------|
| ScopeGrant 動態改 settings | 等 autonomy 真有需要時做 follow-up |
| AgentLedger emit sandbox decisions | 等 Quest B Ledger 落地（已 ship）後另開 PR |
| 自動 `npm install srt` | 安裝由使用者掌控；fail-closed 提示比自動安裝更清晰 |
| Custom 違規 callback / audit log | srt 自己有 violation store（macOS），先觀察是否需要橋接 |

## 排錯

| 症狀 | 解 |
|------|----|
| `srt binary not found on PATH` | `npm install -g @anthropic-ai/sandbox-runtime` |
| `Invalid configuration in /tmp/loom-srt-*.json: filesystem.denyWrite: Required` | srt schema 更新了；對齊 `to_srt_json()` |
| 寫 `/tmp` 失敗 | 改寫 `/private/tmp`（macOS symlink） |
| `curl: (56) CONNECT tunnel failed, response 403` | domain 不在 `allowed_domains` 內 |
| 想暫時關沙盒除錯 | `backend = "none"`，重啟 session |

## 相關資料

- 上游 repo：https://github.com/anthropic-experimental/sandbox-runtime
- Anthropic 部落格：https://www.anthropic.com/engineering/claude-code-sandboxing
- Layer 1 模組：`doc/45b-Security-Module.md`、`doc/46-CommandScanner.md`
- 主線定位：`doc/52-主線與支線.md` §1.2 Quest A
