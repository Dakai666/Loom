# CLI 命令

Loom CLI 提供完整的命令列介面來操作 Loom 的各個功能。

---

## 安裝

```bash
# pip 安裝
pip install loom-cli

# 或使用 uv
uv tool install loom-cli
```

---

## 全域選項

```bash
# 查看版本
loom --version

# 查看幫助
loom --help

# 指定設定檔
loom --config /path/to/loom.toml

# 開啟除錯模式
loom --debug

# 安靜模式（只輸出結果）
loom --quiet
```

---

## 聊天命令

### `loom chat`

啟動互動式聊天。

```bash
loom chat [options]

# 選項
--personality, -p     設定人格（預設: architect）
--session, -s          指定 session ID 或建立新的
--resume               恢復上次 session
--model                指定模型
--no-memory            停用記憶功能
--context              附加額外上下文
```

**範例**

```bash
# 使用預設設定聊天
loom chat

# 使用 minimalist 人格
loom chat -p minimalist

# 恢復上次 session
loom chat --resume

# 指定特定 session
loom chat --session abc123
```

---

## 記憶命令

### `loom memory`

查看和管理記憶。

```bash
loom memory <subcommand> [options]

# 子命令
loom memory list                    # 列出所有記憶
loom memory search <query>          # 搜尋記憶
loom memory get <key>               # 獲取特定記憶
loom memory set <key> <value>       # 設定記憶
loom memory delete <key>            # 刪除記憶
loom memory stats                   # 查看統計
loom memory clear [--type TYPE]     # 清除記憶
```

**範例**

```bash
# 列出所有 semantic 記憶
loom memory list --type semantic

# 搜尋記憶
loom memory search "trust level"

# 設定新記憶
loom memory set project:name "Next Framework"

# 查看統計
loom memory stats
```

### `loom recall`

快速搜尋記憶。

```bash
loom recall <query> [options]

# 選項
--limit, -n       最大結果數（預設: 5）
--type            記憶類型（semantic/episodic/skill/relational）
--format          輸出格式（text/json）
```

**範例**

```bash
loom recall "harness middleware"
loom recall "python" --limit 10
loom recall "error" --type episodic --format json
```

---

## 任務命令

### `loom task`

執行和管理任務。

```bash
loom task <subcommand> [options]

# 子命令
loom task run <task-file>          # 執行任務
loom task list                      # 列出任務
loom task status <task-id>         # 查看任務狀態
loom task cancel <task-id>         # 取消任務
loom task logs <task-id>           # 查看任務日誌
```

**範例**

```bash
# 執行任務檔案
loom task run ./tasks/deploy.yaml

# 查看任務狀態
loom task status task-123
```

---

## Autonomy 命令

### `loom autonomy`

管理 Autonomy Daemon。

```bash
loom autonomy <subcommand> [options]

# 子命令
loom autonomy start                 # 啟動 daemon
loom autonomy stop                  # 停止 daemon
loom autonomy status               # 查看狀態
loom autonomy restart              # 重啟 daemon
loom autonomy logs [options]       # 查看日誌
```

**範例**

```bash
# 啟動 daemon
loom autonomy start

# 查看狀態
loom autonomy status

# 查看日誌
loom autonomy logs --tail 100
loom autonomy logs --trigger daily_summary
```

### `loom trigger`

管理觸發器。

```bash
loom trigger <subcommand> [options]

# 子命令
loom trigger list                   # 列出觸發器
loom trigger enable <id>           # 啟用觸發器
loom trigger disable <id>          # 停用觸發器
loom trigger fire <id>             # 手動觸發
loom trigger add <config-file>     # 新增觸發器
loom trigger remove <id>           # 移除觸發器
```

**範例**

```bash
# 列出所有觸發器
loom trigger list

# 啟用觸發器
loom trigger enable daily_summary

# 手動觸發
loom trigger fire daily_summary
```

---

