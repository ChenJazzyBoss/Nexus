"""Visualizer hub — SSE event stream for real-time agent monitoring.

Provides Server-Sent Events (SSE) endpoint for monitoring agent
collaboration in real time.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from nexus.orchestrator.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)


@dataclass
class AgentStatus:
    """Current status of an agent."""

    agent_id: str
    status: str = "idle"  # idle | running | completed | failed
    current_task: str = ""
    tool_calls: int = 0
    started_at: float | None = None


class VisualizerHub:
    """SSE endpoint for real-time agent monitoring.

    Usage:
        hub = VisualizerHub(event_bus)

        # In your SSE handler:
        async for event in hub.stream_events():
            yield f"data: {json.dumps(event)}\n\n"
    """

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._agents: dict[str, AgentStatus] = {}

        # Subscribe to events for automatic status updates
        self.event_bus.on_all(self._update_agent_status)

    async def stream_events(self) -> AsyncIterator[dict[str, Any]]:
        """Stream events as SSE-compatible dicts.

        Yields dicts with 'event' and 'data' keys.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue()

        # Subscribe to all events
        def on_event(event: Event):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

        unsubscribe = self.event_bus.on_all(on_event)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield event.to_dict()

                    # Update agent status based on events
                    self._update_agent_status(event)

                except asyncio.TimeoutError:
                    # Send keepalive
                    yield {"event": "keepalive", "data": {"timestamp": time.time()}}

        except asyncio.CancelledError:
            pass
        finally:
            unsubscribe()

    def _update_agent_status(self, event: Event) -> None:
        """Update agent status from events."""
        data = event.data
        agent_id = data.get("agent_id", "")

        if not agent_id:
            return

        if event.event_type == EventType.SUBTASK_STARTED:
            self._agents[agent_id] = AgentStatus(
                agent_id=agent_id,
                status="running",
                current_task=data.get("goal", ""),
                started_at=event.timestamp,
            )
        elif event.event_type == EventType.SUBTASK_COMPLETED:
            if agent_id in self._agents:
                self._agents[agent_id].status = "completed"
        elif event.event_type == EventType.SUBTASK_FAILED:
            if agent_id in self._agents:
                self._agents[agent_id].status = "failed"

    def get_agent_statuses(self) -> list[dict[str, Any]]:
        """Get current status of all agents."""
        return [
            {
                "agent_id": a.agent_id,
                "status": a.status,
                "current_task": a.current_task,
                "tool_calls": a.tool_calls,
                "started_at": a.started_at,
            }
            for a in self._agents.values()
        ]

    def get_task_tree(self, task_id: str) -> dict[str, Any]:
        """Get the task tree for visualization."""
        events = self.event_bus.get_history(task_id=task_id)

        tree: dict[str, Any] = {
            "task_id": task_id,
            "status": "unknown",
            "subtasks": [],
        }

        subtask_map: dict[str, dict[str, Any]] = {}

        for event in events:
            data = event.data

            if event.event_type == EventType.TASK_CREATED:
                tree["task"] = data.get("task", "")
                tree["status"] = "created"

            elif event.event_type == EventType.TASK_STARTED:
                tree["status"] = "running"

            elif event.event_type == EventType.TASK_COMPLETED:
                tree["status"] = "completed"
                tree["duration"] = data.get("duration", 0)

            elif event.event_type == EventType.TASK_FAILED:
                tree["status"] = "failed"
                tree["error"] = data.get("error", "")

            elif event.event_type == EventType.SUBTASK_CREATED:
                sid = data.get("subtask_id", "")
                subtask_map[sid] = {
                    "subtask_id": sid,
                    "goal": data.get("goal", ""),
                    "agent_id": data.get("agent_id", ""),
                    "status": "created",
                }
                tree["subtasks"].append(subtask_map[sid])

            elif event.event_type == EventType.SUBTASK_STARTED:
                sid = data.get("subtask_id", "")
                if sid in subtask_map:
                    subtask_map[sid]["status"] = "running"

            elif event.event_type == EventType.SUBTASK_COMPLETED:
                sid = data.get("subtask_id", "")
                if sid in subtask_map:
                    subtask_map[sid]["status"] = "completed"
                    subtask_map[sid]["duration"] = data.get("duration", 0)
                    subtask_map[sid]["result_summary"] = data.get("result_summary", "")

            elif event.event_type == EventType.SUBTASK_FAILED:
                sid = data.get("subtask_id", "")
                if sid in subtask_map:
                    subtask_map[sid]["status"] = "failed"
                    subtask_map[sid]["error"] = data.get("error", "")

        return tree
