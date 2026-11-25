"""日志脱敏复杂度、跨线程有界投递与关闭/重绑定竞态回归。"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import subprocess
import sys
import threading

import pytest

from src.webapp.log_buffer import (
    MAX_LINE_CHARS,
    MAX_PENDING_LOGS,
    LogBuffer,
    LogBufferHandler,
    redact_or_drop,
    sync_root_handler,
)


class QueuedLoop:
    def __init__(self):
        self.callbacks = []

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, callback, *args):
        self.callbacks.append((callback, args))

    def drain(self):
        callback, args = self.callbacks.pop(0)
        callback(*args)


def append(buffer, message="message"):
    return buffer.append(level="INFO", logger_name="audit", message=message)


def record(message):
    return logging.LogRecord("audit", logging.INFO, __file__, 1, message, None, None)


def test_identifier_scan_retains_old_unicode_quote_and_separator_semantics():
    # 旧规则作为脱敏语义参照：新算法不得让任何旧规则挡住的凭证形态漏出。
    previous = re.compile(
        r"[A-Za-z0-9_.\-]*(?:token|secret|passwd|password|cookie|authorization|api[_-]?key)"
        r"[A-Za-z0-9_.\-]*[\"']?\s*[:=]", re.IGNORECASE,
    )
    from src.webapp.log_buffer import _ASSIGNED_IDENTIFIER, _SENSITIVE_PART

    parts = ["access_token", "tokensecrettoken", "API-KEY", "İıſK", "password", "cookie",
             "Authorization", "task", "AAA", ".", "-", "_", "'", '"', ":", "=", " ",
             "\n", "\t", "中", "。", "tokenx", "tokens", "/", "\\"]
    randomizer = random.Random(0)
    for _ in range(1000):
        line = "".join(randomizer.choice(parts) for _ in range(randomizer.randint(0, 20)))
        actual = any(_SENSITIVE_PART.search(line, match.start(), match.end()) is not None
                     for match in _ASSIGNED_IDENTIFIER.finditer(line))
        assert actual == bool(previous.search(line)), repr(line)


@pytest.mark.parametrize("suffix", [
    'access_token="sentinel"', "client_secret=sentinel", "x-api-key: sentinel",
    "İıſKtoken_suffix = sentinel", "prepassword_post'\t:sentinel",
])
def test_sensitive_suffix_beyond_truncation_is_dropped_from_every_output(suffix):
    persisted = []

    class Store:
        def append(self, **values):
            persisted.append(values)

    loop = QueuedLoop()
    buffer = LogBuffer(capacity=lambda: 5, persist=Store())
    buffer.hub.subscribe()
    buffer.bind_loop(loop)
    LogBufferHandler(buffer).emit(record("A" * (MAX_LINE_CHARS * 2) + " " + suffix))
    assert buffer.snapshot() == persisted == loop.callbacks == []
    assert not buffer._pending


def test_long_adversarial_logs_finish_without_quadratic_identifier_backtracking():
    # 独立进程给反退化验证加时限；若重引入旧正则，长输入不会挂住整个测试集。
    result = subprocess.run(
        [sys.executable, "-c", "from src.webapp.log_buffer import redact_or_drop; "
         "lines=['A'*64000, 'A'*64000+'=', 'token_'*8000, 'token_'*8000+'=value']; "
         "assert all(redact_or_drop(line)==line for line in lines[:3]); "
         "assert redact_or_drop(lines[3]) is None"],
        capture_output=True, text=True, timeout=5, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_normal_assignment_before_sensitive_assignment_does_not_stop_scan():
    assert redact_or_drop("request_id=abc user_name=alice access_token=sentinel") is None
    assert redact_or_drop("request_id=abc user_name=alice") is not None


def test_coalesced_dispatch_does_not_drop_persistence_or_change_ring_capacity():
    persisted = []

    class Store:
        def append(self, **values):
            persisted.append(values)

    buffer = LogBuffer(capacity=lambda: 2, persist=Store())
    subscription = buffer.hub.subscribe()
    loop = QueuedLoop()
    buffer.bind_loop(loop)
    for index in range(MAX_PENDING_LOGS + 5):
        append(buffer, str(index))
    assert len(persisted) == MAX_PENDING_LOGS + 5
    assert [item["message"] for item in buffer.snapshot()] == ["203", "204"]
    assert len(loop.callbacks) == 1
    loop.drain()
    assert [item["message"] for item in subscription.drain()] == [
        str(index) for index in range(5, 205)
    ]


def test_disabled_handler_does_not_attach_to_clean_logger():
    root = logging.Logger("isolated")
    assert sync_root_handler(LogBuffer(capacity=lambda: 0), root) is None
    assert root.handlers == []


async def test_blocked_loop_keeps_only_latest_200_logs_and_one_callback():
    loop = asyncio.get_running_loop()
    buffer = LogBuffer(capacity=lambda: 10)
    subscription = buffer.hub.subscribe()
    buffer.bind_loop(loop)
    initial = len(loop._ready)

    def producer():
        for index in range(2000):
            append(buffer, f"line {index:04d} " + "A" * 3980)

    thread = threading.Thread(target=producer)
    thread.start()
    thread.join()  # 刻意阻塞事件循环，确认线程持续写日志也不产生无界 callbacks。
    assert len(loop._ready) - initial == 1
    assert len(buffer._lines) == 10 and not subscription.events
    assert len(buffer._pending) == MAX_PENDING_LOGS == 200
    assert sum(len(item["message"]) for item in buffer._pending) <= 200 * 4000
    callback = loop._ready[-1]
    assert callback._args == (loop,)  # 唤醒只持循环，不持每条日志正文。
    await asyncio.sleep(0)
    events = subscription.drain()
    assert [item["seq"] for item in events] == list(range(1801, 2001))
    assert not buffer._pending and not buffer._dispatch_scheduled
    append(buffer, "next wave")
    await asyncio.sleep(0)
    assert [item["message"] for item in subscription.drain()] == ["next wave"]
    buffer.hub.unsubscribe(subscription)


def test_rebinding_same_loop_preserves_queue_and_rebinding_new_loop_discards_old_callback():
    buffer = LogBuffer(capacity=lambda: 5)
    subscription = buffer.hub.subscribe()
    old, new = QueuedLoop(), QueuedLoop()
    buffer.bind_loop(old)
    append(buffer, "old")
    buffer.bind_loop(old)
    assert len(buffer._pending) == 1 and len(old.callbacks) == 1
    buffer.bind_loop(new)
    append(buffer, "new")
    old.drain()
    assert len(buffer._pending) == 1 and buffer._dispatch_scheduled
    new.drain()
    assert [item["message"] for item in subscription.drain()] == ["new"]


@pytest.mark.parametrize("action", ["append", "snapshot", "drain"])
def test_disabling_logs_clears_pending_and_never_delivers_old_rows(action):
    capacity = [5]
    buffer = LogBuffer(capacity=lambda: capacity[0])
    subscription = buffer.hub.subscribe()
    loop = QueuedLoop()
    buffer.bind_loop(loop)
    append(buffer)
    capacity[0] = 0
    if action == "append":
        assert append(buffer) is None
    elif action == "snapshot":
        assert buffer.snapshot() == []
    loop.drain()
    assert not buffer._pending and not buffer._dispatch_scheduled
    assert subscription.drain() == []


@pytest.mark.parametrize("extra_write", [False, True])
def test_unsubscribed_logs_do_not_accumulate_or_replay(extra_write):
    buffer = LogBuffer(capacity=lambda: 5)
    subscription = buffer.hub.subscribe()
    loop = QueuedLoop()
    buffer.bind_loop(loop)
    append(buffer)
    buffer.hub.unsubscribe(subscription)
    if extra_write:
        append(buffer)
        assert not buffer._pending
    loop.drain()
    assert not buffer._pending and not buffer._dispatch_scheduled
    assert subscription.drain() == []


@pytest.mark.parametrize("rebind", [False, True])
def test_loop_closes_between_check_and_scheduling_does_not_leave_stuck_dispatch(rebind):
    buffer = LogBuffer(capacity=lambda: 5)
    subscription = buffer.hub.subscribe()
    new = QueuedLoop()

    class ClosingLoop(QueuedLoop):
        def call_soon_threadsafe(self, callback, *args):
            if rebind:
                buffer.bind_loop(new)
                append(buffer, "new")
            raise RuntimeError("loop closed")

    buffer.bind_loop(ClosingLoop())
    append(buffer, "old")
    if rebind:
        assert len(buffer._pending) == 1 and buffer._dispatch_scheduled
        new.drain()
        assert [item["message"] for item in subscription.drain()] == ["new"]
    else:
        assert not buffer._pending and not buffer._dispatch_scheduled
        buffer.bind_loop(new)
        append(buffer, "recovered")
        new.drain()
        assert [item["message"] for item in subscription.drain()] == ["recovered"]


def test_logs_arriving_during_batch_delivery_schedule_next_bounded_drain():
    loop = QueuedLoop()
    buffer = LogBuffer(capacity=lambda: 5)
    subscription = buffer.hub.subscribe()
    buffer.bind_loop(loop)
    publish = buffer.hub.publish

    def concurrent_publish(payload):
        publish(payload)
        if payload["message"] == "first":
            append(buffer, "second")
            append(buffer, "third")

    buffer.hub.publish = concurrent_publish
    append(buffer, "first")
    loop.drain()
    assert len(loop.callbacks) == 1 and len(buffer._pending) == 2
    assert [item["message"] for item in subscription.drain()] == ["first"]
    loop.drain()
    assert [item["message"] for item in subscription.drain()] == ["second", "third"]
    assert not buffer._pending and not buffer._dispatch_scheduled
