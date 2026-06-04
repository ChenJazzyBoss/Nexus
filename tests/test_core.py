"""Tests for Nexus core modules."""

import json
import threading

import pytest

from nexus.core.config import (
    NexusConfig,
    ProviderProfile,
    get_provider,
    list_providers,
    load_config,
    register_provider,
)
from nexus.core.context_engine import ContextEngine
from nexus.core.iteration_budget import IterationBudget
from nexus.core.memory import MemoryManager, MemoryProvider, sanitize_context
from nexus.core.tool_engine import (
    ToolEntry,
    ToolRegistry,
    registry,
    tool_error,
    tool_result,
)


# ── IterationBudget Tests ──────────────────────────────────────


class TestIterationBudget:
    def test_consume_within_budget(self):
        budget = IterationBudget(5)
        for _ in range(5):
            assert budget.consume() is True
        assert budget.used == 5
        assert budget.remaining == 0

    def test_consume_exceeds_budget(self):
        budget = IterationBudget(2)
        assert budget.consume() is True
        assert budget.consume() is True
        assert budget.consume() is False
        assert budget.used == 2

    def test_refund(self):
        budget = IterationBudget(3)
        budget.consume()
        budget.consume()
        assert budget.used == 2
        budget.refund()
        assert budget.used == 1
        assert budget.remaining == 2

    def test_refund_at_zero(self):
        budget = IterationBudget(3)
        budget.refund()  # Should not go negative
        assert budget.used == 0

    def test_thread_safety(self):
        budget = IterationBudget(1000)
        results = []

        def consume_all():
            count = 0
            while budget.consume():
                count += 1
            results.append(count)

        threads = [threading.Thread(target=consume_all) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert budget.used == 1000
        assert sum(results) == 1000


# ── ProviderProfile Tests ──────────────────────────────────────


class TestProviderProfile:
    def test_openai_profile(self):
        p = get_provider("openai")
        assert p is not None
        assert p.name == "openai"
        assert p.api_mode == "chat_completions"
        assert "OPENAI_API_KEY" in p.env_vars

    def test_anthropic_profile(self):
        p = get_provider("anthropic")
        assert p is not None
        assert p.api_mode == "anthropic_messages"

    def test_unknown_provider(self):
        assert get_provider("nonexistent") is None

    def test_register_custom_provider(self):
        custom = ProviderProfile(
            name="test_provider",
            base_url="https://test.example.com/v1",
            env_vars=("TEST_API_KEY",),
        )
        register_provider(custom)
        assert get_provider("test_provider") is not None
        assert "test_provider" in [p.name for p in list_providers()]

    def test_hostname_from_base_url(self):
        p = ProviderProfile(name="test", base_url="https://api.example.com/v1")
        assert p.get_hostname() == "api.example.com"

    def test_hostname_explicit(self):
        p = ProviderProfile(name="test", hostname="custom.host.com")
        assert p.get_hostname() == "custom.host.com"


# ── NexusConfig Tests ──────────────────────────────────────────


class TestNexusConfig:
    def test_default_config(self):
        config = NexusConfig()
        assert config.default_provider == "openai"
        assert config.max_iterations == 90
        assert config.api_port == 8600

    def test_load_config_from_yaml(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "project_name: test\n"
            "default_provider: anthropic\n"
            "default_model: claude-sonnet-4-20250514\n"
            "max_iterations: 50\n"
        )
        config = load_config(config_file)
        assert config.project_name == "test"
        assert config.default_provider == "anthropic"
        assert config.max_iterations == 50

    def test_load_missing_config(self):
        config = load_config("/nonexistent/config.yaml")
        assert config.project_name == "nexus"  # defaults


# ── Memory Tests ───────────────────────────────────────────────


class TestMemoryManager:
    def test_sanitize_context(self):
        text = "Hello <memory-context>secret</memory-context> world"
        assert sanitize_context(text) == "Hello  world"

    def test_build_memory_context_block(self):
        from nexus.core.memory import build_memory_context_block
        block = build_memory_context_block("test context")
        assert "<memory-context>" in block
        assert "test context" in block


# ── ToolRegistry Tests ─────────────────────────────────────────


class TestToolRegistry:
    def test_register_and_dispatch(self):
        test_registry = ToolRegistry()

        def my_handler(args):
            return json.dumps({"result": args.get("query", "")})

        test_registry.register(
            name="test_tool",
            toolset="test",
            schema={"description": "A test tool", "parameters": {"type": "object"}},
            handler=my_handler,
        )

        assert "test_tool" in test_registry.get_all_tool_names()
        result = test_registry.dispatch("test_tool", {"query": "hello"})
        assert json.loads(result)["result"] == "hello"

    def test_dispatch_unknown_tool(self):
        test_registry = ToolRegistry()
        result = test_registry.dispatch("nonexistent", {})
        assert "Unknown tool" in json.loads(result)["error"]

    def test_toolset_filtering(self):
        test_registry = ToolRegistry()

        test_registry.register(
            name="tool_a", toolset="group_a",
            schema={"description": "A"}, handler=lambda args: "{}",
        )
        test_registry.register(
            name="tool_b", toolset="group_b",
            schema={"description": "B"}, handler=lambda args: "{}",
        )

        defs = test_registry.get_definitions({"tool_a"})
        assert len(defs) == 1
        assert defs[0]["function"]["name"] == "tool_a"

    def test_deregister(self):
        test_registry = ToolRegistry()
        test_registry.register(
            name="temp_tool", toolset="temp",
            schema={"description": "Temp"}, handler=lambda args: "{}",
        )
        assert "temp_tool" in test_registry.get_all_tool_names()
        test_registry.deregister("temp_tool")
        assert "temp_tool" not in test_registry.get_all_tool_names()

    def test_tool_error(self):
        result = json.loads(tool_error("something failed"))
        assert result["error"] == "something failed"

    def test_tool_result(self):
        result = json.loads(tool_result({"key": "value"}))
        assert result["key"] == "value"

    def test_tool_result_kwargs(self):
        result = json.loads(tool_result(success=True, count=42))
        assert result["success"] is True
        assert result["count"] == 42
