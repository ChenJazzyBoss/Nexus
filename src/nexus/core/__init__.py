"""Nexus core modules."""

from nexus.core.agent import AgentConfig, AgentRunner, TurnResult
from nexus.core.chunker import chunk_markdown
from nexus.core.config import NexusConfig, ProviderProfile, load_config
from nexus.core.context_engine import ContextEngine
from nexus.core.embedding import EmbeddingProvider
from nexus.core.error_classifier import ClassifiedError, FailoverReason, classify_api_error
from nexus.core.iteration_budget import IterationBudget
from nexus.core.memory import MemoryManager, MemoryProvider
from nexus.core.message_utils import clean_messages
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
    # Chunker
    "chunk_markdown",
    # Config
    "NexusConfig",
    "ProviderProfile",
    "load_config",
    # Context
    "ContextEngine",
    # Embedding
    "EmbeddingProvider",
    # Error
    "ClassifiedError",
    "FailoverReason",
    "classify_api_error",
    # Budget
    "IterationBudget",
    # Memory
    "MemoryManager",
    "MemoryProvider",
    # Message utils
    "clean_messages",
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
