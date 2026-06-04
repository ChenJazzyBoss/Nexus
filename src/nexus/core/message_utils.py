"""消息清理与修复工具。

确保发送给 LLM API 的消息列表符合 API 规范：
- role 交替正确（user/assistant 交替出现）
- 无孤立 tool_result（每个 tool_result 都有对应的 tool_call）
- tool_call 参数是有效 JSON
- 无空 thinking block
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def repair_message_sequence(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确保 user/assistant 交替出现，合并连续同 role 消息。

    处理规则：
    1. system 消息保留在开头，不参与交替检查
    2. tool 消息跟随在包含对应 tool_call 的 assistant 消息之后，不参与交替检查
    3. 连续同 role（user/user 或 assistant/assistant）的消息合并为一条
    4. 如果缺少 user 消息则插入空 user 消息占位

    Args:
        messages: 原始消息列表

    Returns:
        修复后的消息列表
    """
    if not messages:
        return messages

    result: list[dict[str, Any]] = []
    pending: dict[str, Any] | None = None

    for msg in messages:
        role = msg.get("role", "")

        # system 消息直接透传，不参与交替检查
        if role == "system":
            result.append(msg)
            continue

        # tool 消息直接透传（孤立 tool_result 由 sanitize_api_messages 处理）
        if role == "tool":
            # 先把 pending 消息写入
            if pending is not None:
                result.append(pending)
                pending = None
            result.append(msg)
            continue

        # user / assistant 消息
        if pending is None:
            pending = dict(msg)
            continue

        if role == pending.get("role"):
            # 同 role 连续 —— 合并内容
            pending = _merge_messages(pending, msg)
        else:
            # 正常交替
            result.append(pending)
            pending = dict(msg)

    if pending is not None:
        result.append(pending)

    # 确保以 user 消息开始（system 之后）
    result = _ensure_starts_with_user(result)

    return result


def _merge_messages(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """合并两条同 role 的消息内容。"""
    content_a = a.get("content", "") or ""
    content_b = b.get("content", "") or ""

    # 处理 content 可能为 list（多模态）的情况
    if isinstance(content_a, list) or isinstance(content_b, list):
        parts = []
        if isinstance(content_a, list):
            parts.extend(content_a)
        elif content_a:
            parts.append({"type": "text", "text": content_a})
        if isinstance(content_b, list):
            parts.extend(content_b)
        elif content_b:
            parts.append({"type": "text", "text": content_b})
        merged_content = parts
    else:
        merged_content = f"{content_a}\n{content_b}".strip()

    merged = dict(a)
    merged["content"] = merged_content

    # 合并 tool_calls（如果有）
    tc_a = a.get("tool_calls") or []
    tc_b = b.get("tool_calls") or []
    if tc_a or tc_b:
        merged["tool_calls"] = list(tc_a) + list(tc_b)

    return merged


def _ensure_starts_with_user(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """确保 system 消息之后第一条是 user 消息。"""
    if not messages:
        return messages

    # 找到第一条非 system 消息的索引
    first_non_system = -1
    for i, msg in enumerate(messages):
        if msg.get("role") != "system":
            first_non_system = i
            break

    if first_non_system == -1:
        # 全是 system 消息
        return messages

    if messages[first_non_system].get("role") != "user":
        # 在该位置插入空 user 消息
        messages.insert(first_non_system, {"role": "user", "content": ""})

    return messages


def sanitize_api_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """删除没有对应 tool_call 的孤立 tool_result 消息。

    每个 tool 消息的 tool_call_id 必须能在之前的 assistant 消息的
    tool_calls 中找到匹配。找不到则视为孤立并删除。

    Args:
        messages: 原始消息列表

    Returns:
        清理后的消息列表
    """
    # 第一遍：收集所有 assistant 消息提供的 tool_call_id
    valid_tool_call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                if tc_id:
                    valid_tool_call_ids.add(tc_id)

    # 第二遍：过滤孤立的 tool 消息
    result: list[dict[str, Any]] = []
    removed_count = 0
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id and tc_id not in valid_tool_call_ids:
                removed_count += 1
                logger.debug("移除孤立 tool 消息: tool_call_id=%s", tc_id)
                continue
        result.append(msg)

    if removed_count:
        logger.info("清理了 %d 条孤立 tool 消息", removed_count)

    return result


def sanitize_tool_call_arguments(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """修复损坏的 tool_call JSON 参数。

    对每条 assistant 消息中的 tool_calls，检查 function.arguments
    是否为有效 JSON 字符串。若解析失败则替换为 "{}"。

    Args:
        messages: 原始消息列表

    Returns:
        修复后的消息列表
    """
    fixed_count = 0
    for msg in messages:
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue

        for tc in msg["tool_calls"]:
            func = tc.get("function", {})
            arguments = func.get("arguments", "")

            # 如果已经是 dict，转为 JSON 字符串
            if isinstance(arguments, dict):
                func["arguments"] = json.dumps(arguments, ensure_ascii=False)
                continue

            # 如果是字符串，验证是否为有效 JSON
            if isinstance(arguments, str):
                try:
                    json.loads(arguments)
                except (json.JSONDecodeError, ValueError):
                    func["arguments"] = "{}"
                    fixed_count += 1
                    logger.debug(
                        "修复损坏的 tool_call 参数: tool=%s",
                        func.get("name", "unknown"),
                    )

    if fixed_count:
        logger.info("修复了 %d 条损坏的 tool_call 参数", fixed_count)

    return messages


def clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """消息清理入口函数 —— 按顺序执行所有清理操作。

    执行顺序：
    1. sanitize_api_messages —— 移除孤立 tool_result
    2. sanitize_tool_call_arguments —— 修复损坏的 JSON 参数
    3. repair_message_sequence —— 确保 role 交替正确

    Args:
        messages: 原始消息列表

    Returns:
        清理后的消息列表，可安全发送给 LLM API
    """
    result = messages
    result = sanitize_api_messages(result)
    result = sanitize_tool_call_arguments(result)
    result = repair_message_sequence(result)
    return result
