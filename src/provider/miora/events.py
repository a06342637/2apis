"""Miora ACP 通知转中立事件；云端工具执行事件不冒充客户端工具请求。"""

from __future__ import annotations

import math
from typing import Any

from ..base import ErrKind, Event, EventKind, Usage


def classify_status(status: int, body: bytes = b"") -> ErrKind:
    """长登录票失效、短运行时票、额度与模型权限由调用路径进一步分流。"""
    text = body.decode("utf-8", errors="replace").lower()
    if status == 401:
        return ErrKind.DEAD
    if status == 429:
        return ErrKind.SOFT
    if status == 402 or any(
        word in text for word in ("insufficient credit", "credit exhausted", "积分不足", "余额不足")
    ):
        return ErrKind.CREDIT
    if any(word in text for word in ("model not found", "model not available", "模型不存在")):
        return ErrKind.BLOCKED
    if status == 403:
        return ErrKind.SOFT
    if status in (400, 404, 422):
        return ErrKind.INVALID
    if 500 <= status < 600:
        return ErrKind.SOFT
    return ErrKind.OTHER


def _number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return int(value)


def _usage(update: dict) -> Usage | None:
    """保留上游明确给出的 token 计数；上下文 used/size 不冒充输出量。"""
    raw = update.get("usage", update)
    if not isinstance(raw, dict):
        return None
    prompt = _number(raw.get("inputTokens", raw.get("input_tokens")))
    completion = _number(raw.get("outputTokens", raw.get("output_tokens")))
    if prompt is None and completion is None:
        return None
    return Usage(
        input_tokens=prompt,
        output_tokens=completion,
        cached_tokens=_number(raw.get("cachedReadTokens", raw.get("cache_read_input_tokens"))),
        reasoning_tokens=_number(raw.get("reasoningTokens", raw.get("reasoning_tokens"))),
    )


def parse_update(frame: Any) -> list[Event]:
    if not isinstance(frame, dict):
        return []
    error = frame.get("error")
    if isinstance(error, dict):
        return [
            Event(
                kind=EventKind.ERROR,
                error_message="Miora ACP 请求失败",
                error_kind=ErrKind.UPSTREAM_PROTOCOL,
            )
        ]
    result = frame.get("result")
    if isinstance(result, dict) and "stopReason" in result:
        reason = result["stopReason"]
        # 客户端主动取消由异步生成器取消处理；收到上游 cancelled 终态时
        # 本次回答并未正常完成，不能将部分输出伪装为成功的 stop。
        if reason in ("end_turn", "stop"):
            return [Event(kind=EventKind.FINISH, finish_reason="stop")]
        if reason in ("max_tokens", "length"):
            return [Event(kind=EventKind.FINISH, finish_reason="length")]
        return [
            Event(
                kind=EventKind.ERROR,
                error_message="Miora 对话异常结束",
                error_kind=ErrKind.UPSTREAM_PROTOCOL,
            )
        ]
    params = frame.get("params")
    if frame.get("method") != "session/update" or not isinstance(params, dict):
        return []
    meta = params.get("_meta")
    namespace = meta.get("codebuddy.ai") if isinstance(meta, dict) else None
    if isinstance(namespace, dict) and (
        namespace.get("mode") == "history" or namespace.get("parentToolUseId")
    ):
        return []
    update = params.get("update")
    if not isinstance(update, dict):
        return []
    kind = update.get("sessionUpdate")
    if kind in ("agent_message_chunk", "agent_thought_chunk"):
        content = update.get("content")
        text = content.get("text") if isinstance(content, dict) else None
        if isinstance(text, str) and text:
            event_kind = EventKind.CONTENT if kind == "agent_message_chunk" else EventKind.REASONING
            return [Event(kind=event_kind, content=text)]
    elif kind == "usage_update":
        usage = _usage(update)
        if usage is not None:
            return [Event(kind=EventKind.USAGE, usage=usage)]
    return []
