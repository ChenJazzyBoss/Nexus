"""API error classification for smart failover and recovery.

Adapted from hermes-agent/agent/error_classifier.py (simplified).
Provides a structured taxonomy of API errors and a priority-ordered
classification pipeline that determines the correct recovery action.
"""

from __future__ import annotations

import enum
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Error taxonomy ──────────────────────────────────────────────


class FailoverReason(enum.Enum):
    """Why an API call failed — determines recovery strategy."""

    auth = "auth"
    auth_permanent = "auth_permanent"
    billing = "billing"
    rate_limit = "rate_limit"
    overloaded = "overloaded"
    server_error = "server_error"
    timeout = "timeout"
    context_overflow = "context_overflow"
    payload_too_large = "payload_too_large"
    model_not_found = "model_not_found"
    content_policy_blocked = "content_policy_blocked"
    format_error = "format_error"
    unknown = "unknown"


@dataclass
class ClassifiedError:
    """Structured classification of an API error with recovery hints."""

    reason: FailoverReason
    status_code: int | None = None
    provider: str | None = None
    model: str | None = None
    message: str = ""
    error_context: dict[str, Any] = field(default_factory=dict)

    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False


# ── Pattern lists (simplified from hermes-agent) ────────────────

_BILLING_PATTERNS = [
    "insufficient credits", "insufficient_quota", "insufficient balance",
    "credits exhausted", "no usable credits", "payment required",
    "billing hard limit", "exceeded your current quota",
    "account is deactivated", "out of funds", "balance_depleted",
]

_RATE_LIMIT_PATTERNS = [
    "rate limit", "rate_limit", "too many requests", "throttled",
    "requests per minute", "tokens per minute", "try again in",
    "please retry after", "resource_exhausted",
]

_CONTEXT_OVERFLOW_PATTERNS = [
    "context length", "context size", "maximum context", "token limit",
    "too many tokens", "reduce the length", "exceeds the limit",
    "context window", "prompt is too long", "prompt exceeds max length",
    "max_tokens", "maximum number of tokens", "context length exceeded",
    "exceeds the max_model_len", "prompt length",
]

_MODEL_NOT_FOUND_PATTERNS = [
    "is not a valid model", "invalid model", "model not found",
    "model_not_found", "does not exist", "no such model",
    "unknown model", "unsupported model",
]

_AUTH_PATTERNS = [
    "invalid api key", "invalid_api_key", "authentication",
    "unauthorized", "forbidden", "invalid token", "token expired",
    "access denied",
]

_CONTENT_POLICY_PATTERNS = [
    "flagged for possible cybersecurity risk",
    "violates our usage policies", "violates openai's usage policies",
    "your request was flagged by", "prompt was flagged by our safety",
    "content_filter", "responsibleaipolicyviolation",
]

_TIMEOUT_PATTERNS = [
    "timed out", "request timed out", "deadline exceeded",
    "operation timed out", "upstream timed out",
]

_TRANSPORT_ERROR_TYPES = frozenset({
    "ReadTimeout", "ConnectTimeout", "PoolTimeout",
    "ConnectError", "ConnectionError", "ConnectionResetError",
    "TimeoutError", "APITimeoutError", "APIConnectionError",
})


# ── Classification pipeline ─────────────────────────────────────


def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200000,
) -> ClassifiedError:
    """Classify an API error into a structured recovery recommendation.

    Priority-ordered pipeline:
      1. Content-policy blocks (deterministic, don't retry)
      2. HTTP status code classification
      3. Message pattern matching
      4. Transport error heuristics
      5. Fallback: unknown (retryable)
    """
    status_code = _extract_status_code(error)
    error_type = type(error).__name__
    body = _extract_error_body(error)
    error_msg = _build_error_msg(error, body)
    provider_lower = (provider or "").strip().lower()

    def _result(reason: FailoverReason, **overrides: Any) -> ClassifiedError:
        defaults = {
            "reason": reason,
            "status_code": status_code,
            "provider": provider,
            "model": model,
            "message": _extract_message(error, body),
        }
        defaults.update(overrides)
        return ClassifiedError(**defaults)

    # ── 1. Content-policy blocks ────────────────────────────────
    if any(p in error_msg for p in _CONTENT_POLICY_PATTERNS):
        return _result(FailoverReason.content_policy_blocked, retryable=False)

    # ── 2. HTTP status code classification ──────────────────────
    if status_code is not None:
        classified = _classify_by_status(status_code, error_msg, body, _result)
        if classified is not None:
            return classified

    # ── 3. Message pattern matching ─────────────────────────────
    if any(p in error_msg for p in _BILLING_PATTERNS):
        return _result(
            FailoverReason.billing, retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )
    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return _result(
            FailoverReason.rate_limit, retryable=True,
            should_rotate_credential=True, should_fallback=True,
        )
    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return _result(
            FailoverReason.context_overflow, retryable=True,
            should_compress=True,
        )
    if any(p in error_msg for p in _AUTH_PATTERNS):
        return _result(
            FailoverReason.auth, retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )
    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return _result(
            FailoverReason.model_not_found, retryable=False,
            should_fallback=True,
        )
    if any(p in error_msg for p in _TIMEOUT_PATTERNS):
        return _result(FailoverReason.timeout, retryable=True)

    # ── 4. Transport error heuristics ───────────────────────────
    if error_type in _TRANSPORT_ERROR_TYPES or isinstance(error, (TimeoutError, ConnectionError)):
        return _result(FailoverReason.timeout, retryable=True)

    # ── 5. Fallback ─────────────────────────────────────────────
    return _result(FailoverReason.unknown, retryable=True)


