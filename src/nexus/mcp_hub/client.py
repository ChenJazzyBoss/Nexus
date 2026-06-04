"""MCP client — connect to external MCP servers.

Adapted from claude-code packages/mcp-client and hermes-agent tools/mcp_tool.py.
Manages connections to external MCP servers, discovers tools, and registers
them into the Nexus tool registry.

MCP (Model Context Protocol) is a standard protocol for connecting AI agents
to external tools and data sources.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# 工具名规范化：只保留 [a-zA-Z0-9_-]，其余替换为 _
_NAME_NORMALIZE_PATTERN = re.compile(r"[^a-zA-Z0-9_-]")

# MCP SDK is optional
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    HAS_MCP = True
except ImportError:
    HAS_MCP = False


def _normalize_tool_name(name: str) -> str:
    """规范化工具名：非 [a-zA-Z0-9_-] 字符替换为 _。"""
    return _NAME_NORMALIZE_PATTERN.sub("_", name)


@dataclass
class McpServerConfig:
    """Configuration for an MCP server."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0


@dataclass
class McpTool:
    """A tool discovered from an MCP server."""
    name: str
    server_name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        """Name with server prefix for registration (normalized)."""
        normalized = _normalize_tool_name(self.name)
        return f"mcp_{self.server_name}__{normalized}"


