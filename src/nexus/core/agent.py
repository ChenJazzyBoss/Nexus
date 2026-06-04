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
from nexus.core.message_utils import clean_messages
from nexus.core.retry_utils import jittered_backoff
from nexus.core.tool_engine import ToolRegistry, registry as global_registry
from nexus.knowledge.session_store import SessionStore

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
        session_store: SessionStore | None = None,
    ):
        self.config = config
        self.nexus_config = nexus_config
        self.tool_registry = tool_registry or global_registry
        self.context_engine = context_engine
        self.memory_manager = memory_manager
        self.session_store = session_store

        self.budget = IterationBudget(config.max_iterations)
        self.messages: list[dict[str, Any]] = []
        self.session_id: str = uuid.uuid4().hex

        # LLM client (lazy init)
        self._client: AsyncOpenAI | None = None
        self._provider_profile: ProviderProfile | None = None

        # Provider 降级链
        self._fallback_chain: list[dict[str, Any]] = list(
            nexus_config.fallback_providers
        )
        self._current_fallback_idx: int = -1  # -1 表示使用主 provider

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

    def _try_activate_fallback(self) -> bool:
        """尝试切换到下一个 fallback provider。

        Returns:
            True 表示成功切换，False 表示没有更多 fallback 可用。
        """
        next_idx = self._current_fallback_idx + 1
        if next_idx >= len(self._fallback_chain):
            logger.warning(
                "Agent %s: 所有 fallback provider 已耗尽（共 %d 个）",
                self.agent_id, len(self._fallback_chain),
            )
            return False

        fb = self._fallback_chain[next_idx]
        provider_name = fb.get("provider", "")
        model = fb.get("model", "")
        base_url = fb.get("base_url", "")
        api_key = fb.get("api_key", "")

        if not provider_name or not model:
            logger.warning(
                "Agent %s: fallback #%d 配置无效（缺少 provider 或 model）",
                self.agent_id, next_idx,
            )
            return False

        logger.info(
            "Agent %s: 切换到 fallback provider #%d: %s/%s",
            self.agent_id, next_idx, provider_name, model,
        )

        self._current_fallback_idx = next_idx
        self.config.provider = provider_name
        self.config.model = model

        # 重建 provider profile 和 client
        # 直接用 fallback 配置构建 profile，不依赖注册表
        self._provider_profile = ProviderProfile(
            name=provider_name,
            api_key=api_key,
            base_url=base_url,
        )
        self._client = AsyncOpenAI(
            api_key=api_key or "dummy",
            base_url=base_url or None,
        )

        return True

    def _restore_primary(self) -> None:
        """恢复到主 provider（每个新 turn 开始时调用）。"""
        if self._current_fallback_idx < 0:
            return  # 没有切换过，不需要恢复

        logger.info(
            "Agent %s: 恢复到主 provider: %s/%s",
            self.agent_id,
            self.nexus_config.default_provider,
            self.nexus_config.default_model,
        )

        self._current_fallback_idx = -1
        self._client = None
        self._provider_profile = None

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
        # 每个新 turn 开始时恢复主 provider
        self._restore_primary()

        # Memory prefetch — inject into user message if available
        if self.memory_manager:
            context = self.memory_manager.prefetch_all(
                user_message, session_id=self.session_id
            )
            if context:
                user_message = f"<memory-context>\n{context}\n</memory-context>\n\n{user_message}"

        # Add user message
        self.messages.append({"role": "user", "content": user_message})

        # Context engine lifecycle
        if self.context_engine:
            self.context_engine.on_session_start(self.session_id)

        # Run the conversation loop
        result = await self._conversation_loop()

        # Memory sync (non-blocking)
        if self.memory_manager and result.content:
            try:
                asyncio.create_task(
                    asyncio.to_thread(
                        self.memory_manager.sync_turn,
                        user_message, result.content,
                        session_id=self.session_id,
                    )
                )
            except Exception:
                logger.exception("Memory sync scheduling failed")

        # Context engine session end
        if self.context_engine:
            self.context_engine.on_session_end(self.session_id, self.messages)

        # 会话持久化 — 将 user message 和 assistant reply 保存到 SessionStore
        if self.session_store and result.content:
            try:
                # 确保 session 存在（首次调用时创建）
                session_info = self.session_store.get_session(self.session_id)
                if session_info is None:
                    self.session_store.create_session(
                        agent_id=self.agent_id,
                        metadata={"session_id": self.session_id},
                    )
                self.session_store.save_message(self.session_id, "user", user_message)
                self.session_store.save_message(self.session_id, "assistant", result.content)
            except Exception:
                logger.exception("SessionStore 持久化失败")

        return result

    async def _conversation_loop(self) -> TurnResult:
        """The core conversation loop: model call → tool dispatch → repeat.

        当重试耗尽时，尝试切换到 fallback provider 并重新开始重试循环。
        """
        tools = self._get_tool_definitions()
        total_tool_calls = 0
        tokens_used: dict[str, int] = {}

        # Pre-loop preflight compression check
        if self.context_engine and self.context_engine.should_compress_preflight(self.messages):
            logger.info("Agent %s: preflight compression triggered", self.agent_id)
            self.messages = self.context_engine.compress(self.messages)

        # 外层循环：支持 fallback provider 切换后重新开始重试
        while True:
            last_error: str | None = None

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

                        # Pre-LLM-call compression check
                        if self.context_engine and self.context_engine.should_compress():
                            logger.info("Agent %s: compression triggered before LLM call", self.agent_id)
                            self.messages = self.context_engine.compress(self.messages)

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

                    last_error = f"{classified.reason.value}: {classified.message}"

                    if not classified.retryable or attempt >= MAX_RETRIES:
                        # 重试耗尽 —— 跳出重试循环，尝试 fallback
                        break

                    # Context compression if needed
                    if classified.should_compress and self.context_engine:
                        self.messages = self.context_engine.compress(self.messages)

                    # Backoff
                    delay = jittered_backoff(attempt + 1)
                    logger.info("Retrying in %.1f seconds...", delay)
                    await asyncio.sleep(delay)

            # 重试循环结束 —— 尝试 fallback provider
            if not self._try_activate_fallback():
                # 没有更多 fallback，返回错误
                return TurnResult(
                    content="",
                    tool_calls_made=total_tool_calls,
                    tokens_used=tokens_used,
                    error=last_error or "Max retries exceeded",
                )

            # fallback 成功，重新开始外层循环
            logger.info(
                "Agent %s: fallback 切换成功，重新开始重试循环",
                self.agent_id,
            )

    def _build_messages(self) -> list[dict[str, Any]]:
        """Build the full message list for the LLM call.

        在返回前调用 clean_messages 确保消息符合 API 规范。
        """
        system_prompt = self.get_system_prompt()
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self.messages)
        return clean_messages(messages)

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
