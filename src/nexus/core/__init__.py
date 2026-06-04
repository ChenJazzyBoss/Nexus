"""Nexus core modules."""

from nexus.core.config import NexusConfig, ProviderProfile, load_config
from nexus.core.context_engine import ContextEngine
from nexus.core.iteration_budget import IterationBudget
from nexus.core.memory import MemoryManager, MemoryProvider
from nexus.core.tool_engine import (
    CoreTool,
    PermissionResult,
    ToolEntry,
    ToolRegistry,
    ToolResult,
    registry,
    tool_error,
    tool_result,
)

__all__ = [
    # Config
    "NexusConfig",
    "ProviderProfile",
    "load_config",
    # Context
    "ContextEngine",
    # Budget
    "IterationBudget",
    # Memory
    "MemoryManager",
    "MemoryProvider",
    # Tools
    "CoreTool",
    "PermissionResult",
    "ToolEntry",
    "ToolRegistry",
    "ToolResult",
    "registry",
    "tool_error",
    "tool_result",
]
