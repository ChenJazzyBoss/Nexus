"""Event bus for task lifecycle events.

Adapted from openclaw/src/sessions/session-lifecycle-events.py pattern.
Provides simple pub/sub for agent and task lifecycle events.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventType(enum.Enum):
    """Task lifecycle event types."""

    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"

    SUBTASK_CREATED = "subtask_created"
    SUBTASK_STARTED = "subtask_started"
    SUBTASK_COMPLETED = "subtask_completed"
    SUBTASK_FAILED = "subtask_failed"


@dataclass
class Event:
    """An event emitted by the orchestrator."""

    event_type: EventType
    task_id: str
    timestamp: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "event": self.event_type.value,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "data": self.data,
        }


# Type alias for event listeners
EventListener = Callable[[Event], None]


class EventBus:
    """Simple pub/sub event bus.

    Usage:
        bus = EventBus()
        bus.on(EventType.TASK_COMPLETED, my_handler)
        bus.emit(Event(event_type=EventType.TASK_COMPLETED, task_id="123"))
    """

    def __init__(self):
        self._listeners: dict[EventType, set[EventListener]] = {}
        self._lock = threading.Lock()
        self._history: list[Event] = []
        self._max_history = 1000

    def on(self, event_type: EventType, listener: EventListener) -> Callable[[], None]:
        """Subscribe to an event type.

        Returns an unsubscribe function.
        """
        with self._lock:
            if event_type not in self._listeners:
                self._listeners[event_type] = set()
            self._listeners[event_type].add(listener)

        def unsubscribe():
            with self._lock:
                listeners = self._listeners.get(event_type)
                if listeners:
                    listeners.discard(listener)

        return unsubscribe

    def on_all(self, listener: EventListener) -> Callable[[], None]:
        """Subscribe to all event types.

        Returns an unsubscribe function.
        """
        unsubscribers = []
        for event_type in EventType:
            unsubscribers.append(self.on(event_type, listener))

        def unsubscribe_all():
            for unsub in unsubscribers:
                unsub()

        return unsubscribe_all

    def emit(self, event: Event) -> None:
        """Emit an event to all subscribers."""
        # Store in history
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]

        # Notify listeners
        with self._lock:
            listeners = list(self._listeners.get(event.event_type, set()))

        for listener in listeners:
            try:
                listener(event)
            except Exception:
                logger.exception(
                    "Event listener error for %s", event.event_type.value
                )

    def get_history(
        self,
        event_type: EventType | None = None,
        task_id: str | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Get event history with optional filtering."""
        with self._lock:
            events = list(self._history)

        if event_type:
            events = [e for e in events if e.event_type == event_type]
        if task_id:
            events = [e for e in events if e.task_id == task_id]

        return events[-limit:]

    def clear_history(self) -> None:
        """Clear event history."""
        with self._lock:
            self._history.clear()
