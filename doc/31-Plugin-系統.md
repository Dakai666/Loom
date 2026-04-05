# Plugin 系統

Plugin 是 Loom 的「功能插件」。它允許新增全新的元件類型，包括工具、Notifier、Trigger 等，而不需要修改核心程式碼。

---

## Plugin 結構

```
my-plugin/
├── __init__.py          # Plugin 入口
├── plugin.py            # Plugin 主類
├── manifest.toml        # Plugin 描述
├── tools/               # 工具定義
│   ├── __init__.py
│   └── my_tool.py
├── notifiers/           # Notifier 定義
│   └── my_notifier.py
└── triggers/            # Trigger 定義
    └── my_trigger.py
```

---

## LoomPlugin 抽象

```python
# loom/core/plugin/abc.py
class LoomPlugin(ABC):
    """Plugin 抽象基類"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Plugin 名稱"""
        pass
    
    @property
    @abstractmethod
    def version(self) -> str:
        """Plugin 版本"""
        pass
    
    @property
    def dependencies(self) -> list[str]:
        """依賴的其他 Plugins"""
        return []
    
    async def install(self, registry: PluginRegistry):
        """Plugin 安裝鉤子"""
        pass
    
    async def uninstall(self, registry: PluginRegistry):
        """Plugin 卸載鉤子"""
        pass
    
    async def on_load(self):
        """Plugin 載入時呼叫"""
        pass
    
    async def on_unload(self):
        """Plugin 卸載時呼叫"""
        pass
```

---

## Plugin 註冊

### 工具註冊

```python
# loom/core/plugin/registry.py
class PluginRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._notifiers: dict[str, Notifier] = {}
        self._triggers: dict[str, type[Trigger]] = {}
        self._lenses: dict[str, BaseLens] = {}
    
    def register_tool(self, tool: Tool):
        """註冊工具"""
        if tool.name in self._tools:
            raise PluginConflictError(f"Tool already exists: {tool.name}")
        self._tools[tool.name] = tool
    
    def register_notifier(self, notifier: Notifier):
        """註冊 Notifier"""
        self._notifiers[notifier.name] = notifier
    
    def register_trigger(self, trigger_class: type[Trigger]):
        """註冊 Trigger"""
        self._triggers[trigger_class.__name__] = trigger_class
    
    def register_lens(self, lens: BaseLens):
        """註冊 Lens"""
        self._lenses[lens.name] = lens
```

---

## 完整 Plugin 範例

### manifest.toml

```toml
[plugin]
name = "example-plugin"
version = "1.0.0"
description = "一個範例 Plugin"
author = "developer@example.com"
license = "MIT"

[plugin.dependencies]
loom = ">=1.0.0"

[plugin.provides]
tools = ["example_tool", "example_tool2"]
notifiers = ["example_notifier"]
triggers = ["ExampleTrigger"]

[plugin.permissions]
network = "required"
filesystem = "read-only"
```

### __init__.py

```python
# example_plugin/__init__.py
from .plugin import ExamplePlugin

__all__ = ["ExamplePlugin"]
```

### plugin.py

```python
# example_plugin/plugin.py
from loom.core.plugin.abc import LoomPlugin
from loom.core.plugin.registry import PluginRegistry

class ExamplePlugin(LoomPlugin):
    """範例 Plugin"""
    
    name = "example-plugin"
    version = "1.0.0"
    description = "一個範例 Plugin"
    
    def __init__(self):
        self._registry: PluginRegistry | None = None
    
    async def install(self, registry: PluginRegistry):
        """安裝 Plugin"""
        self._registry = registry
        
        # 註冊工具
        from .tools.example_tool import ExampleTool
        registry.register_tool(ExampleTool())
        
        # 註冊 Notifier
        from .notifiers.example_notifier import ExampleNotifier
        registry.register_notifier(ExampleNotifier())
        
        # 註冊 Trigger
        from .triggers.example_trigger import ExampleTrigger
        registry.register_trigger(ExampleTrigger)
    
    async def uninstall(self, registry: PluginRegistry):
        """卸載 Plugin"""
        # 移除已註冊的元件
        registry._tools.pop("example_tool", None)
        registry._notifiers.pop("example_notifier", None)
        registry._triggers.pop("ExampleTrigger", None)
    
    async def on_load(self):
        """Plugin 載入時"""
        logger.info(f"Plugin {self.name} loaded")
    
    async def on_unload(self):
        """Plugin 卸載時"""
        logger.info(f"Plugin {self.name} unloaded")
```