def _classify_by_status(
    status_code: int, error_msg: str, body: dict, result_fn: Any,
) -> ClassifiedError | None:
    """Classify based on HTTP status code."""

    if status_code == 401:
        return result_fn(
            FailoverReason.auth, retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )
    if status_code == 403:
        if any(p in error_msg for p in _BILLING_PATTERNS):
            return result_fn(
                FailoverReason.billing, retryable=False,
                should_rotate_credential=True, should_fallback=True,
            )
        return result_fn(FailoverReason.auth, retryable=False, should_fallback=True)
    if status_code == 402:
        return result_fn(
            FailoverReason.billing, retryable=False,
            should_rotate_credential=True, should_fallback=True,
        )
    if status_code == 404:
        if any(p in error_msg for p in _BILLING_PATTERNS):
            return result_fn(
                FailoverReason.billing, retryable=False,
                should_rotate_credential=True, should_fallback=True,
            )
        if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
            return result_fn(
                FailoverReason.model_not_found, retryable=False,
                should_fallback=True,
            )
        return result_fn(FailoverReason.unknown, retryable=True)
    if status_code == 413:
        return result_fn(
            FailoverReason.payload_too_large, retryable=True,
            should_compress=True,
        )
    if status_code == 429:
        return result_fn(
            FailoverReason.rate_limit, retryable=True,
            should_rotate_credential=True, should_fallback=True,
        )
    if status_code == 400:
        if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
            return result_fn(
                FailoverReason.context_overflow, retryable=True,
                should_compress=True,
            )
        if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
            return result_fn(
                FailoverReason.model_not_found, retryable=False,
                should_fallback=True,
            )
        return result_fn(FailoverReason.format_error, retryable=False, should_fallback=True)
    if status_code in {500, 502}:
        return result_fn(FailoverReason.server_error, retryable=True)
    if status_code in {503, 529}:
        return result_fn(FailoverReason.overloaded, retryable=True)
    if 400 <= status_code < 500:
        return result_fn(FailoverReason.format_error, retryable=False, should_fallback=True)
    if 500 <= status_code < 600:
        return result_fn(FailoverReason.server_error, retryable=True)

    return None


# ── Helpers ─────────────────────────────────────────────────────


def _extract_status_code(error: Exception) -> int | None:
    """Walk the error chain to find an HTTP status code."""
    current = error
    for _ in range(5):
        code = getattr(current, "status_code", None)
        if isinstance(code, int):
            return code
        code = getattr(current, "status", None)
        if isinstance(code, int) and 100 <= code < 600:
            return code
        cause = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if cause is None or cause is current:
            break
        current = cause
    return None


def _extract_error_body(error: Exception) -> dict:
    """Extract the structured error body from an SDK exception."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    response = getattr(error, "response", None)
    if response is not None:
        try:
            json_body = response.json()
            if isinstance(json_body, dict):
                return json_body
        except Exception:
            pass
    return {}


def _build_error_msg(error: Exception, body: dict) -> str:
    """Build a comprehensive error message string for pattern matching."""
    parts = [str(error).lower()]
    if isinstance(body, dict):
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            body_msg = str(err_obj.get("message") or "").lower()
            if body_msg and body_msg not in parts[0]:
                parts.append(body_msg)
    return " ".join(parts)


def _extract_message(error: Exception, body: dict) -> str:
    """Extract the most informative error message."""
    if body:
        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            msg = error_obj.get("message", "")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:500]
        msg = body.get("message", "")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:500]
    return str(error)[:500]
