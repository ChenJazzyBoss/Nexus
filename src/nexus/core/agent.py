"""Agent runtime — conversation loop, tool dispatch, iteration budget.

Adapted from hermes-agent run_agent.py and agent/conversation_loop.py.
The AgentRunner drives one conversation turn: model call → tool dispatch →
retry → budget check → return.

This module depends on:
  - nexus.core.config (ProviderProfile, NexusConfig)
  - nexus.core.tool_engine (ToolRegistry, registry)
  - nexus.core.context_engine (ContextEngine)
  - nexus.core.memory (MemoryManager)
  - nexus.core.iteration_budget (IterationBudget)
  - nexus.core.error_classifier (classify_api_error)
  - nexus.core.retry_utils (jittered_backoff)
  - nexus.transports.base (ProviderTransport, NormalizedResponse)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI, OpenAI

from nexus.core.config import NexusConfig, ProviderProfile
from nexus.core.context_engine import ContextEngine
from nexus.core.error_classifier import ClassifiedError, classify_api_error
from nexus.core.iteration_budget import IterationBudget
from nexus.core.memory import MemoryManager
from nexus.core.retry_utils import jittered_backoff
from nexus.core.tool_engine import ToolRegistry, registry as global_registry

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────

MAX_RETRIES = 3
TOOL_CALL_TIMEOUT_SECONDS = 120


# ── Data classes ────────────────────────────────────────────────


@dataclass
class AgentConfig:
    """Configuration for a single agent instance."""

    agent_id: str = ""
    model: str = "gpt-4o"
    provider: str = "openai"
    system_prompt: str = "You are a helpful AI assistant."
    max_iterations: int = 90
    tool_names: set[str] = field(default_factory=set)
    temperature: float | None = None
    max_tokens: int | None = None

    def __post_init__(self):
        if not self.agent_id:
            self.agent_id = f"agent_{uuid.uuid4().hex[:8]}"


@dataclass
class TurnResult:
    """Result of a single conversation turn."""

    content: str = ""
    tool_calls_made: int = 0
    tokens_used: dict[str, int] = field(default_factory=dict)
    error: str | None = None


# ── AgentRunner ─────────────────────────────────────────────────


class AgentRunner:
    """Single agent instance with isolated context, tools, and budget.

    Drives the conversation loop:
      1. Build messages (system + history + user)
      2. Call LLM
      3. If tool calls → execute tools → loop back to 2
      4. If text response → return
      5. Budget check, retry on error
    """

    def __init__(
        self,
        config: AgentConfig,
        nexus_config: NexusConfig,
        tool_registry: ToolRegistry | None = None,
        context_engine: ContextEngine | None = None,
        memory_manager: MemoryManager | None = None,
    ):
        self.config = config
        self.nexus_config = nexus_config
        self.tool_registry = tool_registry or global_registry
        self.context_engine = context_engine
        self.memory_manager = memory_manager

        self.budget = IterationBudget(config.max_iterations)
        self.messages: list[dict[str, Any]] = []
        self.session_id: str = uuid.uuid4().hex

        # LLM client (lazy init)
        self._client: AsyncOpenAI | None = None
        self._provider_profile: ProviderProfile | None = None

    @property
    def agent_id(self) -> str:
        return self.config.agent_id

    @property
    def provider_profile(self) -> ProviderProfile:
        if self._provider_profile is None:
            self._provider_profile = self.nexus_config.get_provider_profile(
                self.config.provider
            )
        return self._provider_profile

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            profile = self.provider_profile
            api_key = profile.get_api_key() or "dummy"
            self._client = AsyncOpenAI(
                api_key=api_key,
                base_url=profile.base_url or None,
                default_headers=profile.default_headers or None,
            )
        return self._client

    def get_system_prompt(self) -> str:
        """Build the complete system prompt including memory context."""
        parts = [self.config.system_prompt]

        if self.memory_manager:
            memory_block = self.memory_manager.build_system_prompt()
            if memory_block:
                parts.append(memory_block)

        if self.context_engine:
            engine_status = self.context_engine.get_status()
            if engine_status.get("compression_count", 0) > 0:
                parts.append(
                    f"[Context has been compressed {engine_status['compression_count']} times]"
                )

        return "\n\n".join(parts)

    def _get_tool_definitions(self) -> list[dict[str, Any]]:
        """Get OpenAI-format tool definitions for this agent's toolset."""
        if not self.config.tool_names:
            return []
        return self.tool_registry.get_definitions(self.config.tool_names)

    async def run_turn(self, user_message: str) -> TurnResult:
        """Execute one conversation turn.

        Args:
            user_message: The user's input message.

        Returns:
            TurnResult with the agent's response.
        """
        # Add user message
        self.messages.append({"role": "user", "content": user_message})

        # Memory prefetch
        if self.memory_manager:
            context = self.memory_manager.prefetch_all(
                user_message, session_id=self.session_id
            )
            if context:
                # Inject as system context (not visible to user)
                pass

        # Context engine lifecycle
        if self.context_engine:
            self.context_engine.on_session_start(self.session_id)

        # Run the conversation loop
        result = await self._conversation_loop()

        # Memory sync
        if self.memory_manager and result.content:
            self.memory_manager.sync_turn(
                user_message, result.content, session_id=self.session_id
            )

        return result

    async def _conversation_loop(self) -> TurnResult:
        """The core conversation loop: model call → tool dispatch → repeat."""
        tools = self._get_tool_definitions()
        total_tool_calls = 0
        tokens_used: dict[str, int] = {}

        for attempt in range(MAX_RETRIES + 1):
            try:
                while True:
                    # Budget check
                    if not self.budget.consume():
                        logger.warning(
                            "Agent %s: iteration budget exhausted", self.agent_id
                        )
                        return TurnResult(
                            content="[Budget exhausted]",
                            tool_calls_made=total_tool_calls,
                            tokens_used=tokens_used,
                        )

                    # Build messages
                    full_messages = self._build_messages()

                    # Call LLM
                    response = await self._call_llm(full_messages, tools)

                    # Update context engine
                    if self.context_engine and response.usage:
                        self.context_engine.update_from_response(response.usage)
                        tokens_used = response.usage

                    # Check for tool calls
                    if response.tool_calls:
                        total_tool_calls += len(response.tool_calls)

                        # Add assistant message with tool calls
                        self.messages.append({
                            "role": "assistant",
                            "content": response.content or "",
                            "tool_calls": [
                                {
                                    "id": tc["id"],
                                    "type": "function",
                                    "function": {
                                        "name": tc["name"],
                                        "arguments": json.dumps(tc["arguments"]),
                                    },
                                }
                                for tc in response.tool_calls
                            ],
                        })

                        # Execute tools
                        for tc in response.tool_calls:
                            tool_result = await self._execute_tool(
                                tc["name"], tc["arguments"]
                            )
                            self.messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": tool_result,
                            })

                        # Continue the loop
                        continue

                    # Text response — done
                    self.messages.append({
                        "role": "assistant",
                        "content": response.content,
                    })

                    return TurnResult(
                        content=response.content,
                        tool_calls_made=total_tool_calls,
                        tokens_used=tokens_used,
                    )

            except Exception as e:
                classified = classify_api_error(
                    e,
                    provider=self.config.provider,
                    model=self.config.model,
                )
                logger.warning(
                    "Agent %s: API error (attempt %d/%d): %s - %s",
                    self.agent_id, attempt + 1, MAX_RETRIES + 1,
                    classified.reason.value, classified.message,
                )

                if not classified.retryable or attempt >= MAX_RETRIES:
                    return TurnResult(
                        content="",
                        tool_calls_made=total_tool_calls,
                        tokens_used=tokens_used,
                        error=f"{classified.reason.value}: {classified.message}",
                    )

                # Context compression if needed
                if classified.should_compress and self.context_engine:
                    self.messages = self.context_engine.compress(self.messages)

                # Backoff
                delay = jittered_backoff(attempt + 1)
                logger.info("Retrying in %.1f seconds...", delay)
                await asyncio.sleep(delay)

        return TurnResult(
            content="",
            tool_calls_made=total_tool_calls,
            tokens_used=tokens_used,
            error="Max retries exceeded",
        )

    def _build_messages(self) -> list[dict[str, Any]]:
        """Build the full message list for the LLM call."""
        system_prompt = self.get_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self.messages)
        return messages

    async def _call_llm(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        """Call the LLM API."""
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if self.config.temperature is not None:
            kwargs["temperature"] = self.config.temperature

        max_tokens = self.config.max_tokens or self.provider_profile.get_max_tokens(
            self.config.model
        )
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        # Use extra_body from provider profile
        extra = self.provider_profile.build_extra_body()
        if extra:
            kwargs["extra_body"] = extra

        response = await self.client.chat.completions.create(**kwargs)

        # Normalize response
        choice = response.choices[0]
        content = choice.message.content or ""
        tool_calls = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        # Create a simple namespace object
        class Response:
            pass

        resp = Response()
        resp.content = content
        resp.tool_calls = tool_calls if tool_calls else None
        resp.usage = usage
        resp.finish_reason = choice.finish_reason

        return resp

    async def _execute_tool(self, name: str, args: dict[str, Any]) -> str:
        """Execute a tool call with timeout."""
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self.tool_registry.dispatch, name, args),
                timeout=TOOL_CALL_TIMEOUT_SECONDS,
            )
            # Truncate if needed
            entry = self.tool_registry.get_entry(name)
            if entry and entry.max_result_size_chars and len(result) > entry.max_result_size_chars:
                result = result[:entry.max_result_size_chars] + "\n...[truncated]"
            return result
        except asyncio.TimeoutError:
            logger.warning("Tool %s timed out after %ds", name, TOOL_CALL_TIMEOUT_SECONDS)
            return json.dumps({"error": f"Tool '{name}' timed out"})
        except Exception as e:
            logger.exception("Tool %s execution failed", name)
            return json.dumps({"error": f"Tool '{name}' failed: {str(e)}"})

    def reset(self) -> None:
        """Reset the agent's conversation state."""
        self.messages.clear()
        self.budget = IterationBudget(self.config.max_iterations)
        self.session_id = uuid.uuid4().hex
        if self.context_engine:
            self.context_engine.on_session_reset()
