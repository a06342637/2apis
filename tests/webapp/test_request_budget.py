from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from anyio import CancelScope

from src.webapp import request_budget as mod
from src.webapp.request_budget import (
    MIB,
    BudgetRejected,
    RequestBudget,
    RequestBudgetMiddleware,
    inference_request,
    request_weight_mb,
)


def settings(**values):
    return (
        SimpleNamespace(
            resource_max_active_requests=1,
            resource_max_queued_requests=32,
            resource_queue_timeout_seconds=60,
            resource_input_budget_mb=0,
            **values,
        )
        if not values
        else SimpleNamespace(
            **{
                "resource_max_active_requests": 1,
                "resource_max_queued_requests": 32,
                "resource_queue_timeout_seconds": 60,
                "resource_input_budget_mb": 0,
                **values,
            }
        )
    )


def scope(name="request", *, path="/v1/chat/completions", method="POST", headers=None, kind="http"):
    return {
        "type": kind,
        "path": path,
        "method": method,
        "test_name": name,
        "headers": [(b"content-length", b"0")] if headers is None else headers,
    }


class App:
    def __init__(self):
        self.entered = []
        self.gates = {}
        self.tail = None
        self.body_done = asyncio.Event()
        self.first_chunk = asyncio.Event()
        self.error = None
        self.stream_gate = None

    async def __call__(self, request_scope, receive, send):
        name = request_scope["test_name"]
        self.entered.append(name)
        await receive()
        if name in self.gates:
            await self.gates[name].wait()
        if self.error:
            raise self.error
        await send({"type": "http.response.start", "status": 200, "headers": []})
        if self.stream_gate is not None:
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
            self.first_chunk.set()
            await self.stream_gate.wait()
        await send({"type": "http.response.body", "body": b"done"})
        self.body_done.set()
        if self.tail is not None:
            await self.tail.wait()


def invoke(middleware, request_scope=None, *, sender_error=None):
    calls = SimpleNamespace(receives=0, messages=[])

    async def receive():
        calls.receives += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if sender_error:
            raise sender_error
        calls.messages.append(message.copy())

    return asyncio.create_task(middleware(request_scope or scope(), receive, send)), calls


async def until(check):
    async with asyncio.timeout(3):
        while not check():
            await asyncio.sleep(0)


@pytest.mark.parametrize(
    "path",
    [
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
        "/messages",
        "/v1/images/generations",
        "/v1/videos",
        "/v1/videos/",
        "/api/playground/chat/completions",
        "/v1beta/models/model:generateContent",
        "/v1beta/models/vendor/model:streamGenerateContent",
    ],
)
def test_only_real_inference_post_paths_are_selected(path):
    assert inference_request(scope(path=path))


@pytest.mark.parametrize(
    "value",
    [
        scope(method="GET"),
        scope(kind="websocket"),
        scope(path="/v1/models"),
        scope(path="/api/settings"),
        scope(path="/api/playground/models"),
        scope(path="/v1/messages/count_tokens"),
        scope(path="/v1beta/models/model:countTokens"),
        scope(path="/unknown:generateContent"),
        scope(path="/v1beta/models/model:unknown"),
    ],
)
def test_management_read_only_and_counting_paths_are_not_inference(value):
    assert not inference_request(value)


@pytest.mark.parametrize(
    "size, weight",
    [
        (b"0", 1),
        (b"1", 1),
        (str(MIB).encode(), 1),
        (str(MIB + 1).encode(), 2),
        (str(16 * MIB).encode(), 16),
        (b" 00001 ", 1),
    ],
)
def test_content_length_reservation_rounds_up_to_mib(size, weight):
    assert request_weight_mb(scope(headers=[(b"Content-Length", size)])) == weight


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"content-length", b"")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"bad")],
        [(b"content-length", b"9" * 5000)],
        [(b"content-length", b"1"), (b"Content-Length", b"1")],
        [(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
        [(b"content-length", b"\xb2")],
    ],
)
def test_unknown_invalid_duplicate_or_chunked_length_uses_authoritative_maximum(headers):
    assert request_weight_mb(scope(headers=headers)) == 16
    assert request_weight_mb(scope(path="/api/backups/restore", headers=headers)) == 512


async def test_active_limit_queues_before_body_read_and_releases_after_completion():
    app = App()
    app.gates["first"] = asyncio.Event()
    middleware = RequestBudgetMiddleware(app, settings=settings())
    first, first_calls = invoke(middleware, scope("first"))
    await until(lambda: app.entered == ["first"])
    second, second_calls = invoke(middleware, scope("second"))
    await until(lambda: middleware.budget.queued_requests == 1)
    assert first_calls.receives == 1 and second_calls.receives == 0
    assert middleware.budget.active_requests == middleware.budget.active_input_mb == 1
    app.gates["first"].set()
    await asyncio.gather(first, second)
    assert app.entered == ["first", "second"] and second_calls.receives == 1
    assert middleware.budget.active_requests == middleware.budget.active_input_mb == 0
    assert middleware.budget.queued_requests == 0


