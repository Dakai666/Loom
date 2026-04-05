# Extensibility 概述

Extensibility 是 Loom 的「擴充系統」。它讓開發者可以新增工具、通知適配器、人格等元件，而不需要修改核心程式碼。

---

## 三大擴充機制

```
┌─────────────────────────────────────────────────────────────┐
│                    Loom 擴充系統                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│   │    Lens     │  │   Plugin    │  │  Skill      │    │
│   │   系統      │  │   系統      │  │  Import     │    │
│   │            │  │            │  │            │    │
│   │  包裝工具   │  │  擴充功能   │  │  匯入技能   │    │
│   │  增強功能   │  │  新增模組   │  │  審查流程   │    │
│   └─────────────┘  └─────────────┘  └─────────────┘    │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Lens 系統

### 用途

Lens 是工具的「包裝器」。它為現有工具添加新功能：

| 功能 | 說明 |
|------|------|
| 輸入處理 | 驗證、轉換、豐富化 |
| 輸出處理 | 格式化、快取、錯誤處理 |
| 日誌記錄 | 追蹤工具呼叫 |
| 重試機制 | 自動重試失敗的呼叫 |

詳見 [30-Lens-系統.md](30-Lens-系統.md)。

---

## Plugin 系統

### 用途

Plugin 是功能的「插件」。它允許新增全新的模組：

| 功能 | 說明 |
|------|------|
| 新工具 | 新增自訂工具 |
| 新 Notifier | 新增通知方式 |
| 新 Middleware | 新增中介層功能 |
| 新 Trigger | 新增觸發器類型 |

詳見 [31-Plugin-系統.md](31-Plugin-系統.md)。

---

## Skill Import

### 用途

Skill Import 允許從外部匯入技能到 Loom：

| 功能 | 說明 |
|------|------|
| 技能發現 | 從描述中发现潜在技能 |
| 審查流程 | 驗證技能的有效性 |
| 去重 | 避免重複技能 |
| Confidence Gate | 設定初始 confidence |

詳見 [32-Skill-Import.md](32-Skill-Import.md)。

---

## 擴充的層級

```
┌─────────────────────────────────────────────────────────────┐
│                      擴充層級                                  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   Level 1: Configuration（配置層）                           │
│   │   直接修改 loom.toml                                     │
│   │   新增工具定義、通知設定、觸發器                          │
│   │                                                           │
│   ├─▶ Level 2: Lens（鏡片層）                               │
│   │   為工具添加包裝器，不需要修改工具本身                    │
│   │                                                           │
│   ├─▶ Level 3: Plugin（插件層）                             │
│   │   新增全新的元件類型                                      │
│   │                                                           │
│   └─▶ Level 4: Core（核心層）                               │
│       修改 Loom 核心程式碼（需要 fork）                      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## 擴充載入時機

```python
# loom/core/extensibility/loader.py
class ExtensionLoader:
    """擴充載入器"""
    
    async def load_all(self):
        """載入所有擴充"""
        
        # 1. 載入 Plugins
        await self._load_plugins()
        
        # 2. 載入 Lenses
        await self._load_lenses()
        
        # 3. 載入 Skills
        await self._load_skills()
        
        # 4. 載入 Personalities
        await self._load_personalities()
    
    async def _load_plugins(self):
        """載入 plugins"""
        plugin_dirs = self._discover_plugins()
        
        for plugin_dir in plugin_dirs:
            plugin = await self._load_plugin(plugin_dir)
            self.plugin_registry.register(plugin)
    
    async def _load_lenses(self):
        """載入 lenses"""
        lens_configs = self.config.get("lenses", [])
        
        for config in lens_configs:
            lens = self._create_lens(config)
            self.lens_registry.register(lens)
```

---

## Plugin Discovery

### 自動發現

Loom 會自動掃描以下位置來發現 plugins：

```python
PLUGIN_PATHS = [
    "~/.loom/plugins/",          # 用戶 plugins
    "./loom/plugins/",           # 專案 plugins
    "/usr/local/share/loom/plugins/",  # 系統 plugins
]
```

### Plugin 結構

```
~/.loom/plugins/
└── my-plugin/
    ├── __init__.py
    ├── plugin.py          # Plugin 實現
    └── manifest.toml       # Plugin 描述
```

### manifest.toml

```toml
[plugin]
name = "my-plugin"
version = "1.0.0"
description = "我的自訂插件"
author = "developer@example.com"

[plugin.dependencies]
loom = ">=0.1.0"
other-plugin = ">=1.0.0"

[plugin.provides]
tools = ["my_tool"]
notifiers = ["my_notifier"]

[plugin.requires]
permissions = ["network", "filesystem"]
```

---

## 安全性考量

### Sandboxing

Plugin 運行在受限環境中：

```python
# 預設禁止的權限
RESTRICTED_PERMISSIONS = [
    "subprocess",      # 不能執行外部程式
    "eval",            # 不能使用 eval
    "import",          # 不能動態 import
]
```

### 權限請求

```toml
[plugin.permissions]
network = "optional"    # 可選的網路權限
filesystem = "read-only"  # 只讀檔案系統
```

---

## loom.toml 配置

```toml
[extensibility]

# Plugin 搜尋路徑
plugin_paths = [
    "~/.loom/plugins/",
    "./loom/plugins/",
]

# 是否啟用自動發現
auto_discover = true

# Lens 設定
[extensibility.lenses]
enabled = true
auto_wrap = false  # 不自動包裝所有工具

# Plugin 安全性
[extensibility.security]
sandbox = true
allow_unsigned = false  # 必須有簽章
```

---

## 總結

Loom 的擴充系統提供多層次的客製化能力：

| 層級 | 機制 | 用途 |
|------|------|------|
| 配置層 | loom.toml | 工具定義、通知設定、觸發器 |
| Lens 層 | Lens 包裝器 | 為工具添加功能 |
| Plugin 層 | Plugin 系統 | 新增功能模組 |
| 核心層 | Fork | 修改核心行為 |

選擇合適的層級可以保持向後相容性，減少維護成本。
