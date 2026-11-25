# Trust Level（信任等級）

Trust Level 是 Loom 工具安全模型的基礎。每個工具在註冊時都會被指定一個信任等級，決定它的執行是否需要人類確認。

從 v0.2.3.4 起，`TrustLevel` 三級制之上增加了 `ToolCapability` 旗標系統，讓框架可以對「同屬 GUARDED 但風險截然不同」的工具做出更細緻的區分。

---

## 三級信任（TrustLevel）

### SAFE（安全）

**定義**：唯讀、本地、可逆。

這些操作風險極低，session 開始時已預授權，不需要每次確認。

**典型工具**：`read_file`, `list_dir`, `recall`, `fetch_url`

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
| `recall` | SAFE | — | 搜尋記憶（含三元組，#451 phase B 起為唯一讀取入口）|
| `fetch_url` | SAFE | NETWORK | 擷取網頁（唯讀） |
| `write_file` | GUARDED | MUTATES | 寫入工作區檔案 |
| `memorize` | GUARDED | MUTATES | 寫入語意記憶 |
| `relate` | GUARDED | MUTATES | 寫入三元組（#451 phase B 起透過 relational_bridge 落到 semantic 同一表） |
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

## 授權模式取代 /auto 與工具名授權（#704）

`/auto`（exec_auto）在 #704 S3 退役，改用單一的授權模式 `/mode safe | self | yolo`（預設自理，見 doc/22「授權模式」）。輸入 `/auto` 只會提示改用 `/mode`。

工具名稱本身不再授權任何呼叫：

- **SAFE 工具**本來就是唯讀、本機、可逆，直接執行。
- **宣告（boundary）**：Discord 平台宣告回應本 thread 的工具（`send_discord_file` 等）；routine 的 `allowed_tools` 宣告該 program 自己的無人值守呼叫會用到的工具（有人值守的插隊 turn 看不到）；子代理只能宣告父代理已宣告的工具。宣告過的工具仍要經過授權模式的決策點：不涵蓋 EXEC 與 CRITICAL、不能越過 program 宣告的 scope（ceiling）、不能越過硬護欄（例如寫 `.env`、`loom.toml`、skill 的 `checks.py`）。
- **人工確認**只涵蓋這一次呼叫；下一次同一個工具照樣重新判斷。

## PermissionContext

```python
@dataclass
class PermissionContext:
    session_id: str
    boundaries: dict[str, Boundary]   # 宣告：來源 → 工具名（#704 S3）
    grants: list[ScopeGrant]          # 資源層級的 scope grant
    mode: PermissionMode              # 安全 / 自理 / YOLO

    def declare(self, source: str, tools, *, unattended_only: bool = False) -> None: ...
    def withdraw(self, source: str) -> None: ...
    def declared_by(self, tool_name: str, *, attended: bool) -> str | None: ...
    def runs_unasked(self, tool_name: str, trust_level: TrustLevel) -> bool: ...  # 平行 dispatch 用
```

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

**Q: 確認過一次的 GUARDED 工具，同一個 session 還會再問嗎？**

A: 會（#704 S3 起）。人工確認只涵蓋那一次呼叫；要少問，靠的是授權模式（自理下規則與 Luna、YOLO 下硬護欄內直接執行）與明確的宣告，不是記住工具名稱。
