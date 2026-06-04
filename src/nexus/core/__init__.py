"""Nexus core modules."""

from nexus.core.agent import AgentConfig, AgentRunner, TurnResult
from nexus.core.config import NexusConfig, ProviderProfile, load_config
from nexus.core.context_engine import ContextEngine
from nexus.core.error_classifier import ClassifiedError, FailoverReason, classify_api_error
from nexus.core.iteration_budget import IterationBudget
from nexus.core.memory import MemoryManager, MemoryProvider
from nexus.core.retry_utils import jittered_backoff
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
    # Agent
    "AgentConfig",
    "AgentRunner",
    "TurnResult",
    # Config
    "NexusConfig",
    "ProviderProfile",
    "load_config",
    # Context
    "ContextEngine",
    # Error
    "ClassifiedError",
    "FailoverReason",
    "classify_api_error",
    # Budget
    "IterationBudget",
    # Memory
    "MemoryManager",
    "MemoryProvider",
    # Retry
    "jittered_backoff",
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
