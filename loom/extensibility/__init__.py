from .lens import BaseLens, LensResult, LensRegistry
from .hermes import HermesLens
from .openai_tools import OpenAIToolsLens
from .pipeline import ImportDecision, SkillImportPipeline
from .adapter import AdapterRegistry
from .plugin import LoomPlugin, PluginRegistry
from .mcp_server import run_mcp_server
from .mcp_client import LoomMCPClient, MCPServerConfig, load_mcp_servers_into_session
from .dreaming_plugin import DreamingPlugin
from .self_reflection_plugin import SelfReflectionPlugin

__all__ = [
    "BaseLens", "LensResult", "LensRegistry",
    "HermesLens",
    "OpenAIToolsLens",
    "ImportDecision", "SkillImportPipeline",
    "AdapterRegistry",
    "LoomPlugin", "PluginRegistry",
    # MCP — Issue #9 (requires: pip install loom[mcp])
    "run_mcp_server",
    "LoomMCPClient", "MCPServerConfig", "load_mcp_servers_into_session",
    # Plugins
    "DreamingPlugin",
    "SelfReflectionPlugin",
]