async def test_weighted_budget_works_without_active_count_limit_and_keeps_fifo():
    budget = RequestBudget(settings(resource_max_active_requests=0, resource_input_budget_mb=3))
    first = await budget.acquire(1)
    big = asyncio.create_task(budget.acquire(3))
    await until(lambda: budget.queued_requests == 1)
    small = asyncio.create_task(budget.acquire(1))
    await until(lambda: budget.queued_requests == 2)
    assert not big.done() and not small.done()
    await first.release()
    big_lease = await big
    assert not small.done() and budget.active_input_mb == 3
    await big_lease.release()
    small_lease = await small
    await small_lease.release()
    assert budget.active_requests == budget.active_input_mb == budget.queued_requests == 0


@pytest.mark.parametrize("length", [str(5 * MIB).encode(), b"bad"])
async def test_single_request_over_input_budget_is_413_without_reading_or_queueing(length):
    app = App()
    middleware = RequestBudgetMiddleware(app, settings=settings(resource_input_budget_mb=4))
    task, calls = invoke(middleware, scope(headers=[(b"content-length", length)]))
    await task
    assert calls.receives == 0 and app.entered == []
    assert calls.messages[0]["status"] == 413
    assert b"retry-after" not in dict(calls.messages[0]["headers"])
    assert json.loads(calls.messages[1]["body"])["error"]["code"] == "resource_input_limit"
    assert middleware.budget.active_requests == middleware.budget.queued_requests == 0


async def test_full_queue_returns_503_retry_after_without_buffering_new_body():
    app = App()
    app.gates["first"] = asyncio.Event()
    middleware = RequestBudgetMiddleware(app, settings=settings(resource_max_queued_requests=1))
    first, _ = invoke(middleware, scope("first"))
    await until(lambda: app.entered == ["first"])
    second, second_calls = invoke(middleware, scope("second"))
    await until(lambda: middleware.budget.queued_requests == 1)
    third, third_calls = invoke(middleware, scope("third"))
    await third
    assert third_calls.messages[0]["status"] == 503
    assert dict(third_calls.messages[0]["headers"])[b"retry-after"] == b"1"
    assert json.loads(third_calls.messages[1]["body"])["error"]["code"] == "resource_queue_full"
    assert second_calls.receives == third_calls.receives == 0
    app.gates["first"].set()
    await asyncio.gather(first, second)


@pytest.mark.parametrize("timeout", [0, 0.01])
async def test_queue_timeout_returns_503_and_removes_waiter_without_body_read(timeout):
    app = App()
    app.gates["first"] = asyncio.Event()
    middleware = RequestBudgetMiddleware(
        app, settings=settings(resource_queue_timeout_seconds=timeout)
    )
    first, _ = invoke(middleware, scope("first"))
    await until(lambda: app.entered == ["first"])
    second, calls = invoke(middleware, scope("second"))
    await second
    assert calls.messages[0]["status"] == 503 and calls.receives == 0
    assert json.loads(calls.messages[1]["body"])["error"]["code"] == "resource_queue_timeout"
    assert middleware.budget.queued_requests == 0 and middleware.budget.active_requests == 1
    app.gates["first"].set()
    await first


async def test_cancel_queued_request_cleans_waiter_and_does_not_read_body():
    budget = RequestBudget(settings())
    first = await budget.acquire(1)
    queued = asyncio.create_task(budget.acquire(1))
    await until(lambda: budget.queued_requests == 1)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    assert budget.queued_requests == 0 and budget.active_requests == 1
    await first.release()


@pytest.mark.parametrize("error", [RuntimeError("application failed"), asyncio.CancelledError()])
async def test_app_exception_or_cancel_releases_all_active_reservations(error):
    app = App()
    app.error = error
    middleware = RequestBudgetMiddleware(app, settings=settings())
    task, _ = invoke(middleware)
    with pytest.raises(type(error)):
        await task
    assert middleware.budget.active_requests == middleware.budget.active_input_mb == 0


