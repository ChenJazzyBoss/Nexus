"""Tool registry and execution engine.

Adapted from hermes-agent/tools/registry.py and claude-code CoreTool interface.
Provides a self-registering tool system with toolset grouping, dispatch,
and check_fn TTL cache.

Usage:
    from nexus.core.tool_engine import registry, tool_error, tool_result

    # Register a tool at module level
    registry.register(
        name="search",
        toolset="knowledge",
        schema={"description": "Search documents", "parameters": {...}},
        handler=my_search_handler,
    )

    # Dispatch a tool call
    result = registry.dispatch("search", {"query": "attention mechanism"})
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CoreTool Protocol (adapted from claude-code packages/agent-tools)
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Result from a tool execution."""
    data: Any = None
    error: str | None = None
    is_error: bool = False

    def to_json(self) -> str:
        if self.is_error:
            return json.dumps({"error": self.error}, ensure_ascii=False)
        return json.dumps(self.data, ensure_ascii=False)


@dataclass
class PermissionResult:
    """Result from a permission check."""
    behavior: str = "allow"  # allow | deny | passthrough
    reason: str = ""


@runtime_checkable
class CoreTool(Protocol):
    """Protocol defining the minimal interface for a Nexus tool."""

    name: str
    input_schema: dict[str, Any]
    max_result_size_chars: int

    async def call(self, args: dict[str, Any], context: Any) -> ToolResult:
        """Execute the tool."""
        ...

    def is_read_only(self, args: dict[str, Any]) -> bool:
        """Return True if this tool call doesn't modify state."""
        ...

    def is_destructive(self, args: dict[str, Any]) -> bool:
        """Return True if this tool call is destructive."""
        ...

    def check_permissions(
        self, args: dict[str, Any], context: Any
    ) -> PermissionResult:
        """Check if this tool call is permitted."""
        ...


# ---------------------------------------------------------------------------
# ToolEntry
# ---------------------------------------------------------------------------

@dataclass
class ToolEntry:
    """Metadata for a single registered tool."""

    name: str
    toolset: str
    schema: dict[str, Any]
    handler: Callable
    check_fn: Callable | None = None
    requires_env: list[str] = field(default_factory=list)
    is_async: bool = False
    description: str = ""
    emoji: str = "⚡"
    max_result_size_chars: int | float | None = None
    dynamic_schema_overrides: Callable | None = None


# ---------------------------------------------------------------------------
# check_fn TTL cache
# ---------------------------------------------------------------------------

_CHECK_FN_TTL_SECONDS = 30.0
_check_fn_cache: dict[Callable, tuple[float, bool]] = {}
_check_fn_cache_lock = threading.Lock()


def _check_fn_cached(fn: Callable) -> bool:
    """Return bool(fn()), TTL-cached across calls. Swallows exceptions as False."""
    now = time.monotonic()
    with _check_fn_cache_lock:
        cached = _check_fn_cache.get(fn)
        if cached is not None:
            ts, value = cached
            if now - ts < _CHECK_FN_TTL_SECONDS:
                return value
    try:
        value = bool(fn())
    except Exception:
        value = False
    with _check_fn_cache_lock:
        _check_fn_cache[fn] = (now, value)
    return value