## Reflection 命令

### `loom reflect`

觸發 Reflection 分析。

```bash
loom reflect [options]

# 選項
--session <id>       指定 session（預設: 當前 session）
--format             輸出格式（text/json）
--output, -o         輸出到檔案
--no-persist         不寫入記憶
```

**範例**

```bash
# 反射當前 session
loom reflect

# 反射特定 session
loom reflect --session abc123

# 輸出到檔案
loom reflect --format json -o report.json
```

---

## Session 管理

### `loom sessions`

管理聊天 sessions。

```bash
loom sessions <subcommand> [options]

# 子命令
loom sessions list                 # 列出 sessions
loom sessions show <id>            # 查看 session 詳情
loom sessions resume <id>         # 恢復 session
loom sessions delete <id>          # 刪除 session
loom sessions export <id>          # 匯出 session
```

**範例**

```bash
# 列出所有 sessions
loom sessions list

# 查看 session 詳情
loom sessions show abc123

# 刪除 session
loom sessions delete abc123

# 匯出 session
loom sessions export abc123 -o session.json
```

---

## Plugin 管理

### `loom plugin`

管理 plugins。

```bash
loom plugin <subcommand> [options]

# 子命令
loom plugin list                   # 列出已安裝 plugins
loom plugin install <source>       # 安裝 plugin
loom plugin uninstall <name>       # 卸載 plugin
loom plugin update <name>          # 更新 plugin
loom plugin info <name>           # 查看 plugin 資訊
loom plugin search <query>         # 搜尋可用 plugins
```

**範例**

```bash
# 列出已安裝
loom plugin list

# 從目錄安裝
loom plugin install ./my-plugin

# 從 GitHub 安裝
loom plugin install https://github.com/user/plugin

# 搜尋 plugins
loom plugin search "webhook"
```

---

## 設定命令

### `loom config`

管理設定。

```bash
loom config <subcommand> [options]

# 子命令
loom config show                   # 顯示當前設定
loom config set <key> <value>     # 設定值
loom config get <key>             # 獲取值
loom config edit                  # 編輯設定檔
loom config validate              # 驗證設定
loom config reset                # 重置為預設
```

**範例**

```bash
# 顯示設定
loom config show

# 設定值
loom config set cognition.default_model "gpt-4o"

# 驗證設定
loom config validate
```

---

## Tool 管理

### `loom tool`

管理工具。

```bash
loom tool <subcommand> [options]

# 子命令
loom tool list                     # 列出工具
loom tool info <name>            # 查看工具詳情
loom tool test <name> [args]     # 測試工具
loom tool validate <file>         # 驗證工具定義
```

**範例**

```bash
# 列出所有工具
loom tool list

# 測試工具
loom tool test read_file path="README.md"
```

---

## 初始化

### `loom init`

初始化 Loom 設定。

```bash
loom init [options]

# 選項
--path <directory>         設定檔目錄
--force                    覆蓋現有設定
--template <name>          使用範本
```

**範例**

```bash
# 在目前目錄初始化
loom init

# 在指定目錄初始化
loom init --path ~/.loom

# 使用範本初始化
loom init --template minimal
```

---

## 説明命令

### `loom help`

顯示命令幫助。

```bash
loom help [command]

# 範例
loom help chat
loom help memory
loom help autonomy
```

---

## 總結

| 命令 | 功能 |
|------|------|
| `loom chat` | 互動式聊天 |
| `loom memory` | 記憶管理 |
| `loom recall` | 快速搜尋記憶 |
| `loom task` | 任務執行 |
| `loom autonomy` | Autonomy Daemon |
| `loom trigger` | 觸發器管理 |
| `loom reflect` | Reflection 分析 |
| `loom sessions` | Session 管理 |
| `loom plugin` | Plugin 管理 |
| `loom config` | 設定管理 |
| `loom tool` | 工具管理 |
| `loom init` | 初始化 |
