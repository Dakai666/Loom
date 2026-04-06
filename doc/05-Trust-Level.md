# Trust Level（信任等級）

Trust Level 是 Loom 工具安全模型的基礎。每個工具在註冊時都會被指定一個信任等級，決定它的執行是否需要人類確認。

從 v0.2.3.4 起，`TrustLevel` 三級制之上增加了 `ToolCapability` 旗標系統，讓框架可以對「同屬 GUARDED 但風險截然不同」的工具做出更細緻的區分。

---

## 三級信任（TrustLevel）

### SAFE（安全）

**定義**：唯讀、本地、可逆。

這些操作風險極低，session 開始時已預授權，不需要每次確認。

**典型工具**：`read_file`, `list_dir`, `recall`, `query_relations`, `fetch_url`

**執行流程**：
```
Agent 請求 → MiddlewarePipeline → 工具直接執行 → 回傳結果
```

---

### GUARDED（警戒）

**定義**：寫入、網路請求、具有副作用。

這些操作可能影響外部狀態，需要人類首次確認。確認後，**大多數** GUARDED 工具在同一 session 內後續執行無需再確認——但帶有 `EXEC` 或 `AGENT_SPAN` 能力的工具例外（見下方 ToolCapability 說明）。

**典型工具**：`write_file`, `memorize`, `relate`, `web_search`, `run_bash`, `spawn_agent`

**執行流程**：
```
首次執行（一般 GUARDED）：
  Agent 請求 → BlastRadiusMiddleware 攔截 → ⚠️ 詢問用戶 → 同意 → 執行 → 記住授權

session 內後續執行（一般 GUARDED）：
  Agent 請求 → BlastRadiusMiddleware 發現已授權 → 直接執行

EXEC / AGENT_SPAN 工具（每次都重新確認）：
  Agent 請求 → BlastRadiusMiddleware 攔截 → ⚠️ 每次詢問 → 同意 → 執行（不記住授權）
```

**Session 結束後**：所有 GUARDED 授權重置，下次 session 需重新確認。

---

### CRITICAL（危險）

**定義**：破壞性、跨系統、不可逆。

每次執行都需要人類明確確認，沒有任何 session 內免確認機制。

**典型場景**：`rm -rf` 類刪除操作、跨系統寫入、執行破壞性腳本

**執行流程**：
```
每次執行：
  Agent 請求 → BlastRadiusMiddleware 攔截 → 🔴 強制確認 → 同意 → 執行

超時 / 否決：
  → 工具不執行，Agent 收到 DENIED，授權狀態不改變
```

---

## ToolCapability 旗標（v0.2.3.4+）

`ToolCapability` 是附加在 `TrustLevel` 之上的位元旗標，描述工具「能做什麼」，而非僅描述「風險多高」。

```python
class ToolCapability(Flag):
    NONE       = 0
    EXEC       = auto()      # 執行任意 shell / 子程序命令
    NETWORK    = auto()      # 發出對外網路請求
    AGENT_SPAN = auto()      # 生成子代理
    MUTATES    = auto()      # 修改檔案、記憶或其他持久狀態
```

### 內建工具完整分類表

| 工具 | TrustLevel | Capabilities | 說明 |
|------|-----------|-------------|------|
| `read_file` | SAFE | — | 讀取工作區內的檔案 |
| `list_dir` | SAFE | — | 列出目錄內容 |
| `recall` | SAFE | — | 搜尋記憶 |
| `query_relations` | SAFE | — | 查詢關聯記憶三元組 |
| `fetch_url` | SAFE | NETWORK | 擷取網頁（唯讀） |
| `write_file` | GUARDED | MUTATES | 寫入工作區檔案 |
| `memorize` | GUARDED | MUTATES | 寫入語意記憶 |
| `relate` | GUARDED | MUTATES | 寫入關聯記憶 |
| `web_search` | GUARDED | NETWORK | 透過 Brave API 搜尋 |
| `run_bash` | GUARDED | **EXEC** | 執行 shell 指令 — 每次重新確認 |
| `spawn_agent` | GUARDED | **AGENT_SPAN** + MUTATES | 啟動子代理 — 每次重新確認 |

### EXEC 與 AGENT_SPAN 的特殊行為

這兩個旗標代表「即使信任等級是 GUARDED，每次執行都必須重新確認」：

- **EXEC**（`run_bash`）：任意 shell 指令的影響範圍無法靜態預測。一次授權不代表下次的命令同樣安全。
- **AGENT_SPAN**（`spawn_agent`）：子代理本身可以再呼叫更多工具，存在遞迴升級風險。

這樣的設計讓這兩個工具實際上達到 CRITICAL 的確認頻率，同時保留 GUARDED 的分類以便做更細粒度的政策設定。

---

## strict_sandbox（工作區沙箱，v0.2.3.4+）

在 `loom.toml` 中啟用後，`run_bash` 的子程序會以 `cwd=workspace` 啟動，將 shell 的相對路徑錨定在專案資料夾內：

```toml
[harness]
strict_sandbox = true   # 預設 false
```

**效果與限制**：

| 工具 | 行為 |
|------|------|
| `read_file` | **始終**重新路由至 workspace，與 `strict_sandbox` 無關 |
| `write_file` | **始終**重新路由至 workspace，與 `strict_sandbox` 無關 |
| `list_dir` | **始終**重新路由至 workspace，與 `strict_sandbox` 無關 |
| `run_bash` | `strict_sandbox=false`：以呼叫者的 CWD 執行；`strict_sandbox=true`：以 `workspace` 為 cwd 執行 |
| `spawn_agent` | 與 `strict_sandbox` 無關；`AGENT_SPAN` 能力使每次執行都重新確認，不受沙箱設定影響 |

