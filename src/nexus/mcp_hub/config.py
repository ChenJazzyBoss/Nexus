"""MCP configuration auto-discovery.

从多个来源发现 MCP 服务器配置：
1. config.yaml 中的 mcp_servers 字段（通过 NexusConfig 传入）
2. .claude/settings.json 中的 mcpServers 字段
3. 项目根目录的 .mcp.json 文件
4. 环境变量展开：${VAR} 替换为实际值

合并规则：同名配置后面的覆盖前面的。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from nexus.mcp_hub.client import McpServerConfig

logger = logging.getLogger(__name__)

# 环境变量匹配模式：${VAR_NAME}
_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


def _expand_env_vars(value: Any) -> Any:
    """递归展开字符串中的 ${VAR} 环境变量引用。

    非字符串值原样返回。未定义的环境变量保留原始占位符。
    """
    if isinstance(value, str):
        def _replacer(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))
        return _ENV_VAR_PATTERN.sub(_replacer, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


def _read_yaml_servers(path: Path) -> dict[str, dict[str, Any]]:
    """从 YAML 配置文件读取 mcp_servers 字段。"""
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("mcp_servers", {})
    except Exception as e:
        logger.warning("读取 YAML 配置失败 (%s): %s", path, e)
        return {}


def _read_claude_settings(project_root: Path) -> dict[str, dict[str, Any]]:
    """从 .claude/settings.json 中读取 mcpServers 配置。

    检查项目级和用户级两个位置。
    """
    sources = [
        project_root / ".claude" / "settings.json",       # 项目级
        Path.home() / ".claude" / "settings.json",         # 用户级
    ]
    merged: dict[str, dict[str, Any]] = {}
    for path in sources:
        if not path.exists():
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            servers = data.get("mcpServers", {})
            if servers:
                logger.debug("从 %s 发现 %d 个 MCP 服务器配置", path, len(servers))
            merged.update(servers)
        except Exception as e:
            logger.warning("读取 Claude settings 失败 (%s): %s", path, e)
    return merged


def _read_mcp_json(project_root: Path) -> dict[str, dict[str, Any]]:
    """从项目根目录的 .mcp.json 文件读取 MCP 服务器配置。"""
    path = project_root / ".mcp.json"
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        servers = data.get("mcpServers", data.get("servers", {}))
        if servers:
            logger.debug("从 %s 发现 %d 个 MCP 服务器配置", path, len(servers))
        return servers
    except Exception as e:
        logger.warning("读取 .mcp.json 失败 (%s): %s", path, e)
        return {}


def _raw_to_server_config(name: str, raw: dict[str, Any]) -> McpServerConfig:
    """将原始配置字典转换为 McpServerConfig 对象。"""
    return McpServerConfig(
        name=name,
        command=raw.get("command", ""),
        args=raw.get("args", []),
        env=raw.get("env", {}),
        timeout=float(raw.get("timeout", 30.0)),
    )


def discover_mcp_configs(
    project_root: str | Path = ".",
    yaml_config: dict[str, dict[str, Any]] | None = None,
) -> list[McpServerConfig]:
    """自动发现所有来源的 MCP 服务器配置并合并。

    优先级（后者覆盖前者）：
    1. yaml_config（来自 config.yaml 的 mcp_servers）
    2. .claude/settings.json 中的 mcpServers
    3. 项目根目录 .mcp.json 中的 mcpServers

    Args:
        project_root: 项目根目录路径
        yaml_config: 从 NexusConfig.mcp_servers 传入的配置（可选）

    Returns:
        合并后的 McpServerConfig 列表
    """
    project_root = Path(project_root).resolve()

    # 按优先级从低到高收集
    raw_servers: dict[str, dict[str, Any]] = {}

    # 1. YAML 配置（最低优先级）
    if yaml_config:
        raw_servers.update(yaml_config)

    # 2. .claude/settings.json
    raw_servers.update(_read_claude_settings(project_root))

    # 3. .mcp.json（最高优先级）
    raw_servers.update(_read_mcp_json(project_root))

    # 环境变量展开
    raw_servers = _expand_env_vars(raw_servers)

    # 转换为 McpServerConfig 列表
    configs = []
    for name, raw in raw_servers.items():
        if not isinstance(raw, dict):
            logger.warning("跳过无效的 MCP 服务器配置: %s", name)
            continue
        config = _raw_to_server_config(name, raw)
        if not config.command:
            logger.warning("MCP 服务器 %s 缺少 command 字段，跳过", name)
            continue
        configs.append(config)

    logger.info("共发现 %d 个 MCP 服务器配置", len(configs))
    return configs
