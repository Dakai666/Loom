# CommandScanner — Shell 注入掃描

> Issue #100 / Issue #165 — 防禦縱深的 tripwire 與 audit signal。

---

## 定位

`CommandScanner` 是 Loom 安全模組的第二層（位於 `loom/core/security/command_scanner.py`），補足 `SelfTerminationGuard`（#98）只檢查自終止模式的不足。

設計目標：**防禦縱深（defense-in-depth）的 tripwire 與 audit signal**，不是 security boundary。

```
┌──────────────────────────────────────────────────────────┐
│  Layer 1: TrustLevel / BlastRadiusMiddleware             │
│           （policy — 決定是否需要確認）                   │
├──────────────────────────────────────────────────────────┤
│  Layer 2: CommandScanner + SelfTerminationGuard           │
│           （tripwire — 檢測危險 pattern，audit log）       │
├──────────────────────────────────────────────────────────┤
│  Layer 3: Sandbox (Issue #29)                            │
│           （actual wall — OS/container isolation）         │
│           目前尚未實作，此為長程目標                        │
└──────────────────────────────────────────────────────────┘
```

**重要警告**：RegEx sanitization 可透過 obfuscation（`w""g""e""t`、base64-staged scripts、here-docs 寫入 intermediate file）繞過。不要依賴 CommandScanner 作為安全邊界。

---

## Pattern 行為分級

| Pattern Key | Verdict | 說明 |
|-------------|---------|------|
| `pipe_to_shell` | **BLOCK** | curl/wget piped to shell interpreter |
| `bash_tcp` | **BLOCK** | `>&/dev/tcp/` 或 `</dev/tcp/`（potential reverse shell）|
| `encoded_exec` | **BLOCK** | base64 decode piped to shell |
| `chained_destructive` | **BLOCK** | chained destructive targeting root or home |
| `exfil_env` | **BLOCK** | curl/wget referencing sensitive env var |
| `heredoc_exec` | **WARN** | interpreter followed by `<<` |
| `cmd_sub_env` | **WARN** | command substitution accessing sensitive env var |

### BLOCK 的意義

當 `check()` 返回 `verdict="block"` 時：
- `is_blocked = True`
- `run_bash` executor 應該 `raise PermissionError`
- 工具不回傳 `ToolResult`，而是例外向上傳播（由 LifecycleMiddleware 的例外保護捕獲，轉為 failed ToolResult）

### WARN 的意義

當 `check()` 返回 `verdict="warn"` 時：
- `is_blocked = False`
- 發出警告但不自動阻止
- 寫入 audit log
- 適用於：heredoc execution（可能是 legitimate 使用場景）、env var 存取（可能正常也可能不正常）

---

## API

```python
scanner = CommandScanner()  # 無狀態，可 reuse

verdict = scanner.check(command: str) -> GuardVerdict
scanner.is_allowed(command)  # True only when verdict == "allow"
scanner.is_blocked(command)  # True when verdict == "block"
```

### GuardVerdict dataclass

```python
@dataclass(frozen=True)
class GuardVerdict:
    is_blocked: bool       # True → MUST NOT execute
    verdict: str            # "allow" | "warn" | "block"
    pattern_key: str       # 觸發的 pattern 名稱
    description: str       # 人類可讀的說明，包含 matched string
```

---

## 與 SelfTerminationGuard 的分工

| | SelfTerminationGuard (#98) | CommandScanner (#165) |
|---|---|---|
| **目標** | 防止 agent 終止 Loom 自己 | 防止 shell injection / exfiltration |
| **Pattern 數量** | 5 個 | 7 個 |
| **Block 行為** | block self-termination（pkill loom 等）| block injection patterns |
| **位置** | `loom/core/security/self_termination_guard.py` | `loom/core/security/command_scanner.py` |
| **WARN patterns** | persistence mechanisms（authorized_keys、crontab 等）| heredoc、cmd_sub_env |

### 共同使用的 GuardVerdict

兩個模組都回傳 `GuardVerdict`（定義在 `self_termination_guard.py`），讓調用方有統一的介面：

```python
# run_bash tool executor 內
self_termination_verdict = self_termination_guard.check(command)
if self_termination_verdict.is_blocked:
    raise PermissionError(f"Self-termination blocked: {self_termination_verdict.description}")

injection_verdict = self._command_scanner.check(command)
if injection_verdict.is_blocked:
    raise PermissionError(f"Injection blocked: {injection_verdict.description}")
```

---

## 與 Lifecycle 的整合

`CommandScanner` 的結果寫入 `call.metadata["security_scan"]`，`LifecycleMiddleware` 在 `MEMORIALIZED` 階段將此寫入 action record 的 metadata，供 audit log 使用：

```python
# 工具 executor 內（BlastRadiusMiddleware 之後執行）
verdict = self._command_scanner.check(command)
call.metadata["security_scan"] = {
    "verdict": verdict.verdict,
    "pattern_key": verdict.pattern_key,
    "description": verdict.description,
    "blocked": verdict.is_blocked,
}
if verdict.is_blocked:
    raise PermissionError(f"Blocked: {verdict.description}")
```

Audit log 寫入時：
```sql
INSERT INTO audit_log (tool_name, error, details, created_at)
VALUES ('security:scan', NULL, '{"pattern": "pipe_to_shell", "command": "..."}')
```

---

## 設計原則

1. **Stateless**：所有 pattern 是 module-level 常數，scanner 實例無內部狀態，可任意 reuse
2. **零外部依賴**：CommandScanner 不依賴 DB、網路或任何 state
3. **Block ≠ 阻止一切**：真正的安全牆是 sandbox（#29），Scanner 只是 tripwire
4. **不可依賴混淆防護**：Obfuscation 可繞過 regex，要用實際的 OS isolation（#29）
5. **WARN 不自動 block**：允許 legitimate 使用場景（如 heredoc 正常寫入檔案）

---

## 限制（Known Limitations）

1. **Pipe ambiguity**：`curl http://a.com/file | python -` 在法律上合理，但可能被 block
2. **Variable expansion ambiguity**：`echo $HOME` 正常，但 `$SECRET` 存取敏感 env 會被 warn
3. **No command parsing**：Scanner 基於 regex，不理解 shell 語法。無法處理 `eval "$cmd"` 類動態執行
4. **Bash TCP pattern 是狹義的**：只匹配 `/dev/tcp/`，不匹配其他 reverse shell technique（如 `nc -e`）
5. **Base64 只檢查 `base64 -d` / `base64 --decode`**：其他 decode 方式（如 python base64 module）不會被檢測

---

*文件草稿 | 2026-04-26 03:10 Asia/Taipei*