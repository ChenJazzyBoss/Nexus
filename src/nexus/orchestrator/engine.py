"""Orchestrator engine — task decomposition, agent dispatch, result aggregation.

Implements the Orchestrator spec defined in .superspec/specs/orchestrator/spec.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from nexus.core.agent import AgentConfig, AgentRunner, TurnResult
from nexus.core.config import NexusConfig
from nexus.core.iteration_budget import IterationBudget
from nexus.core.tool_engine import ToolRegistry, registry as global_registry
from nexus.orchestrator.events import Event, EventBus, EventType

logger = logging.getLogger(__name__)

# Default timeout for subtasks (5 minutes)
DEFAULT_SUBTASK_TIMEOUT = 300


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SubTask:
    """A decomposed subtask."""

    subtask_id: str = ""
    goal: str = ""
    tools: set[str] = field(default_factory=set)
    role: str = ""

    def __post_init__(self):
        if not self.subtask_id:
            self.subtask_id = f"sub_{uuid.uuid4().hex[:8]}"


@dataclass
class SubTaskResult:
    """Result of a subtask execution."""

    subtask_id: str
    agent_id: str
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    error: str | None = None
    duration_seconds: float = 0.0
    tool_calls_made: int = 0


@dataclass
class TaskResult:
    """Result of a complete task."""

    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    result: str = ""
    subtasks: list[SubTaskResult] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    @property
    def duration_seconds(self) -> float:
        if self.completed_at:
            return self.completed_at - self.created_at
        return time.time() - self.created_at


@dataclass
class AgentPool:
    """Manages agent lifecycle and concurrency."""

    max_concurrent: int = 5
    _active: dict[str, AgentRunner] = field(default_factory=dict)
    _executor: ThreadPoolExecutor | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent)

    async def acquire(self) -> bool:
        """Check if we can spawn a new agent."""
        async with self._lock:
            return len(self._active) < self.max_concurrent

    def register(self, agent_id: str, agent: AgentRunner) -> None:
        """Register an active agent."""
        self._active[agent_id] = agent

    def unregister(self, agent_id: str) -> None:
        """Unregister an agent."""
        self._active.pop(agent_id, None)

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def executor(self) -> ThreadPoolExecutor:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent)
        return self._executor


class Orchestrator:
    """Top-level task router and agent lifecycle manager.

    Receives tasks, decomposes them, spawns workers, and aggregates results.
    """

    def __init__(
        self,
        nexus_config: NexusConfig,
        tool_registry: ToolRegistry | None = None,
        event_bus: EventBus | None = None,
    ):
        self.nexus_config = nexus_config
        self.tool_registry = tool_registry or global_registry
        self.event_bus = event_bus or EventBus()

        self.agent_pool = AgentPool(
            max_concurrent=nexus_config.max_concurrent_agents
        )

        self._tasks: dict[str, TaskResult] = {}
        self._task_futures: dict[str, Future] = {}

    async def execute_task(
        self,
        task: str,
        context: dict[str, Any] | None = None,
        tool_names: set[str] | None = None,
    ) -> TaskResult:
        """Execute a task — decompose, dispatch, aggregate.

        Args:
            task: The task description
            context: Optional context for the task
            tool_names: Tools available for the task

        Returns:
            TaskResult with the final result
        """
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task_result = TaskResult(task_id=task_id, status=TaskStatus.PENDING)
        self._tasks[task_id] = task_result

        # Emit task_created
        self.event_bus.emit(Event(
            event_type=EventType.TASK_CREATED,
            task_id=task_id,
            data={"task": task},
        ))

        try:
            # Update status
            task_result.status = TaskStatus.RUNNING
            self.event_bus.emit(Event(
                event_type=EventType.TASK_STARTED,
                task_id=task_id,
                data={"task": task},
            ))

            # Try to decompose the task
            subtasks = await self._decompose_task(task, tool_names)

            if len(subtasks) <= 1:
                # Simple task — execute directly
                result = await self._execute_single_task(
                    task_id, task, tool_names or set()
                )
                task_result.result = result.result
                task_result.status = result.status
                task_result.subtasks = [result]
            else:
                # Complex task — execute subtasks in parallel
                subtask_results = await self._execute_subtasks(
                    task_id, subtasks
                )
                task_result.subtasks = subtask_results

                # Aggregate results
                task_result.result = await self._aggregate_results(
                    task, subtask_results
                )

                # Determine status
                failed = [r for r in subtask_results if r.status == TaskStatus.FAILED]
                if len(failed) == len(subtask_results):
                    task_result.status = TaskStatus.FAILED
                else:
                    task_result.status = TaskStatus.COMPLETED

            task_result.completed_at = time.time()

            # Emit completion event
            event_type = (
                EventType.TASK_COMPLETED
                if task_result.status == TaskStatus.COMPLETED
                else EventType.TASK_FAILED
            )
            self.event_bus.emit(Event(
                event_type=event_type,
                task_id=task_id,
                data={
                    "status": task_result.status.value,
                    "duration": task_result.duration_seconds,
                    "result": task_result.result[:500] if task_result.result else None,
                },
            ))

        except Exception as e:
            logger.exception("Task %s failed: %s", task_id, e)
            task_result.status = TaskStatus.FAILED
            task_result.result = f"Task failed: {str(e)}"
            task_result.completed_at = time.time()

            self.event_bus.emit(Event(
                event_type=EventType.TASK_FAILED,
                task_id=task_id,
                data={"error": str(e)},
            ))

        return task_result

    async def _decompose_task(
        self, task: str, tool_names: set[str] | None
    ) -> list[SubTask]:
        """Decompose a task into subtasks using LLM."""
        try:
            # Create a temporary agent for decomposition
            decomposer = AgentRunner(
                config=AgentConfig(
                    agent_id="decomposer",
                    model=self.nexus_config.default_model,
                    provider=self.nexus_config.default_provider,
                    system_prompt=(
                        "You are a task decomposition engine. "
                        "Given a user task, break it into at most 3 independent subtasks. "
                        "Each subtask should be completable by a single agent.\n\n"
                        "Respond with a JSON array of subtasks:\n"
                        '[{"goal": "...", "tools": ["tool1", "tool2"], "role": "..."}]\n\n'
                        "Rules:\n"
                        "- Maximum 3 subtasks. Fewer is better.\n"
                        "- If the task is simple, return a SINGLE subtask with the original goal.\n"
                        "- Each goal must be concrete and actionable.\n\n"
                        "Available tools: " + ", ".join(tool_names or [])
                    ),
                    max_iterations=3,
                    tool_names=set(),  # No tools for decomposition
                ),
                nexus_config=self.nexus_config,
            )

            result = await decomposer.run_turn(
                f"Decompose this task into subtasks:\n\n{task}"
            )

            if result.error:
                logger.warning("Decomposition failed: %s", result.error)
                return [SubTask(goal=task, tools=tool_names or set())]

            # Parse the response
            try:
                # Try to extract JSON from the response
                content = result.content.strip()
                # Handle markdown code blocks
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()

                subtask_dicts = json.loads(content)
                if not isinstance(subtask_dicts, list):
                    subtask_dicts = [subtask_dicts]

                subtasks = []
                for st in subtask_dicts:
                    subtasks.append(SubTask(
                        goal=st.get("goal", task),
                        tools=set(st.get("tools", [])),
                        role=st.get("role", ""),
                    ))

                return subtasks if subtasks else [SubTask(goal=task, tools=tool_names or set())]

            except (json.JSONDecodeError, KeyError, IndexError) as e:
                logger.warning("Failed to parse decomposition result: %s", e)
                return [SubTask(goal=task, tools=tool_names or set())]

        except Exception as e:
            logger.warning("Decomposition error: %s", e)
            return [SubTask(goal=task, tools=tool_names or set())]

    async def _execute_single_task(
        self,
        task_id: str,
        goal: str,
        tool_names: set[str],
        subtask_id: str | None = None,
    ) -> SubTaskResult:
        """Execute a single task with an AgentRunner."""
        agent_id = f"agent_{uuid.uuid4().hex[:8]}"
        sid = subtask_id or f"sub_{uuid.uuid4().hex[:8]}"

        # Emit subtask events
        self.event_bus.emit(Event(
            event_type=EventType.SUBTASK_CREATED,
            task_id=task_id,
            data={"subtask_id": sid, "goal": goal, "agent_id": agent_id},
        ))

        start_time = time.time()

        try:
            # Check pool availability
            if not await self.agent_pool.acquire():
                return SubTaskResult(
                    subtask_id=sid,
                    agent_id=agent_id,
                    status=TaskStatus.FAILED,
                    error="Agent pool exhausted",
                    duration_seconds=time.time() - start_time,
                )

            # Create agent
            agent = AgentRunner(
                config=AgentConfig(
                    agent_id=agent_id,
                    model=self.nexus_config.default_model,
                    provider=self.nexus_config.default_provider,
                    system_prompt=(
                        "You are a focused worker agent. "
                        "Complete the given task thoroughly and return the result."
                    ),
                    max_iterations=max(self.nexus_config.max_iterations, 50),
                    tool_names=tool_names,
                ),
                nexus_config=self.nexus_config,
                tool_registry=self.tool_registry,
            )

            self.agent_pool.register(agent_id, agent)

            self.event_bus.emit(Event(
                event_type=EventType.SUBTASK_STARTED,
                task_id=task_id,
                data={"subtask_id": sid, "agent_id": agent_id},
            ))

            # Execute
            result = await asyncio.wait_for(
                agent.run_turn(goal),
                timeout=DEFAULT_SUBTASK_TIMEOUT,
            )

            duration = time.time() - start_time

            if result.error:
                self.event_bus.emit(Event(
                    event_type=EventType.SUBTASK_FAILED,
                    task_id=task_id,
                    data={
                        "subtask_id": sid,
                        "agent_id": agent_id,
                        "error": result.error,
                        "duration": duration,
                    },
                ))
                return SubTaskResult(
                    subtask_id=sid,
                    agent_id=agent_id,
                    status=TaskStatus.FAILED,
                    error=result.error,
                    duration_seconds=duration,
                    tool_calls_made=result.tool_calls_made,
                )

            self.event_bus.emit(Event(
                event_type=EventType.SUBTASK_COMPLETED,
                task_id=task_id,
                data={
                    "subtask_id": sid,
                    "agent_id": agent_id,
                    "duration": duration,
                    "result_summary": result.content[:200],
                },
            ))

            return SubTaskResult(
                subtask_id=sid,
                agent_id=agent_id,
                status=TaskStatus.COMPLETED,
                result=result.content,
                duration_seconds=duration,
                tool_calls_made=result.tool_calls_made,
            )

        except asyncio.TimeoutError:
            duration = time.time() - start_time
            self.event_bus.emit(Event(
                event_type=EventType.SUBTASK_FAILED,
                task_id=task_id,
                data={
                    "subtask_id": sid,
                    "agent_id": agent_id,
                    "error": "Timeout",
                    "duration": duration,
                },
            ))
            return SubTaskResult(
                subtask_id=sid,
                agent_id=agent_id,
                status=TaskStatus.FAILED,
                error=f"Subtask timed out after {DEFAULT_SUBTASK_TIMEOUT}s",
                duration_seconds=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            logger.exception("Subtask %s failed: %s", sid, e)
            return SubTaskResult(
                subtask_id=sid,
                agent_id=agent_id,
                status=TaskStatus.FAILED,
                error=str(e),
                duration_seconds=duration,
            )

        finally:
            self.agent_pool.unregister(agent_id)

    async def _execute_subtasks(
        self,
        task_id: str,
        subtasks: list[SubTask],
    ) -> list[SubTaskResult]:
        """Execute multiple subtasks in parallel."""
        tasks = []
        for subtask in subtasks:
            tasks.append(
                self._execute_single_task(
                    task_id=task_id,
                    goal=subtask.goal,
                    tool_names=subtask.tools,
                    subtask_id=subtask.subtask_id,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        subtask_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                subtask_results.append(SubTaskResult(
                    subtask_id=subtasks[i].subtask_id,
                    agent_id="unknown",
                    status=TaskStatus.FAILED,
                    error=str(result),
                ))
            else:
                subtask_results.append(result)

        return subtask_results

    async def _aggregate_results(
        self,
        original_task: str,
        subtask_results: list[SubTaskResult],
    ) -> str:
        """Aggregate subtask results into a final response."""
        successful = [r for r in subtask_results if r.status == TaskStatus.COMPLETED]
        failed = [r for r in subtask_results if r.status == TaskStatus.FAILED]

        if not successful:
            errors = "\n".join(f"- {r.subtask_id}: {r.error}" for r in failed)
            return f"All subtasks failed:\n{errors}"

        # Build aggregation prompt
        parts = [f"Original task: {original_task}\n"]
        parts.append("Subtask results:\n")
        for i, r in enumerate(successful, 1):
            parts.append(f"--- Result {i} ({r.subtask_id}) ---\n{r.result}\n")

        if failed:
            parts.append("\nFailed subtasks:\n")
            for r in failed:
                parts.append(f"- {r.subtask_id}: {r.error}\n")

        parts.append(
            "\nSynthesize the above results into a coherent, comprehensive response "
            "for the original task. If any subtasks failed, note what was missed."
        )

        # Use an aggregator agent
        aggregator = AgentRunner(
            config=AgentConfig(
                agent_id="aggregator",
                model=self.nexus_config.default_model,
                provider=self.nexus_config.default_provider,
                system_prompt=(
                    "You are a result aggregation agent. "
                    "Your job is to synthesize multiple subtask results "
                    "into a single coherent response."
                ),
                max_iterations=5,
                tool_names=set(),
            ),
            nexus_config=self.nexus_config,
        )

        result = await aggregator.run_turn("\n".join(parts))

        if result.error:
            # Fallback: simple concatenation
            parts = []
            for r in successful:
                parts.append(r.result)
            return "\n\n---\n\n".join(parts)

        return result.content

    def get_task(self, task_id: str) -> TaskResult | None:
        """Get task result by ID."""
        return self._tasks.get(task_id)

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        limit: int = 50,
    ) -> list[TaskResult]:
        """List tasks with optional status filter."""
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        tasks.sort(key=lambda t: t.created_at, reverse=True)
        return tasks[:limit]