> **注意**：`strict_sandbox` 對 `run_bash` 只鎖定工作目錄，無法防止使用絕對路徑存取 workspace 外的檔案。若需要完整隔離，請搭配容器（Docker）或 OS 層級沙箱使用。

---

## /auto — session 內 EXEC 自動批准（v0.2.3.4+）

`/auto` 是一個 in-session 切換指令，讓使用者在 session 生命週期內臨時放寬 `run_bash` 的確認需求。

### 前提條件

`/auto` **必須搭配 `strict_sandbox = true`** 才能啟用：

```
/auto   →  ⚠️  需要 strict_sandbox = true in loom.toml
```

這個設計是刻意的：沒有 workspace 沙箱的情況下，pre-authorize `run_bash` 等於把整台機器交給 agent，不應該允許。

### 啟用後的行為

```
/auto on  （strict_sandbox = true）

workspace 內的命令（相對路徑或無路徑）：
  Agent 請求 run_bash → BlastRadiusMiddleware →
    exec_auto=true + 無逃逸路徑 → 直接執行（無確認）

逃逸 workspace 的命令（絕對路徑）：
  Agent 請求 run_bash "cat /etc/passwd" → BlastRadiusMiddleware →
    exec_auto=true + 偵測到逃逸路徑 → ⚠️ 強制確認
```

### 逃逸偵測邏輯

`make_exec_escape_fn(workspace)` 掃描命令字串中所有看起來像路徑的 token（`/` 或 `C:\` 開頭），對每個 token 做 `Path.resolve().relative_to(workspace)` 驗證：

```
cat /etc/passwd         → /etc/passwd 不在 workspace 內 → 逃逸 → 確認
rm -rf /tmp/foo         → /tmp/foo 不在 workspace 內 → 逃逸 → 確認
python test.py          → 無絕對路徑 → 安全 → 自動批准
cd src && ls            → 無絕對路徑 → 安全 → 自動批准
pip install numpy       → 無絕對路徑 → 安全 → 自動批准
```

### 推薦使用場景

```toml
# loom.toml
[harness]
strict_sandbox = true
```

Session 開始後：
```
/auto          ← 開啟，之後連續 shell 操作不再逐一確認
...工作中...
/auto          ← 關閉（或 session 結束自動重置）
```

### 行為矩陣

| strict_sandbox | /auto | 行為 |
|---------------|-------|------|
| `false` | off | 每次確認 |
| `false` | on | ❌ 不允許（警告提示） |
| `true` | off | 每次確認 |
| `true` | on | workspace 內自動批准；偵測到逃逸絕對路徑仍強制確認 |

---

## PermissionContext

`PermissionContext` 是 Trust Level 在 session 內的運行時授權狀態：

```python
@dataclass
class PermissionContext:
    session_id: str
    session_authorized: set[str]   # 本 session 已授權的工具名稱

    def authorize(self, tool_name: str) -> None: ...
    def revoke(self, tool_name: str) -> None: ...
    def is_authorized(self, tool_name: str, trust_level: TrustLevel) -> bool: ...
```

`BlastRadiusMiddleware` 在用戶確認後：
- 一般 GUARDED 工具 → 呼叫 `perm.authorize(tool_name)`，session 內不再詢問
- EXEC / AGENT_SPAN 工具 → **不呼叫 authorize**，下次仍會重新詢問

---

## 設定 Trust Level 的方式

### 內建工具（loom/platform/cli/tools.py）

```python
ToolDefinition(
    name="write_file",
    trust_level=TrustLevel.GUARDED,
    capabilities=ToolCapability.MUTATES,
    ...
)

ToolDefinition(
    name="run_bash",
    trust_level=TrustLevel.GUARDED,
    capabilities=ToolCapability.EXEC,   # ← 每次重新確認
    ...
)
```

### 外掛工具（@loom.tool decorator）

```python
@loom.tool(trust_level="guarded")
async def my_custom_tool(call):
    ...
```

外掛工具目前不支援直接指定 `capabilities`，預設為 `ToolCapability.NONE`（一般 GUARDED 行為）。

---

## 常見問題

**Q: 為什麼 `run_bash` 是 GUARDED 而不是 CRITICAL？**

A: GUARDED 表示「有副作用，需要確認」。CRITICAL 表示「不可逆、跨系統破壞」。`run_bash` 本身不一定是破壞性的（例如 `ls`、`pip install`），因此保留 GUARDED 分類。但透過 `EXEC` capability 旗標，讓它達到與 CRITICAL 相同的「每次重新確認」行為。

**Q: `strict_sandbox` 能完全防止 run_bash 存取 workspace 外的檔案嗎？**

A: 不能。它只是設定子程序的起始目錄（`cwd`）。指令仍然可以使用絕對路徑（如 `cat /etc/passwd`）。若需要完整隔離，請使用 Docker 或 OS 沙箱。

**Q: GUARDED 有 session 內免確認，CRITICAL / EXEC 沒有，為什麼？**

A: GUARDED 的典型場景是「連續寫入多個檔案」，每次詢問會造成操作中斷。EXEC 和 AGENT_SPAN 的影響範圍依命令而異，無法在首次確認時預判後續呼叫的風險。

**Q: 我有 `strict_sandbox = true` + `/auto`，agent 連續跑了 6 個 run_bash 都沒問我，這正常嗎？**

A: 正常，這正是設計目標。只要這 6 個命令都沒有使用 workspace 外的絕對路徑，`exec_auto` 就會讓它們靜默通過。若其中有任何一個命令像 `cat /etc/passwd`，那一個會被攔下來確認。

**Q: `/auto` session 結束後會保留嗎？**

A: 不會。`exec_auto` 是 `PermissionContext` 上的 in-memory 旗標，session 結束後重置。下次 session 開始預設為 off，需要重新 `/auto`。
