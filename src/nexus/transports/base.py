"""Abstract base for provider transports.

Adapted from hermes-agent/agent/transports/base.py.
A transport owns the data path for one api_mode:
  convert_messages -> convert_tools -> build_kwargs -> normalize_response

It does NOT own: client construction, streaming, credential refresh,
prompt caching, interrupt handling, or retry logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedResponse:
    """Normalized response from any LLM provider."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    finish_reason: str = ""

    # Optional extended fields
    reasoning_content: str = ""
    cache_stats: dict[str, int] | None = None


class ProviderTransport(ABC):
    """Base class for provider-specific format conversion and normalization."""

    @property
    @abstractmethod
    def api_mode(self) -> str:
        """The api_mode string this transport handles."""
        ...

    @abstractmethod
    def convert_messages(
        self, messages: list[dict[str, Any]], **kwargs
    ) -> Any:
        """Convert OpenAI-format messages to provider-native format."""
        ...

    @abstractmethod
    def convert_tools(self, tools: list[dict[str, Any]]) -> Any:
        """Convert OpenAI-format tool definitions to provider-native format."""
        ...

    @abstractmethod
    def build_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **params,
    ) -> dict[str, Any]:
        """Build the complete API call kwargs dict."""
        ...

    @abstractmethod
    def normalize_response(self, response: Any, **kwargs) -> NormalizedResponse:
        """Normalize a raw provider response to NormalizedResponse."""
        ...

    def validate_response(self, response: Any) -> bool:
        """Optional: check if the raw response is structurally valid."""
        return True

    def extract_cache_stats(self, response: Any) -> dict[str, int] | None:
        """Optional: extract provider-specific cache hit/creation stats."""
        return None

    def map_finish_reason(self, raw_reason: str) -> str:
        """Optional: map provider-specific stop reason to OpenAI equivalent."""
        return raw_reason
