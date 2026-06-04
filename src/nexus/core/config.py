"""Provider profile and runtime configuration.

Adapted from hermes-agent/providers/base.py.
A ProviderProfile declares everything about an inference provider in one place:
auth, endpoints, client quirks, request-time quirks.

Provider profiles are DECLARATIVE — they describe the provider's behavior.
They do NOT own client construction, credential rotation, or streaming.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Sentinel for "omit temperature entirely"
OMIT_TEMPERATURE = object()


@dataclass
class ProviderProfile:
    """Base provider profile — subclass or instantiate with overrides."""

    # ── Identity ─────────────────────────────────────────────
    name: str
    api_mode: str = "chat_completions"  # chat_completions | anthropic_messages
    aliases: tuple[str, ...] = ()

    # ── Human-readable metadata ───────────────────────────────
    display_name: str = ""
    description: str = ""
    signup_url: str = ""

    # ── Auth & endpoints ─────────────────────────────────────
    env_vars: tuple[str, ...] = ()
    api_key: str = ""
    base_url: str = ""
    models_url: str = ""
    auth_type: str = "api_key"  # api_key | oauth | aws_sdk

    # ── Model catalog ─────────────────────────────────────────
    fallback_models: tuple[str, ...] = ()
    hostname: str = ""

    # ── Client-level quirks ──────────────────────────────────
    default_headers: dict[str, str] = field(default_factory=dict)

    # ── Request-level quirks ─────────────────────────────────
    fixed_temperature: Any = None
    default_max_tokens: int | None = None
    default_aux_model: str = ""

    def get_hostname(self) -> str:
        """Return the provider's base hostname for URL-based detection."""
        if self.hostname:
            return self.hostname
        if self.base_url:
            from urllib.parse import urlparse
            return urlparse(self.base_url).hostname or ""
        return ""

    def get_api_key(self) -> str | None:
        """Resolve API key: explicit field first, then environment variables."""
        if self.api_key:
            return self.api_key
        for var in self.env_vars:
            key = os.environ.get(var)
            if key:
                return key
        return None

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Provider-specific message preprocessing. Default: pass-through."""
        return messages

    def build_extra_body(self, **context: Any) -> dict[str, Any]:
        """Provider-specific extra_body fields."""
        return {}

    def get_max_tokens(self, model: str | None = None) -> int | None:
        """Return the default max_tokens cap for the model."""
        return self.default_max_tokens

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Fetch the live model list from the provider's models endpoint.

        Returns a list of model ID strings, or None if the fetch failed.
        """
        url = (self.models_url or "").strip()
        if not url:
            if not self.base_url:
                return None
            url = self.base_url.rstrip("/") + "/models"

        import json
        import urllib.request

        req = urllib.request.Request(url)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "nexus/0.1.0")
        for k, v in self.default_headers.items():
            req.add_header(k, v)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            items = data if isinstance(data, list) else data.get("data", [])
            return [m["id"] for m in items if isinstance(m, dict) and "id" in m]
        except Exception as exc:
            logger.debug("fetch_models(%s): %s", self.name, exc)
            return None


# ── Built-in provider profiles ────────────────────────────────

OPENAI = ProviderProfile(
    name="openai",
    api_mode="chat_completions",
    display_name="OpenAI",
    env_vars=("OPENAI_API_KEY",),
    base_url="https://api.openai.com/v1",
    fallback_models=("gpt-4o", "gpt-4o-mini", "gpt-4-turbo"),
)

ANTHROPIC = ProviderProfile(
    name="anthropic",
    api_mode="anthropic_messages",
    display_name="Anthropic",
    env_vars=("ANTHROPIC_API_KEY",),
    base_url="https://api.anthropic.com",
    fallback_models=("claude-sonnet-4-20250514", "claude-haiku-4-20250414"),
)

OPENROUTER = ProviderProfile(
    name="openrouter",
    api_mode="chat_completions",
    display_name="OpenRouter",
    env_vars=("OPENROUTER_API_KEY",),
    base_url="https://openrouter.ai/api/v1",
    fallback_models=("anthropic/claude-sonnet-4-20250514", "openai/gpt-4o"),
)

# ── Provider registry ─────────────────────────────────────────

_BUILTIN_PROVIDERS: dict[str, ProviderProfile] = {
    p.name: p for p in [OPENAI, ANTHROPIC, OPENROUTER]
}


def get_provider(name: str) -> ProviderProfile | None:
    """Look up a provider by name or alias."""
    if name in _BUILTIN_PROVIDERS:
        return _BUILTIN_PROVIDERS[name]
    for p in _BUILTIN_PROVIDERS.values():
        if name in p.aliases:
            return p
    return None


def register_provider(profile: ProviderProfile) -> None:
    """Register a custom provider profile."""
    _BUILTIN_PROVIDERS[profile.name] = profile


def list_providers() -> list[ProviderProfile]:
    """Return all registered providers."""
    return list(_BUILTIN_PROVIDERS.values())


# ── YAML config loader ────────────────────────────────────────

@dataclass
class NexusConfig:
    """Top-level Nexus configuration loaded from config.yaml."""

    project_name: str = "nexus"
    default_provider: str = "openai"
    default_model: str = "gpt-4o"
    max_iterations: int = 90
    max_concurrent_agents: int = 5
    context_threshold_percent: float = 0.75

    # Provider overrides
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)

    # MCP servers
    mcp_servers: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Knowledge store
    knowledge_db_path: str = "data/knowledge.db"
    vector_db_path: str = "data/lancedb"

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8600

    def get_provider_profile(self, name: str | None = None) -> ProviderProfile:
        """Get a provider profile, applying config overrides."""
        provider_name = name or self.default_provider
        profile = get_provider(provider_name)
        if profile is None:
            raise ValueError(f"Unknown provider: {provider_name}")

        # Apply config overrides
        overrides = self.providers.get(provider_name, {})
        if overrides:
            import dataclasses
            return dataclasses.replace(profile, **overrides)
        return profile


def load_config(path: str | Path = "config.yaml") -> NexusConfig:
    """Load Nexus configuration from a YAML file."""
    path = Path(path)
    if not path.exists():
        logger.info("Config file %s not found, using defaults", path)
        return NexusConfig()

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return NexusConfig(
        project_name=data.get("project_name", "nexus"),
        default_provider=data.get("default_provider", "openai"),
        default_model=data.get("default_model", "gpt-4o"),
        max_iterations=data.get("max_iterations", 90),
        max_concurrent_agents=data.get("max_concurrent_agents", 5),
        context_threshold_percent=data.get("context_threshold_percent", 0.75),
        providers=data.get("providers", {}),
        mcp_servers=data.get("mcp_servers", {}),
        knowledge_db_path=data.get("knowledge_db_path", "data/knowledge.db"),
        vector_db_path=data.get("vector_db_path", "data/lancedb"),
        api_host=data.get("api_host", "0.0.0.0"),
        api_port=data.get("api_port", 8600),
    )
