"""Tests for Phase 2 modules: Agent, Error Classifier, Retry Utils."""

import json
import time

import pytest

from nexus.core.agent import AgentConfig, AgentRunner
from nexus.core.config import NexusConfig
from nexus.core.error_classifier import (
    ClassifiedError,
    FailoverReason,
    classify_api_error,
)
from nexus.core.retry_utils import jittered_backoff
from nexus.core.iteration_budget import IterationBudget


# ── Error Classifier Tests ──────────────────────────────────────


class TestErrorClassifier:
    def test_rate_limit_error(self):
        """429 should classify as rate_limit."""
        error = Exception("Rate limit exceeded")
        error.status_code = 429
        result = classify_api_error(error)
        assert result.reason == FailoverReason.rate_limit
        assert result.retryable is True
        assert result.should_rotate_credential is True

    def test_auth_error(self):
        """401 should classify as auth."""
        error = Exception("Unauthorized")
        error.status_code = 401
        result = classify_api_error(error)
        assert result.reason == FailoverReason.auth
        assert result.retryable is False

    def test_context_overflow_from_message(self):
        """Context overflow pattern in message should classify correctly."""
        error = Exception("context length exceeded")
        result = classify_api_error(error)
        assert result.reason == FailoverReason.context_overflow
        assert result.should_compress is True

    def test_billing_error(self):
        """Insufficient credits should classify as billing."""
        error = Exception("insufficient credits")
        result = classify_api_error(error)
        assert result.reason == FailoverReason.billing
        assert result.retryable is False

    def test_model_not_found(self):
        """Model not found should classify correctly."""
        error = Exception("model not found: gpt-99")
        result = classify_api_error(error, model="gpt-99")
        assert result.reason == FailoverReason.model_not_found
        assert result.should_fallback is True

    def test_timeout_error(self):
        """TimeoutError should classify as timeout."""
        error = TimeoutError("Connection timed out")
        result = classify_api_error(error)
        assert result.reason == FailoverReason.timeout
        assert result.retryable is True

    def test_server_error(self):
        """500 should classify as server_error."""
        error = Exception("Internal Server Error")
        error.status_code = 500
        result = classify_api_error(error)
        assert result.reason == FailoverReason.server_error
        assert result.retryable is True

    def test_content_policy_blocked(self):
        """Content policy block should not be retryable."""
        error = Exception("your request was flagged by content filter")
        result = classify_api_error(error)
        assert result.reason == FailoverReason.content_policy_blocked
        assert result.retryable is False

    def test_unknown_error_retryable(self):
        """Unknown errors should be retryable."""
        error = Exception("something weird happened")
        result = classify_api_error(error)
        assert result.reason == FailoverReason.unknown
        assert result.retryable is True


# ── Retry Utils Tests ───────────────────────────────────────────


class TestRetryUtils:
    def test_jittered_backoff_increases(self):
        """Backoff delay should increase with attempt number."""
        delays = [jittered_backoff(i, jitter_ratio=0) for i in range(1, 6)]
        # Without jitter, delays should be: 5, 10, 20, 40, 80
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1]

    def test_jittered_backoff_max_cap(self):
        """Backoff should not exceed max_delay."""
        delay = jittered_backoff(20, max_delay=60.0, jitter_ratio=0)
        assert delay <= 60.0

    def test_jittered_backoff_has_jitter(self):
        """With jitter, delays should vary."""
        delays = [jittered_backoff(3) for _ in range(100)]
        # Should not all be identical
        assert len(set(delays)) > 1


# ── AgentConfig Tests ───────────────────────────────────────────


class TestAgentConfig:
    def test_default_agent_id(self):
        """Agent ID should be auto-generated if not provided."""
        config = AgentConfig()
        assert config.agent_id.startswith("agent_")
        assert len(config.agent_id) == 14  # "agent_" + 8 hex chars

    def test_custom_agent_id(self):
        """Custom agent ID should be preserved."""
        config = AgentConfig(agent_id="my_agent")
        assert config.agent_id == "my_agent"

    def test_default_model(self):
        """Default model should be gpt-4o."""
        config = AgentConfig()
        assert config.model == "gpt-4o"


# ── AgentRunner Tests ───────────────────────────────────────────


class TestAgentRunner:
    def test_agent_initialization(self):
        """AgentRunner should initialize with correct config."""
        config = AgentConfig(agent_id="test_agent", model="gpt-4o")
        nexus_config = NexusConfig()
        agent = AgentRunner(config=config, nexus_config=nexus_config)

        assert agent.agent_id == "test_agent"
        assert agent.config.model == "gpt-4o"
        assert agent.budget.max_total == 90
        assert len(agent.messages) == 0

    def test_system_prompt(self):
        """System prompt should include base prompt."""
        config = AgentConfig(
            system_prompt="You are a research assistant.",
        )
        agent = AgentRunner(config=config, nexus_config=NexusConfig())
        prompt = agent.get_system_prompt()
        assert "research assistant" in prompt

    def test_reset(self):
        """Reset should clear messages and budget."""
        config = AgentConfig(max_iterations=10)
        agent = AgentRunner(config=config, nexus_config=NexusConfig())

        # Simulate some state
        agent.messages.append({"role": "user", "content": "hello"})
        agent.budget.consume()
        agent.budget.consume()

        old_session = agent.session_id
        agent.reset()

        assert len(agent.messages) == 0
        assert agent.budget.remaining == 10
        assert agent.session_id != old_session

    def test_tool_definitions_empty(self):
        """With no tools configured, should return empty list."""
        config = AgentConfig(tool_names=set())
        agent = AgentRunner(config=config, nexus_config=NexusConfig())
        assert agent._get_tool_definitions() == []

    def test_provider_profile(self):
        """Should resolve provider profile from config."""
        config = AgentConfig(provider="openai")
        agent = AgentRunner(config=config, nexus_config=NexusConfig())
        profile = agent.provider_profile
        assert profile.name == "openai"
        assert profile.base_url == "https://api.openai.com/v1"