async def test_actual_cancellation_of_active_app_releases_reservation():
    app = App()
    app.gates["first"] = asyncio.Event()
    middleware = RequestBudgetMiddleware(app, settings=settings())
    task, _ = invoke(middleware, scope("first"))
    await until(lambda: app.entered == ["first"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert middleware.budget.active_requests == middleware.budget.active_input_mb == 0


async def test_sending_failure_releases_reservation():
    middleware = RequestBudgetMiddleware(App(), settings=settings())
    task, _ = invoke(middleware, sender_error=OSError("client disconnected"))
    with pytest.raises(OSError):
        await task
    assert middleware.budget.active_requests == middleware.budget.active_input_mb == 0


async def test_streaming_body_holds_budget_until_final_body_then_releases_before_background_work():
    app = App()
    app.stream_gate = asyncio.Event()
    app.tail = asyncio.Event()
    middleware = RequestBudgetMiddleware(app, settings=settings())
    task, _ = invoke(middleware)
    await app.first_chunk.wait()
    assert middleware.budget.active_requests == 1
    app.stream_gate.set()
    await app.body_done.wait()
    assert middleware.budget.active_requests == 0 and not task.done()
    app.tail.set()
    await task
    assert middleware.budget.active_input_mb == 0


async def test_release_is_idempotent_and_cannot_be_interrupted_by_cancel_scope():
    budget = RequestBudget(settings())
    lease = await budget.acquire(1)
    await budget._condition.acquire()
    started, ended = asyncio.Event(), asyncio.Event()

    async def work():
        with CancelScope() as cancellation:
            cancellation.cancel()
            started.set()
            await lease.release()
            ended.set()

    task = asyncio.create_task(work())
    await started.wait()
    assert not ended.is_set()
    budget._condition.release()
    await task
    await lease.release()
    assert ended.is_set() and budget.active_requests == budget.active_input_mb == 0


async def test_read_only_management_and_non_http_requests_bypass_saturated_budget():
    app = App()
    app.gates["first"] = asyncio.Event()
    middleware = RequestBudgetMiddleware(app, settings=settings())
    first, _ = invoke(middleware, scope("first"))
    await until(lambda: middleware.budget.active_requests == 1)
    for value in [
        scope("models", path="/v1/models", method="GET"),
        scope("backup", path="/api/backups/restore"),
        scope("ws", kind="websocket"),
    ]:
        task, calls = invoke(middleware, value)
        await task
        assert calls.receives == 1
    assert middleware.budget.active_requests == 1 and middleware.budget.queued_requests == 0
    app.gates["first"].set()
    await first


async def test_hot_enabling_limits_counts_already_active_legacy_requests():
    config = settings(resource_max_active_requests=0)
    budget = RequestBudget(config)
    leases = [await budget.acquire(1) for _ in range(10)]
    assert budget.active_requests == budget.active_input_mb == 10
    config.resource_max_active_requests = 2
    next_request = asyncio.create_task(budget.acquire(1))
    await until(lambda: budget.queued_requests == 1)
    for lease in leases[:-1]:
        await lease.release()
    extra = await next_request
    assert budget.active_requests == 2
    await extra.release()
    await leases[-1].release()


async def test_hot_increase_is_notified_and_hot_shrink_rejects_oversized_queued_request():
    config = settings(resource_input_budget_mb=3)
    budget = RequestBudget(config)
    first = await budget.acquire(1)
    waiting = asyncio.create_task(budget.acquire(2))
    await until(lambda: budget.queued_requests == 1)
    config.resource_max_active_requests = 2
    await budget.notify()
    second = await waiting
    assert budget.active_requests == 2 and budget.active_input_mb == 3
    third = asyncio.create_task(budget.acquire(2))
    await until(lambda: budget.queued_requests == 1)
    config.resource_input_budget_mb = 1
    await budget.notify()
    with pytest.raises(BudgetRejected) as rejected:
        await third
    assert rejected.value.status == 413 and budget.queued_requests == 0
    await first.release()
    await second.release()


async def test_hot_updates_are_polled_without_external_notifications(monkeypatch):
    assert mod.POLL_SECONDS <= 1
    monkeypatch.setattr(mod, "POLL_SECONDS", 0.01)
    config = settings()
    budget = RequestBudget(config)
    first = await budget.acquire(1)
    waiting = asyncio.create_task(budget.acquire(1))
    await until(lambda: budget.queued_requests == 1)
    config.resource_max_active_requests = 2
    second = await asyncio.wait_for(waiting, 1)
    await first.release()
    await second.release()


async def test_hot_queue_timeout_update_is_applied_to_already_queued_requests(monkeypatch):
    monkeypatch.setattr(mod, "POLL_SECONDS", 0.01)
    config = settings()
    budget = RequestBudget(config)
    first = await budget.acquire(1)
    waiting = asyncio.create_task(budget.acquire(1))
    await until(lambda: budget.queued_requests == 1)
    config.resource_queue_timeout_seconds = 0
    await budget.notify()
    with pytest.raises(BudgetRejected) as rejected:
        await waiting
    assert rejected.value.code == "resource_queue_timeout"
    await first.release()


async def test_many_completed_requests_leave_no_per_request_or_key_state():
    budget = RequestBudget(settings(resource_max_active_requests=0))
    for _ in range(1000):
        lease = await budget.acquire(1)
        await lease.release()
    assert budget.active_requests == budget.active_input_mb == budget.queued_requests == 0
    assert list(budget._waiters) == []
