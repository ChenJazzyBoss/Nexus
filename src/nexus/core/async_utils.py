"""Async/sync bridging helpers.

Adapted from hermes-agent/agent/async_utils.py (verbatim copy).
Provides safe_schedule_threadsafe() for leak-safe coroutine scheduling
from worker threads.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Future
from typing import Any, Coroutine

_DEFAULT_LOGGER = logging.getLogger(__name__)


def safe_schedule_threadsafe(
    coro: Coroutine[Any, Any, Any],
    loop: asyncio.AbstractEventLoop | None,
    *,
    logger: logging.Logger | None = None,
    log_message: str = "Failed to schedule coroutine on loop",
    log_level: int = logging.DEBUG,
) -> Future | None:
    """Schedule coro on loop from a sync context, leak-safe.

    Returns the Future on success, or None if the loop is missing or
    asyncio.run_coroutine_threadsafe raised. In all failure paths the
    coroutine is closed so it does not trigger "coroutine was never
    awaited" warnings.
    """
    log = logger if logger is not None else _DEFAULT_LOGGER

    if loop is None:
        if asyncio.iscoroutine(coro):
            coro.close()
        log.log(log_level, "%s: loop is None", log_message)
        return None

    try:
        return asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:
        if asyncio.iscoroutine(coro):
            coro.close()
        log.log(log_level, "%s: %s", log_message, exc)
        return None
