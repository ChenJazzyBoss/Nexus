"""Tests for Phase 3 modules: Knowledge Store, MCP Hub."""

import json
import tempfile
from pathlib import Path

import pytest

from nexus.knowledge.session_store import SessionStore
from nexus.knowledge.search import (
    HybridSearch,
    SearchResult,
    apply_rrf_scores,
    extract_title,
    extract_wikilinks,
    keyword_search,
    tokenize_query,
)
from nexus.knowledge.graph import (
    GraphData,
    GraphEdge,
    GraphNode,
    build_graph,
    extract_type,
    extract_wikilinks as graph_extract_wikilinks,
    resolve_link,
)
from nexus.mcp_hub.client import McpHub, McpServerConfig, McpTool


# ── SessionStore Tests ──────────────────────────────────────────


class TestSessionStore:
    def test_create_and_load_session(self, tmp_path):
        """Should create a session and load its messages."""
        store = SessionStore(tmp_path / "test.db")

        session_id = store.create_session("test_agent")
        assert session_id

        store.save_message(session_id, "user", "Hello")
        store.save_message(session_id, "assistant", "Hi there!")

        messages = store.load_session(session_id)
        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert messages[1]["role"] == "assistant"

    def test_list_sessions(self, tmp_path):
        """Should list sessions."""
        store = SessionStore(tmp_path / "test.db")

        store.create_session("agent_a")
        store.create_session("agent_b")
        store.create_session("agent_a")

        sessions = store.list_sessions()
        assert len(sessions) == 3

        sessions_a = store.list_sessions(agent_id="agent_a")
        assert len(sessions_a) == 2

    def test_delete_session(self, tmp_path):
        """Should delete session and messages."""
        store = SessionStore(tmp_path / "test.db")

        session_id = store.create_session("test_agent")
        store.save_message(session_id, "user", "test")
        store.delete_session(session_id)

        assert store.get_session(session_id) is None
        assert store.load_session(session_id) == []

    def test_search_messages(self, tmp_path):
        """Should search messages by content."""
        store = SessionStore(tmp_path / "test.db")

        sid = store.create_session("test_agent")
        store.save_message(sid, "user", "Tell me about Transformer architecture")
        store.save_message(sid, "assistant", "The Transformer uses self-attention...")

        results = store.search_messages("Transformer")
        assert len(results) >= 1
        assert any("Transformer" in r["content"] for r in results)

    def test_context_manager(self, tmp_path):
        """Should work as context manager."""
        with SessionStore(tmp_path / "test.db") as store:
            sid = store.create_session("test")
            assert sid


# ── Tokenizer Tests ─────────────────────────────────────────────


class TestTokenizer:
    def test_english_tokens(self):
        """Should tokenize English text."""
        tokens = tokenize_query("multi-agent system")
        assert "multi-agent" in tokens or "multi" in tokens
        assert "agent" in tokens
        assert "system" in tokens

    def test_cjk_bigrams(self):
        """Should generate bigrams for CJK characters."""
        tokens = tokenize_query("注意力机制")
        assert len(tokens) >= 2
        assert "注意" in tokens

    def test_mixed_tokens(self):
        """Should handle mixed CJK and English."""
        tokens = tokenize_query("Transformer 注意力")
        assert "transformer" in tokens
        assert "注意" in tokens

    def test_empty_query(self):
        """Should handle empty query."""
        tokens = tokenize_query("")
        assert tokens == []


# ── Wikilink Tests ──────────────────────────────────────────────


class TestWikilinks:
    def test_extract_simple_link(self):
        """Should extract simple wikilinks."""
        links = extract_wikilinks("See [[Transformer]] for details.")
        assert links == ["Transformer"]

    def test_extract_alias_link(self):
        """Should extract target from alias links."""
        links = extract_wikilinks("See [[Transformer|this paper]] for details.")
        assert links == ["Transformer"]

    def test_extract_multiple_links(self):
        """Should extract multiple links."""
        links = extract_wikilinks("[[A]] and [[B]] and [[C]]")
        assert links == ["A", "B", "C"]

    def test_no_links(self):
        """Should return empty list when no links."""
        links = extract_wikilinks("No links here.")
        assert links == []


# ── Keyword Search Tests ────────────────────────────────────────