def invalidate_check_fn_cache() -> None:
    """Drop all cached check_fn results."""
    with _check_fn_cache_lock:
        _check_fn_cache.clear()


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Singleton registry that collects tool schemas + handlers from tool files."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._toolset_checks: dict[str, Callable] = {}
        self._lock = threading.RLock()
        self._generation: int = 0

    def _snapshot_entries(self) -> list[ToolEntry]:
        """Return a stable snapshot of registered tool entries."""
        with self._lock:
            return list(self._tools.values())

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Callable | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "⚡",
        max_result_size_chars: int | float | None = None,
        dynamic_schema_overrides: Callable | None = None,
        override: bool = False,
    ) -> None:
        """Register a tool. Called at module-import time by each tool file."""
        with self._lock:
            existing = self._tools.get(name)
            if existing and existing.toolset != toolset and not override:
                logger.error(
                    "Tool registration REJECTED: '%s' (toolset '%s') would "
                    "shadow existing tool from toolset '%s'.",
                    name, toolset, existing.toolset,
                )
                return
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                requires_env=requires_env or [],
                is_async=is_async,
                description=description or schema.get("description", ""),
                emoji=emoji,
                max_result_size_chars=max_result_size_chars,
                dynamic_schema_overrides=dynamic_schema_overrides,
            )
            if check_fn and toolset not in self._toolset_checks:
                self._toolset_checks[toolset] = check_fn
            self._generation += 1

    def deregister(self, name: str) -> None:
        """Remove a tool from the registry."""
        with self._lock:
            entry = self._tools.pop(name, None)
            if entry is None:
                return
            toolset_still_exists = any(
                e.toolset == entry.toolset for e in self._tools.values()
            )
            if not toolset_still_exists:
                self._toolset_checks.pop(entry.toolset, None)
            self._generation += 1

    def get_entry(self, name: str) -> ToolEntry | None:
        """Return a registered tool entry by name, or None."""
        with self._lock:
            return self._tools.get(name)

    def get_definitions(
        self, tool_names: set[str] | None = None, quiet: bool = False
    ) -> list[dict]:
        """Return OpenAI-format tool schemas for the requested tool names."""
        result = []
        check_results: dict[Callable, bool] = {}
        entries = self._snapshot_entries()

        for entry in entries:
            if tool_names is not None and entry.name not in tool_names:
                continue
            if entry.check_fn:
                if entry.check_fn not in check_results:
                    check_results[entry.check_fn] = _check_fn_cached(entry.check_fn)
                if not check_results[entry.check_fn]:
                    if not quiet:
                        logger.debug("Tool %s unavailable (check failed)", entry.name)
                    continue
            schema_with_name = {**entry.schema, "name": entry.name}
            if entry.dynamic_schema_overrides is not None:
                try:
                    overrides = entry.dynamic_schema_overrides()
                    if isinstance(overrides, dict):
                        schema_with_name.update(overrides)
                except Exception as exc:
                    logger.warning(
                        "dynamic_schema_overrides for tool %s raised %s",
                        entry.name, exc,
                    )
            result.append({"type": "function", "function": schema_with_name})
        return result

    def dispatch(self, name: str, args: dict, **kwargs) -> str:
        """Execute a tool handler by name."""
        entry = self.get_entry(name)
        if not entry:
            return json.dumps({"error": f"Unknown tool: {name}"})
        try:
            if entry.is_async:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(asyncio.run, entry.handler(args, **kwargs))
                        return future.result()
                return asyncio.run(entry.handler(args, **kwargs))
            return entry.handler(args, **kwargs)
        except Exception as e:
            logger.exception("Tool %s dispatch error: %s", name, e)
            raw = f"Tool execution failed: {type(e).__name__}: {e}"
            return json.dumps({"error": raw})

    def get_all_tool_names(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(entry.name for entry in self._snapshot_entries())

    def get_toolset_for_tool(self, name: str) -> str | None:
        """Return the toolset a tool belongs to, or None."""
        entry = self.get_entry(name)
        return entry.toolset if entry else None

    def get_available_toolsets(self) -> dict[str, dict]:
        """Return toolset metadata for UI display."""
        toolsets: dict[str, dict] = {}
        entries = self._snapshot_entries()
        for entry in entries:
            ts = entry.toolset
            if ts not in toolsets:
                toolsets[ts] = {
                    "available": True,
                    "tools": [],
                    "description": "",
                    "requirements": [],
                }
            toolsets[ts]["tools"].append(entry.name)
            if entry.requires_env:
                for env in entry.requires_env:
                    if env not in toolsets[ts]["requirements"]:
                        toolsets[ts]["requirements"].append(env)
        return toolsets


# Module-level singleton
registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Helpers for tool response serialization
# ---------------------------------------------------------------------------

def tool_error(message: str, **extra: Any) -> str:
    """Return a JSON error string for tool handlers."""
    result = {"error": str(message)}
    if extra:
        result.update(extra)
    return json.dumps(result, ensure_ascii=False)


def tool_result(data: Any = None, **kwargs: Any) -> str:
    """Return a JSON result string for tool handlers."""
    if data is not None:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps(kwargs, ensure_ascii=False)