class McpHub:
    """Manages connections to external MCP servers.

    Usage:
        hub = McpHub()
        await hub.connect_server(McpServerConfig(
            name="arxiv",
            command="npx",
            args=["-y", "arxiv-mcp-server"],
        ))
        tools = hub.get_all_tools()
        result = await hub.call_tool("mcp_arxiv__search", {"query": "attention"})
    """

    def __init__(self):
        self._servers: dict[str, Any] = {}  # name -> session
        self._tools: dict[str, McpTool] = {}  # qualified_name -> McpTool
        self._configs: dict[str, McpServerConfig] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect_server(self, config: McpServerConfig) -> list[McpTool]:
        """Connect to an MCP server and discover its tools.

        Args:
            config: Server configuration

        Returns:
            List of discovered tools
        """
        if not HAS_MCP:
            logger.warning("MCP SDK not installed. Install with: pip install mcp")
            return []

        logger.info("Connecting to MCP server: %s", config.name)

        try:
            server_params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env or None,
            )

            # Start the server process
            read_stream, write_stream = await asyncio.wait_for(
                stdio_client(server_params).__aenter__(),
                timeout=config.timeout,
            )

            session = ClientSession(read_stream, write_stream)
            await asyncio.wait_for(
                session.__aenter__(),
                timeout=config.timeout,
            )

            # Initialize
            await asyncio.wait_for(
                session.initialize(),
                timeout=config.timeout,
            )

            # Discover tools
            tools_response = await asyncio.wait_for(
                session.list_tools(),
                timeout=config.timeout,
            )

            self._servers[config.name] = session
            self._configs[config.name] = config

            discovered = []
            for tool in tools_response.tools:
                mcp_tool = McpTool(
                    name=tool.name,
                    server_name=config.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                )
                self._tools[mcp_tool.qualified_name] = mcp_tool
                discovered.append(mcp_tool)
                logger.debug("Discovered MCP tool: %s", mcp_tool.qualified_name)

            logger.info(
                "Connected to MCP server %s: %d tools discovered",
                config.name, len(discovered),
            )
            return discovered

        except asyncio.TimeoutError:
            logger.error("MCP server %s connection timed out", config.name)
            return []
        except Exception as e:
            logger.error("Failed to connect to MCP server %s: %s", config.name, e)
            return []

    async def disconnect_server(self, server_name: str) -> None:
        """Disconnect from an MCP server."""
        session = self._servers.pop(server_name, None)
        if session:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass

        # Remove tools from this server
        to_remove = [
            name for name, tool in self._tools.items()
            if tool.server_name == server_name
        ]
        for name in to_remove:
            del self._tools[name]

        self._configs.pop(server_name, None)
        logger.info("Disconnected from MCP server: %s", server_name)

    async def disconnect_all(self) -> None:
        """Disconnect from all servers."""
        for name in list(self._servers.keys()):
            await self.disconnect_server(name)

    def get_all_tools(self) -> list[McpTool]:
        """Return all discovered tools."""
        return list(self._tools.values())

    def get_tools_for_registry(self) -> list[dict[str, Any]]:
        """Return tools in a format suitable for ToolRegistry.merge_external_tools().

        每个工具字典包含 name, description, parameters, handler, is_async 字段，
        可直接传入 merge_external_tools()。
        """
        tools = []
        for tool in self._tools.values():
            hub_ref = self
            qname = tool.qualified_name

            async def _make_call(args: dict[str, Any], _qname=qname) -> str:
                return await hub_ref.call_tool(_qname, args)

            tools.append({
                "name": tool.qualified_name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "handler": _make_call,
                "is_async": True,
            })
        return tools

    def register_to_registry(self, registry: Any) -> int:
        """将所有已发现的 MCP 工具桥接到 Nexus ToolRegistry。

        为每个 MCP 工具创建一个同步 call() 闭包，调用时通过 MCP session 执行。
        工具注册到 "mcp" toolset，is_async=True 以支持异步调度。

        Args:
            registry: ToolRegistry 实例

        Returns:
            成功注册的工具数量
        """
        registered = 0
        for tool in self._tools.values():
            # 创建闭包：捕获 qualified_name 和 hub 引用
            hub_ref = self
            qualified_name = tool.qualified_name

            def _make_handler(qname: str) -> Callable:
                """创建 MCP 工具调用闭包。"""

                async def _handler(args: dict[str, Any], **kwargs: Any) -> str:
                    return await hub_ref.call_tool(qname, args)

                return _handler

            schema = {
                "description": tool.description or f"MCP tool: {tool.name}",
                "parameters": tool.input_schema or {"type": "object", "properties": {}},
            }

            try:
                registry.register(
                    name=tool.qualified_name,
                    toolset="mcp",
                    schema=schema,
                    handler=_make_handler(qualified_name),
                    is_async=True,
                    description=tool.description,
                    emoji="🔌",
                    source=f"mcp:{tool.server_name}",
                )
                registered += 1
                logger.debug("桥接 MCP 工具到 registry: %s", tool.qualified_name)
            except Exception as e:
                logger.warning("注册 MCP 工具 %s 失败: %s", tool.qualified_name, e)

        logger.info("已桥接 %d 个 MCP 工具到 ToolRegistry", registered)
        return registered

    async def call_tool(self, qualified_name: str, arguments: dict[str, Any]) -> str:
        """Call an MCP tool by its qualified name.

        Args:
            qualified_name: Tool name with server prefix (e.g. mcp_arxiv__search)
            arguments: Tool arguments

        Returns:
            Tool result as string
        """
        tool = self._tools.get(qualified_name)
        if not tool:
            return json.dumps({"error": f"Unknown MCP tool: {qualified_name}"})

        session = self._servers.get(tool.server_name)
        if not session:
            return json.dumps({"error": f"MCP server {tool.server_name} not connected"})

        try:
            result = await asyncio.wait_for(
                session.call_tool(tool.name, arguments),
                timeout=60.0,
            )

            # Extract text content
            if hasattr(result, 'content'):
                parts = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        parts.append(item.text)
                return "\n".join(parts) if parts else json.dumps({"result": "ok"})

            return str(result)

        except asyncio.TimeoutError:
            return json.dumps({"error": f"MCP tool {qualified_name} timed out"})
        except Exception as e:
            return json.dumps({"error": f"MCP tool {qualified_name} failed: {str(e)}"})

    @property
    def connected_servers(self) -> list[str]:
        """Return list of connected server names."""
        return list(self._servers.keys())
