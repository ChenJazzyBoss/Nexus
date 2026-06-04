"""Concrete context compression engine.

Implements the 4-phase compression pipeline:
  1. Prune old tool results
  2. Determine cut boundary
  3. Generate summary via auxiliary LLM
  4. Reassemble messages

Ported from hermes-agent/context_engine.py patterns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from nexus.core.context_engine import ContextEngine

logger = logging.getLogger(__name__)


class ContextCompressor(ContextEngine):
    """Token-aware context compressor with LLM-based summarization."""

    def __init__(
        self,
        context_length: int = 128_000,
        threshold_percent: float = 0.50,
        protect_first_n: int = 3,
        protect_last_n: int = 6,
        aux_client: Any = None,
        aux_model: str = "",
    ):
        self.context_length = context_length
        self.threshold_percent = threshold_percent
        self.protect_first_n = protect_first_n
        self.protect_last_n = protect_last_n
        self.threshold_tokens = int(context_length * threshold_percent)

        self._aux_client = aux_client
        self._aux_model = aux_model
        self._previous_summary: str | None = None
        self._last_compression_savings: list[float] = []
        self._compression_count = 0

    @property
    def name(self) -> str:
        return "compressor"

    # -- Token tracking ----------------------------------------------------

    def update_from_response(self, usage: dict[str, Any]) -> None:
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
        self.last_completion_tokens = usage.get("completion_tokens", 0)
        self.last_total_tokens = usage.get("total_tokens", 0)

    # -- Compression triggers ----------------------------------------------

    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        tokens = prompt_tokens or self.last_prompt_tokens
        if tokens < self.threshold_tokens:
            return False

        # Debounce: skip if last two compressions saved very little
        if len(self._last_compression_savings) >= 2:
            s1, s2 = self._last_compression_savings[-2], self._last_compression_savings[-1]
            if s1 < 0.05 and s2 < 0.08:
                logger.warning(
                    "Compression skipped: last two savings %.1f%% and %.1f%% — too low",
                    s1 * 100, s2 * 100,
                )
                return False

        return True

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        """Quick rough token estimation. Chars/4 for English, chars/2 for Chinese."""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        # Detect if mostly Chinese
        chinese_chars = sum(1 for c in str(messages) if '一' <= c <= '鿿')
        ratio = 2 if chinese_chars / max(total_chars, 1) > 0.3 else 4
        estimated_tokens = total_chars // ratio
        return estimated_tokens >= self.threshold_tokens

    # -- Compression pipeline ----------------------------------------------

    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
    ) -> list[dict[str, Any]]:
        if self._compression_count >= 3:
            logger.error("Max compression rounds (3) reached, skipping")
            return messages

        before_tokens = current_tokens or self.last_prompt_tokens or self._estimate_tokens(messages)

        # Phase 1: Prune old tool results
        pruned = self._prune_old_tool_results(messages)

        # Phase 2: Determine cut boundary
        head, middle, tail = self._split_messages(pruned)

        # Phase 3: Generate summary
        summary = self._generate_summary_sync(middle, focus_topic)

        # Phase 4: Reassemble
        summary_msg = {"role": "user", "content": f"[Previous context summary]\n{summary}"}
        result = head + [summary_msg] + tail
        result = self._clean_orphans(result)

        after_tokens = self._estimate_tokens(result)
        savings = 1 - (after_tokens / max(before_tokens, 1))
        self._last_compression_savings.append(savings)
        self._compression_count += 1
        self._previous_summary = summary

        logger.info(
            "Context compressed: %d → %d tokens (%.0f%% savings), round %d",
            before_tokens, after_tokens, savings * 100, self._compression_count,
        )
        return result

    # -- Internal phases ---------------------------------------------------

    def _prune_old_tool_results(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Phase 1: Replace old tool results > 500 chars with 1-line summary."""
        # Find the last tool result index
        last_tool_idx = -1
        for i, m in enumerate(messages):
            if m.get("role") == "tool":
                last_tool_idx = i

        result = []
        for i, m in enumerate(messages):
            if m.get("role") == "tool" and i < last_tool_idx and len(str(m.get("content", ""))) > 500:
                tool_id = m.get("tool_call_id", "unknown")
                result.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "content": "[tool execution complete, output pruned]",
                })
            else:
                result.append(m)
        return result

    def _split_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        """Phase 2: Split into head/middle/tail, protecting first N and last N."""
        if len(messages) <= self.protect_first_n + self.protect_last_n:
            return messages, [], []

        head = messages[: self.protect_first_n]
        tail = messages[-self.protect_last_n :]
        middle = messages[self.protect_first_n: -self.protect_last_n]

        # If middle is empty after splitting, shrink tail protection
        if not middle and len(messages) > self.protect_first_n:
            head = messages[: self.protect_first_n]
            middle = messages[self.protect_first_n: -2] if len(messages) > self.protect_first_n + 2 else []
            tail = messages[-2:] if len(messages) > 2 else messages[-1:]

        return head, middle, tail

    def _generate_summary_sync(
        self, middle: list[dict[str, Any]], focus_topic: str | None = None
    ) -> str:
        """Phase 3: Generate summary via auxiliary LLM (sync wrapper)."""
        if not middle:
            return self._previous_summary or "[No messages to summarize]"

        # Build prompt for summarization
        middle_text = "\n".join(
            f"[{m.get('role', '?')}] {str(m.get('content', ''))[:500]}"
            for m in middle
        )

        prompt_parts = [
            "Summarize the following conversation in under 500 characters.",
            "Structure: Resolved issues, Pending issues, Active Task, Remaining Work.",
        ]
        if self._previous_summary:
            prompt_parts.append(f"Previous summary to merge with:\n{self._previous_summary}")
        if focus_topic:
            prompt_parts.append(f"Focus on: {focus_topic}")

        prompt_parts.append(f"\nConversation:\n{middle_text}")
        full_prompt = "\n".join(prompt_parts)

        # Try auxiliary LLM
        if self._aux_client and self._aux_model:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside an async context, use asyncio.to_thread
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(self._call_aux_llm_sync, full_prompt)
                        return future.result(timeout=30)
                else:
                    return self._call_aux_llm_sync(full_prompt)
            except Exception as e:
                logger.error("Auxiliary LLM failed: %s, falling back to truncation", e)

        # Fallback: simple truncation
        return self._fallback_summary(middle)

    def _call_aux_llm_sync(self, prompt: str) -> str:
        """Call auxiliary LLM synchronously."""
        import openai
        client = openai.OpenAI(
            api_key=self._aux_client.api_key if hasattr(self._aux_client, 'api_key') else "dummy",
            base_url=self._aux_client.base_url if hasattr(self._aux_client, 'base_url') else None,
        )
        resp = client.chat.completions.create(
            model=self._aux_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.3,
        )
        return resp.choices[0].message.content or "[Summary generation failed]"

    def _fallback_summary(self, middle: list[dict[str, Any]]) -> str:
        """Fallback: truncate middle messages to a simple summary."""
        roles = [m.get("role", "?") for m in middle]
        return f"[{len(middle)} messages pruned: {' → '.join(roles[:10])}]"

    def _clean_orphans(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Remove orphaned tool_call/tool_result pairs."""
        # Collect all tool_call_ids referenced by tool results
        tool_ids = {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}

        result = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                # Check if any tool_calls have matching results
                valid_calls = [
                    tc for tc in m["tool_calls"]
                    if tc.get("id") in tool_ids
                ]
                if valid_calls:
                    m = {**m, "tool_calls": valid_calls}
                    result.append(m)
                # Skip assistant messages with no valid tool_calls
            else:
                result.append(m)
        return result

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """Rough token estimate for a message list."""
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        return total_chars // 4

    # -- Model switch support ----------------------------------------------

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: str = "",
        provider: str = "",
        api_mode: str = "",
    ) -> None:
        self.context_length = context_length
        self.threshold_tokens = int(context_length * self.threshold_percent)
