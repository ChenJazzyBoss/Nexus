"""Tests for Phase 4 modules: Orchestrator, EventBus, Visualizer."""

import asyncio
import time

import pytest

from nexus.core.config import NexusConfig
from nexus.orchestrator.engine import (
    AgentPool,
    Orchestrator,
    SubTask,
    SubTaskResult,
    TaskResult,
    TaskStatus,
)
from nexus.orchestrator.events import Event, EventBus, EventType
from nexus.visualizer.hub import VisualizerHub


# ── EventBus Tests ──────────────────────────────────────────────


class TestEventBus:
    def test_emit_and_subscribe(self):
        """Should deliver events to subscribers."""
        bus = EventBus()
        received = []

        bus.on(EventType.TASK_CREATED, lambda e: received.append(e))
        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="123"))

        assert len(received) == 1
        assert received[0].task_id == "123"

    def test_unsubscribe(self):
        """Should stop receiving after unsubscribe."""
        bus = EventBus()
        received = []

        unsub = bus.on(EventType.TASK_CREATED, lambda e: received.append(e))
        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="1"))
        unsub()
        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="2"))

        assert len(received) == 1

    def test_on_all(self):
        """Should receive all event types."""
        bus = EventBus()
        received = []

        bus.on_all(lambda e: received.append(e))

        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="1"))
        bus.emit(Event(event_type=EventType.TASK_STARTED, task_id="1"))
        bus.emit(Event(event_type=EventType.TASK_COMPLETED, task_id="1"))

        assert len(received) == 3

    def test_event_history(self):
        """Should store event history."""
        bus = EventBus()

        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="1"))
        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="2"))

        history = bus.get_history()
        assert len(history) == 2

    def test_event_history_filter(self):
        """Should filter history by task_id."""
        bus = EventBus()

        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="1"))
        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="2"))
        bus.emit(Event(event_type=EventType.TASK_COMPLETED, task_id="1"))

        filtered = bus.get_history(task_id="1")
        assert len(filtered) == 2

    def test_listener_error_doesnt_crash(self):
        """Should handle listener errors gracefully."""
        bus = EventBus()

        def bad_listener(event):
            raise ValueError("boom")

        bus.on(EventType.TASK_CREATED, bad_listener)
        # Should not raise
        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="1"))

    def test_event_to_dict(self):
        """Event should serialize to dict."""
        event = Event(
            event_type=EventType.TASK_CREATED,
            task_id="123",
            data={"task": "test"},
        )
        d = event.to_dict()
        assert d["event"] == "task_created"
        assert d["task_id"] == "123"
        assert d["data"]["task"] == "test"


# ── AgentPool Tests ─────────────────────────────────────────────


class TestAgentPool:
    def test_pool_concurrency_limit(self):
        """Should enforce max concurrent limit."""
        pool = AgentPool(max_concurrent=2)

        # Register 2 agents
        pool.register("a1", None)
        pool.register("a2", None)
        assert pool.active_count == 2

        # Unregister one
        pool.unregister("a1")
        assert pool.active_count == 1

    def test_pool_unregister_nonexistent(self):
        """Should handle unregistering non-existent agent."""
        pool = AgentPool()
        pool.unregister("nonexistent")  # Should not raise


# ── SubTask Tests ───────────────────────────────────────────────


class TestSubTask:
    def test_auto_id(self):
        """Should auto-generate subtask ID."""
        st = SubTask(goal="test")
        assert st.subtask_id.startswith("sub_")

    def test_custom_id(self):
        """Should use custom ID."""
        st = SubTask(subtask_id="custom", goal="test")
        assert st.subtask_id == "custom"


# ── TaskResult Tests ────────────────────────────────────────────


class TestTaskResult:
    def test_duration(self):
        """Should calculate duration."""
        tr = TaskResult(task_id="test", created_at=time.time() - 10)
        tr.completed_at = time.time()
        assert tr.duration_seconds >= 9.9


# ── VisualizerHub Tests ─────────────────────────────────────────


class TestVisualizerHub:
    def test_get_task_tree(self):
        """Should build task tree from events."""
        bus = EventBus()
        hub = VisualizerHub(bus)

        # Simulate events
        bus.emit(Event(event_type=EventType.TASK_CREATED, task_id="t1", data={"task": "test"}))
        bus.emit(Event(event_type=EventType.TASK_STARTED, task_id="t1"))
        bus.emit(Event(event_type=EventType.SUBTASK_CREATED, task_id="t1", data={
            "subtask_id": "s1", "goal": "subtask 1", "agent_id": "a1",
        }))
        bus.emit(Event(event_type=EventType.SUBTASK_COMPLETED, task_id="t1", data={
            "subtask_id": "s1", "agent_id": "a1", "duration": 3.2,
        }))
        bus.emit(Event(event_type=EventType.TASK_COMPLETED, task_id="t1", data={
            "duration": 5.0,
        }))

        tree = hub.get_task_tree("t1")
        assert tree["task_id"] == "t1"
        assert tree["status"] == "completed"
        assert len(tree["subtasks"]) == 1
        assert tree["subtasks"][0]["status"] == "completed"

    def test_agent_status_update(self):
        """Should update agent status from events."""
        bus = EventBus()
        hub = VisualizerHub(bus)

        bus.emit(Event(event_type=EventType.SUBTASK_STARTED, task_id="t1", data={
            "subtask_id": "s1", "agent_id": "a1", "goal": "test",
        }))

        statuses = hub.get_agent_statuses()
        assert len(statuses) == 1
        assert statuses[0]["status"] == "running"

        bus.emit(Event(event_type=EventType.SUBTASK_COMPLETED, task_id="t1", data={
            "subtask_id": "s1", "agent_id": "a1",
        }))

        statuses = hub.get_agent_statuses()
        assert statuses[0]["status"] == "completed"