class TestKeywordSearch:
    def test_basic_search(self):
        """Should find documents matching query."""
        docs = {
            "attention.md": "# Attention Mechanism\nSelf-attention is a key component.",
            "transformer.md": "# Transformer\nThe Transformer architecture uses attention.",
            "rnn.md": "# RNN\nRecurrent neural networks process sequences.",
        }
        results = keyword_search(docs, "attention")
        assert len(results) >= 2
        assert any("attention" in r.title.lower() for r in results)

    def test_title_match_bonus(self):
        """Should rank title matches higher."""
        docs = {
            "attention.md": "# Attention Mechanism\nDetails about attention.",
            "transformer.md": "# Transformer\nUses attention mechanism.",
        }
        results = keyword_search(docs, "attention")
        # "attention.md" should rank higher (title match)
        assert results[0].path == "attention.md"

    def test_empty_query(self):
        """Should handle empty query."""
        docs = {"test.md": "# Test\nContent"}
        results = keyword_search(docs, "")
        assert results == []


# ── RRF Tests ───────────────────────────────────────────────────


class TestRRF:
    def test_rrf_fusion(self):
        """Should combine keyword and vector results."""
        keyword = [
            SearchResult(path="a.md", title="A", snippet="", score=10.0),
            SearchResult(path="b.md", title="B", snippet="", score=5.0),
        ]
        vector = [
            {"path": "b.md", "score": 0.9},
            {"path": "c.md", "score": 0.8},
        ]

        merged = apply_rrf_scores(keyword, vector)
        paths = [r.path for r in merged]

        # All three should appear
        assert "a.md" in paths
        assert "b.md" in paths
        assert "c.md" in paths

        # b.md should rank highest (appears in both)
        assert merged[0].path == "b.md"


# ── Knowledge Graph Tests ───────────────────────────────────────


class TestKnowledgeGraph:
    def test_build_simple_graph(self):
        """Should build graph from documents with wikilinks."""
        docs = {
            "attention": "# Attention\nSee [[Transformer]].",
            "transformer": "# Transformer\nUses [[Attention]].",
            "rnn": "# RNN\nNo links.",
        }
        graph = build_graph(docs)

        assert len(graph.nodes) == 3
        assert len(graph.edges) == 1  # attention <-> transformer
        assert graph.edges[0].weight == 1.0

    def test_self_links_ignored(self):
        """Should ignore self-links."""
        docs = {
            "self": "# Self\nLinks to [[Self]].",
        }
        graph = build_graph(docs)
        assert len(graph.edges) == 0

    def test_extract_type_from_frontmatter(self):
        """Should extract type from YAML frontmatter."""
        content = "---\ntype: paper\ntitle: Test\n---\n# Content"
        assert extract_type(content) == "paper"

    def test_extract_type_default(self):
        """Should return 'other' when no type specified."""
        content = "# No frontmatter"
        assert extract_type(content) == "other"

    def test_resolve_link_exact(self):
        """Should resolve exact match."""
        ids = {"transformer", "attention"}
        assert resolve_link("transformer", ids) == "transformer"

    def test_resolve_link_case_insensitive(self):
        """Should resolve case-insensitive match."""
        ids = {"Transformer"}
        assert resolve_link("transformer", ids) == "Transformer"

    def test_resolve_link_normalized(self):
        """Should resolve with space-to-hyphen normalization."""
        ids = {"multi-agent"}
        assert resolve_link("multi agent", ids) == "multi-agent"


# ── McpHub Tests ────────────────────────────────────────────────


class TestMcpHub:
    def test_mcp_tool_qualified_name(self):
        """Should generate qualified name."""
        tool = McpTool(name="search", server_name="arxiv")
        assert tool.qualified_name == "mcp_arxiv__search"

    def test_hub_initial_state(self):
        """Hub should start empty."""
        hub = McpHub()
        assert hub.get_all_tools() == []
        assert hub.connected_servers == []

    def test_hub_get_tools_for_registry(self):
        """Should return tools in registry format."""
        hub = McpHub()
        hub._tools["mcp_test__search"] = McpTool(
            name="search",
            server_name="test",
            description="Search papers",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        )

        tools = hub.get_tools_for_registry()
        assert len(tools) == 1
        assert tools[0]["name"] == "mcp_test__search"
        assert tools[0]["description"] == "Search papers"