### 工具定義

```python
# example_plugin/tools/example_tool.py
from dataclasses import dataclass
from loom.core.harness.tool import Tool, ToolResult

@dataclass
class ExampleTool(Tool):
    """範例工具"""
    
    name = "example_tool"
    description = "這是一個範例工具"
    trust_level = "SAFE"
    
    async def execute(self, args: dict, context: dict) -> ToolResult:
        """執行工具"""
        
        action = args.get("action", "greet")
        
        if action == "greet":
            return ToolResult(
                success=True,
                output=f"Hello, {args.get('name', 'World')}!"
            )
        elif action == "echo":
            return ToolResult(
                success=True,
                output=args.get("message", "")
            )
        else:
            return ToolResult(
                success=False,
                error=f"Unknown action: {action}"
            )
    
    def get_openai_schema(self) -> dict:
        """返回 OpenAI 格式的 schema"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["greet", "echo"],
                            "description": "要執行的動作"
                        },
                        "name": {
                            "type": "string",
                            "description": "名字（用於 greet 動作）"
                        },
                        "message": {
                            "type": "string",
                            "description": "訊息（用於 echo 動作）"
                        }
                    },
                    "required": ["action"]
                }
            }
        }
```

### Notifier 定義

```python
# example_plugin/notifiers/example_notifier.py
from loom.core.notification.adapters.base import Notifier
from loom.core.notification.models import Notification

class ExampleNotifier(Notifier):
    """範例 Notifier"""
    
    name = "example_notifier"
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
    
    async def send(self, notification: Notification) -> bool:
        """發送通知"""
        
        payload = {
            "title": notification.title,
            "message": notification.message,
            "type": notification.type.value,
        }
        
        async with aiohttp.ClientSession() as session:
            async with session.post(self.webhook_url, json=payload) as response:
                return response.status == 200
```

---

## Plugin 安裝流程

```bash
# 從目錄安裝
loom plugin install ./my-plugin

# 從 URL 安裝
loom plugin install https://example.com/plugins/my-plugin.tar.gz

# 從 registry 安裝
loom plugin install example-plugin

# 列出已安裝的 plugins
loom plugin list

# 卸載 plugin
loom plugin uninstall example-plugin

# 更新 plugin
loom plugin update example-plugin
```

---

## Plugin Registry

### 本地 Registry

```toml
# ~/.loom/plugins/registry.json
{
  "plugins": [
    {
      "name": "example-plugin",
      "version": "1.0.0",
      "path": "~/.loom/plugins/example-plugin",
      "enabled": true,
      "installed_at": "2024-01-01T00:00:00Z"
    }
  ]
}
```

### 官方 Registry

```toml
# loom.toml
[plugin.registry]
official = "https://registry.loom.dev/plugins"
community = "https://registry.loom.community/plugins"

# 鏡像設定
[plugin.registry.mirrors]
asia = "https://asia.registry.loom.dev/plugins"
```

---

## Plugin 安全性

### 簽章驗證

```python
# 驗證 Plugin 簽章
async def verify_plugin(plugin_path: str) -> bool:
    """驗證 Plugin 簽章"""
    
    # 檢查是否有簽章文件
    sig_file = Path(plugin_path) / "manifest.toml.sig"
    if not sig_file.exists():
        return False
    
    # 驗證簽章
    with open(sig_file, "r") as f:
        signature = f.read()
    
    with open(Path(plugin_path) / "manifest.toml", "r") as f:
        manifest = f.read()
    
    return verify_signature(manifest, signature, LOOM_PUBLIC_KEY)
```

### 權限控制

```toml
[plugin.permissions]
network = "required"        # 需要網路權限
filesystem = "read-only"    # 只讀檔案系統
env = ["VAR1", "VAR2"]     # 可訪問的環境變數
```

---

## 總結

Plugin 系統提供完整的功能擴充能力：

| 功能 | 說明 |
|------|------|
| 工具擴充 | 註冊新工具 |
| Notifier 擴充 | 新增通知方式 |
| Trigger 擴充 | 新增觸發器類型 |
| Lens 擴充 | 新增工具包裝器 |
| 安裝管理 | CLI 安裝/卸載/更新 |
| 安全性 | 簽章驗證、權限控制 |
