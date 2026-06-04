"""Pluggable memory system for persistent recall across sessions.

Adapted from hermes-agent/agent/memory_provider.py and memory_manager.py.
Memory providers give the agent persistent recall across sessions.
The MemoryManager enforces a one-external-provider limit to prevent
tool schema bloat and conflicting memory backends.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class MemoryProvider(ABC):
    """Abstract base class for memory providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this provider (e.g. 'builtin', 'honcho')."""

    # -- Core lifecycle (implement these) ------------------------------------

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this provider is configured and ready."""

    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize for a session. May create resources, connections, etc."""

    def system_prompt_block(self) -> str:
        """Return text to include in the system prompt."""
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn."""
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn."""

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Persist a completed turn to the backend."""

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return tool schemas this provider exposes."""

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Handle a tool call for one of this provider's tools."""
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        """Clean shutdown — flush queues, close connections."""

    # -- Optional hooks ------------------------------------------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn with the user message."""

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Called when a session ends."""

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        """Called when the agent switches session_id mid-process."""

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Called before context compression discards old messages."""
        return ""

    def on_delegation(self, task: str, result: str, **kwargs) -> None:
        """Called on the PARENT agent when a subagent completes."""

    def on_memory_write(
        self, action: str, target: str, content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Called when the built-in memory tool writes an entry."""


class MemoryManager:
    """Orchestrator for memory providers.

    Manages registration, routes tool calls, builds system prompt blocks.
    Enforces a one-external-provider limit.
    """

    def __init__(self):
        self._builtin: MemoryProvider | None = None
        self._external: MemoryProvider | None = None

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a memory provider."""
        if provider.name == "builtin":
            self._builtin = provider
        else:
            if self._external is not None:
                logger.warning(
                    "Replacing external memory provider %s with %s",
                    self._external.name, provider.name,
                )
            self._external = provider

    @property
    def providers(self) -> list[MemoryProvider]:
        """Return all registered providers."""
        result = []
        if self._builtin:
            result.append(self._builtin)
        if self._external:
            result.append(self._external)
        return result

    def build_system_prompt(self) -> str:
        """Build memory context block for the system prompt."""
        blocks = []
        for p in self.providers:
            block = p.system_prompt_block()
            if block:
                blocks.append(block)
        if not blocks:
            return ""
        return build_memory_context_block("\n\n".join(blocks))

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """Run prefetch on all providers and combine results."""
        parts = []
        for p in self.providers:
            try:
                result = p.prefetch(query, session_id=session_id)
                if result:
                    parts.append(result)
            except Exception:
                logger.exception("Prefetch failed for provider %s", p.name)
        return "\n\n".join(parts)

    def sync_all(
        self,
        user_msg: str,
        assistant_msg: str,
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """Sync a completed turn to all providers."""
        for p in self.providers:
            try:
                p.sync_turn(
                    user_msg, assistant_msg,
                    session_id=session_id, messages=messages,
                )
            except Exception:
                logger.exception("Sync failed for provider %s", p.name)

    def shutdown(self) -> None:
        """Shutdown all providers."""
        for p in self.providers:
            try:
                p.shutdown()
            except Exception:
                logger.exception("Shutdown failed for provider %s", p.name)


def build_memory_context_block(raw: str) -> str:
    """Wrap memory context in a fenced block with system note."""
    return (
        "<memory-context>\n"
        "The following is recalled context from previous sessions.\n"
        "Use it to inform your responses, but do not mention it directly.\n\n"
        f"{raw}\n"
        "</memory-context>"
    )


def sanitize_context(text: str) -> str:
    """Strip injected memory fence tags from user input."""
    return re.sub(r"<memory-context>.*?</memory-context>", "", text, flags=re.DOTALL)


# ── Built-in Memory Provider ─────────────────────────────────


class BuiltinMemory(MemoryProvider):
    """Built-in memory provider backed by a local MEMORY.md file.

    Reads ~/.nexus/memory/MEMORY.md on initialization and injects its
    contents into the system prompt. No tools exposed.
    """

    MAX_CHARS = 10_000
    TRUNCATE_TO = 8_000

    def __init__(self, memory_dir: str | None = None):
        from pathlib import Path
        self._dir = Path(memory_dir) if memory_dir else Path.home() / ".nexus" / "memory"
        self._file = self._dir / "MEMORY.md"
        self._content: str = ""
        self._session_id: str = ""

    @property
    def name(self) -> str:
        return "builtin"

    def is_available(self) -> bool:
        return self._file.exists()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._dir.mkdir(parents=True, exist_ok=True)
        if not self._file.exists():
            self._file.write_text("", encoding="utf-8")
            logger.info("Created empty memory file: %s", self._file)
        self._load()

    def _load(self) -> None:
        """Load memory content from disk."""
        try:
            self._content = self._file.read_text(encoding="utf-8").strip()
        except Exception:
            logger.exception("Failed to read memory file: %s", self._file)
            self._content = ""

    def system_prompt_block(self) -> str:
        if not self._content:
            return ""
        content = self._content
        if len(content) > self.MAX_CHARS:
            content = content[: self.TRUNCATE_TO] + "\n[memory truncated]"
            logger.warning("Memory content truncated from %d to %d chars", len(self._content), self.TRUNCATE_TO)
        return content

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return []
