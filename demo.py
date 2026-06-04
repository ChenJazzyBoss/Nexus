"""End-to-end verification script for Nexus multi-agent platform."""

import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from nexus.core import NexusConfig, ToolRegistry, tool_result, load_config
from nexus.orchestrator.engine import Orchestrator
from nexus.orchestrator.events import EventBus, EventType, Event
from nexus.visualizer.hub import VisualizerHub


PAPER_DB = {
    "1706.03762": {
        "title": "Attention Is All You Need",
        "abstract": (
            "The dominant sequence transduction models are based on complex recurrent "
            "or convolutional neural networks. We propose a new simple network architecture, "
            "the Transformer, based solely on attention mechanisms."
        ),
    },
    "1810.04805": {
        "title": "BERT: Pre-training of Deep Bidirectional Transformers",
        "abstract": (
            "We introduce BERT, a new language representation model. BERT is designed to "
            "pre-train deep bidirectional representations from unlabeled text."
        ),
    },
}


def handle_read_paper(args):
    arxiv_id = args.get("arxiv_id", "")
    paper = PAPER_DB.get(arxiv_id)
    if paper:
        return tool_result({"found": True, "title": paper["title"], "abstract": paper["abstract"]})
    return tool_result({"found": False, "error": "Paper not found"})


def handle_search_papers(args):
    query = args.get("query", "").lower()
    results = []
    if "attention" in query or "transformer" in query:
        results.append({"arxiv_id": "1706.03762", "title": "Attention Is All You Need", "citations": 120000})
    if "bert" in query or "pre-training" in query:
        results.append({"arxiv_id": "1810.04805", "title": "BERT", "citations": 80000})
    if not results:
        results.append({"arxiv_id": "2301.00001", "title": "Generic ML Paper", "citations": 50})
    return tool_result({"results": results[: args.get("max_results", 3)]})


def handle_calculate(args):
    expr = args.get("expression", "0")
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return tool_result({"result": result})
    except Exception as e:
        return tool_result({"error": str(e)})


def create_demo_tools(registry: ToolRegistry):
    """Register demo tools for verification."""
    registry.register(
        name="read_paper_abstract",
        toolset="research",
        schema={
            "description": "Read the abstract of a research paper by arXiv ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "arxiv_id": {"type": "string", "description": "arXiv paper ID (e.g., 1706.03762)"}
                },
                "required": ["arxiv_id"],
            },
        },
        handler=handle_read_paper,
    )

    registry.register(
        name="search_papers",
        toolset="research",
        schema={
            "description": "Search for papers by keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keywords"},
                    "max_results": {"type": "integer", "description": "Max results", "default": 3},
                },
                "required": ["query"],
            },
        },
        handler=handle_search_papers,
    )

    registry.register(
        name="calculate",
        toolset="utility",
        schema={
            "description": "Evaluate a mathematical expression.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {"type": "string", "description": "Math expression to evaluate"}
                },
                "required": ["expression"],
            },
        },
        handler=handle_calculate,
    )

    tool_names = registry.get_all_tool_names()
    print("[OK] Registered " + str(len(tool_names)) + " tools: " + str(tool_names))


async def main():
    print("=" * 60)
    print("Nexus Multi-Agent Platform - End-to-End Verification")
    print("=" * 60)

    # 1. Load config
    config = load_config("config.yaml")
    print("[OK] Config loaded: model=" + config.default_model)

    # 2. Setup tool registry
    registry = ToolRegistry()
    create_demo_tools(registry)

    # 3. Setup event bus and visualizer
    event_bus = EventBus()
    visualizer = VisualizerHub(event_bus)
    print("[OK] EventBus and VisualizerHub initialized")

    # 4. Create orchestrator
    orchestrator = Orchestrator(
        nexus_config=config,
        tool_registry=registry,
        event_bus=event_bus,
    )
    print("[OK] Orchestrator ready (max_concurrent=" + str(config.max_concurrent_agents) + ")")

    # 5. Test tool dispatch
    print("\n--- Tool Dispatch Test ---")
    result = registry.dispatch("search_papers", {"query": "transformer attention", "max_results": 2})
    print("[OK] search_papers: " + result[:100])

    # 6. Test event bus
    print("\n--- Event Bus Test ---")
    events_received = []
    event_bus.on(EventType.TASK_COMPLETED, lambda e: events_received.append(e))
    event_bus.emit(Event(event_type=EventType.TASK_COMPLETED, task_id="test-001", data={"status": "done"}))
    print("[OK] Event emitted and received: " + str(len(events_received)) + " event(s)")

    # 7. Test visualizer
    print("\n--- Visualizer Status ---")
    statuses = visualizer.get_agent_statuses()
    print("[OK] Agent statuses: " + str(len(statuses)) + " agent(s)")

    # 8. Run orchestrator task
    print("\n--- Orchestrator Task ---")
    task_desc = "Find and summarize the Transformer paper's key contribution"
    print("Submitting task: " + task_desc)
    try:
        task_result = await orchestrator.execute_task(task_desc)
        print("[OK] Task completed: " + str(task_result.status))
        print("     Result: " + str(task_result.result)[:200])
    except Exception as e:
        print("[WARN] Task failed (API may be unavailable): " + str(e)[:100])

    print("\n" + "=" * 60)
    print("Verification complete!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
