from __future__ import annotations

import pytest

from src.provider.base import ErrKind, EventKind
from src.provider.miora.events import classify_status, parse_update


def update(kind: str, **values) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "s",
            "update": {"sessionUpdate": kind, **values},
        },
    }


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (401, b"", ErrKind.DEAD),
        (402, b"", ErrKind.CREDIT),
        (403, b"insufficient credits", ErrKind.CREDIT),
        (500, "积分不足".encode(), ErrKind.CREDIT),
        (400, b"model not found", ErrKind.BLOCKED),
        (403, b"model not available", ErrKind.BLOCKED),
        (403, b"forbidden", ErrKind.SOFT),
        (429, b"", ErrKind.CONCURRENCY),
        (400, b"", ErrKind.INVALID),
        (404, b"", ErrKind.INVALID),
        (422, b"", ErrKind.INVALID),
        (500, b"\xff", ErrKind.OTHER),
        (200, b"", ErrKind.OTHER),
    ],
)
def test_classify(status, body, expected):
    assert classify_status(status, body) == expected


@pytest.mark.parametrize(
    "frame",
    [
        None,
        [],
        "",
        {},
        {"error": "bad"},
        {"result": {}},
        {"method": "other", "params": {}},
        {"method": "session/update", "params": None},
        {"method": "session/update", "params": {"update": None}},
    ],
)
def test_empty_frames(frame):
    assert parse_update(frame) == []


def test_errors_are_sanitized():
    event = parse_update({"error": {"message": "Bearer private-token", "code": -32000}})[0]
    assert event.kind == EventKind.ERROR
    assert event.error_kind == ErrKind.UPSTREAM_PROTOCOL
    assert "private-token" not in event.error_message


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("end_turn", "stop"),
        ("cancelled", "stop"),
        ("canceled", "stop"),
        ("stop", "stop"),
        ("max_tokens", "length"),
        ("length", "length"),
    ],
)
def test_finish(reason, expected):
    event = parse_update({"id": 3, "result": {"stopReason": reason}})[0]
    assert event.kind == EventKind.FINISH
    assert event.finish_reason == expected


def test_unknown_finish_fails_closed():
    assert parse_update({"result": {"stopReason": "unknown"}})[0].kind == EventKind.ERROR


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("agent_message_chunk", EventKind.CONTENT),
        ("agent_thought_chunk", EventKind.REASONING),
    ],
)
def test_text(kind, expected):
    frame = update(kind, content={"type": "text", "text": "hello"})
    event = parse_update(frame)[0]
    assert event.kind == expected and event.content == "hello"
    frame["params"]["_meta"] = {"codebuddy.ai": {"mode": "history"}}
    assert parse_update(frame) == []
    frame["params"]["_meta"] = {"codebuddy.ai": {"mode": "stream", "parentToolUseId": "tool"}}
    assert parse_update(frame) == []
    for meta in ({}, {"codebuddy.ai": None}, {"codebuddy.ai": {}}, "bad"):
        frame["params"]["_meta"] = meta
        assert parse_update(frame)[0].content == "hello"


@pytest.mark.parametrize("content", [None, {}, {"text": None}, {"text": ""}, {"text": 1}])
def test_bad_content(content):
    assert parse_update(update("agent_message_chunk", content=content)) == []


def test_server_tools_are_not_client_requests():
    assert parse_update(update("tool_call", toolCallId="t", rawInput={"x": 1})) == []
    assert parse_update(update("tool_call_update", status="completed", rawOutput="result")) == []
    assert parse_update(update("session_info_update", title="server title")) == []


def test_usage_explicit_counts_only():
    event = parse_update(
        update(
            "usage_update",
            usage={
                "input_tokens": 10.8,
                "output_tokens": 5,
                "cache_read_input_tokens": 0,
                "reasoning_tokens": 2,
            },
        )
    )[0]
    assert event.kind == EventKind.USAGE
    assert event.usage.input_tokens == 10
    assert event.usage.output_tokens == 5
    assert event.usage.cached_tokens == 0
    assert event.usage.reasoning_tokens == 2
    event = parse_update(update("usage_update", usage={"output_tokens": 2}))[0]
    assert event.usage.input_tokens is None
    event = parse_update(update("usage_update", usage={"input_tokens": 2}))[0]
    assert event.usage.output_tokens is None
    for raw in (
        None,
        {},
        {"used": 100, "size": 200},
        {"input_tokens": True},
        {"input_tokens": "2"},
        {"input_tokens": float("nan")},
        {"input_tokens": float("inf")},
        {"input_tokens": -1},
    ):
        assert parse_update(update("usage_update", usage=raw)) == []


def test_live_camel_case_usage():
    event = parse_update(
        update(
            "usage_update",
            inputTokens=77067,
            outputTokens=281,
            cachedReadTokens=9216,
            reasoningTokens=3,
            used=38654,
            size=300000,
        )
    )[0]
    assert event.usage.input_tokens == 77067
    assert event.usage.output_tokens == 281
    assert event.usage.cached_tokens == 9216
    assert event.usage.reasoning_tokens == 3
    assert parse_update(update("usage_update", used=10, size=100)) == []
