"""SSE 在 ASGI 响应头发送阶段断开，也必须归还源流的真实并发许可。"""

from __future__ import annotations

import asyncio
import gc

import anyio
import pytest
from starlette.requests import ClientDisconnect

from src.api import streaming
from src.key_quota import KeyQuota, KeyQuotaGuard
from src.provider.base import aclose_stream

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _Frames:
    """必须显式 aclose 的源流；BoundedSemaphore 会检测漏关或重复归还。"""

    def __init__(self, *, pending=False, failure=None, close_failure=None):
        self.permit = asyncio.BoundedSemaphore(1)
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.ready = asyncio.Event()
        self.settled = asyncio.Event()
        if not pending:
            self.ready.set()
        self.close_calls = 0
        self.active_reads = 0
        self.failure = failure
        self.close_failure = close_failure
        self.frames = iter([b"data: first\n\n", b"data: second\n\n"])

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.active_reads += 1
        try:
            if not self.entered.is_set():
                await self.permit.acquire()
                self.entered.set()
            await self.ready.wait()
            if self.failure is not None:
                raise self.failure
            try:
                return next(self.frames)
            except StopIteration:
                raise StopAsyncIteration from None
        finally:
            self.active_reads -= 1
            self.settled.set()

    async def aclose(self):
        self.close_calls += 1
        assert self.active_reads == 0, "必须先终止并等待取帧任务，再关闭源流"
        # 真实关闭会 await HTTP/进程清理；取消 scope 不能打断这一步。
        await asyncio.sleep(0)
        self.permit.release()
        self.closed.set()
        if self.close_failure is not None:
            raise self.close_failure


SCOPE = {"type": "http", "asgi": {"spec_version": "2.4"}}


async def _receive():
    # ASGI 2.4 通过 send 异常发现断连，由本文件 legacy 用例的接收回调替换。
    return {"type": "http.disconnect"}


async def _prepared(monkeypatch, pending):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 0.001)
    before = asyncio.all_tasks()
    frames = _Frames(pending=pending)
    frames.guard = KeyQuotaGuard()
    frames.lease = frames.guard.admit("test-key", KeyQuota(max_concurrency=1))
    response = await streaming.respond_with_stream_start(frames, {}, release=frames.lease.release)
    await frames.entered.wait()
    return frames, response, asyncio.all_tasks() - before


async def _cleanup(frames, response, tasks):
    """故障复现失败时也清掉测试自己持有的流，避免 GC 掩盖泄漏或污染后续用例。"""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await aclose_stream(response.body_iterator)
    if not frames.closed.is_set():
        await frames.aclose()


async def test_prefetched_response_creation_finishes_its_grace_timer(monkeypatch):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 10)
    before = asyncio.all_tasks()
    frames = _Frames()
    response = await streaming.respond_with_stream_start(frames, {})
    tasks = asyncio.all_tasks() - before
    try:
        # 首帧已完成，返回 Response 时不应再留下取消尚未完成的宽限定时器。
        assert not tasks
        assert frames.permit.locked()  # 源流仍由尚未发送的 Response 持有。
    finally:
        await _cleanup(frames, response, tasks)


@pytest.mark.parametrize("pending", [False, True], ids=["prefetched", "grace-pending"])
async def test_header_send_oserror_closes_source_before_body_iteration(monkeypatch, pending):
    frames, response, tasks = await _prepared(monkeypatch, pending)
    sent = []

    async def send(message):
        sent.append(message)
        assert message["type"] == "http.response.start"
        raise OSError("client disconnected before response headers")

    try:
        assert frames.permit.locked()
        with pytest.raises(ClientDisconnect):
            await response(SCOPE, _receive, send)
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert frames.guard.inflight("test-key") == 0
        assert not frames.permit.locked()
        assert all(task.done() for task in tasks)
        assert len(sent) == 1
    finally:
        await _cleanup(frames, response, tasks)


@pytest.mark.parametrize("pending", [False, True], ids=["prefetched", "grace-pending"])
async def test_cancel_asgi_task_during_header_send_closes_source(monkeypatch, pending):
    frames, response, tasks = await _prepared(monkeypatch, pending)
    sending = asyncio.Event()

    async def send(message):
        assert message["type"] == "http.response.start"
        sending.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(response(SCOPE, _receive, send))
    try:
        await sending.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert frames.guard.inflight("test-key") == 0
        assert not frames.permit.locked()
        assert all(owned.done() for owned in tasks)
    finally:
        await _cleanup(frames, response, tasks)


@pytest.mark.parametrize("pending", [False, True], ids=["prefetched", "grace-pending"])
async def test_header_cancel_scope_does_not_interrupt_cleanup(monkeypatch, pending):
    frames, response, tasks = await _prepared(monkeypatch, pending)
    try:
        with anyio.CancelScope() as cancel:
            async def send(message):
                assert message["type"] == "http.response.start"
                cancel.cancel()
                await anyio.lowlevel.checkpoint()

            await response(SCOPE, _receive, send)
        assert cancel.cancel_called
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert frames.guard.inflight("test-key") == 0
        assert not frames.permit.locked()
        assert all(task.done() for task in tasks)
    finally:
        await _cleanup(frames, response, tasks)


@pytest.mark.parametrize("pending", [False, True], ids=["prefetched", "grace-pending"])
async def test_legacy_asgi_disconnect_while_sending_headers_closes_source(monkeypatch, pending):
    frames, response, tasks = await _prepared(monkeypatch, pending)
    sending = asyncio.Event()

    async def send(message):
        assert message["type"] == "http.response.start"
        sending.set()
        await asyncio.Event().wait()

    async def receive():
        await sending.wait()
        return {"type": "http.disconnect"}

    try:
        await response({"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send)
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert frames.guard.inflight("test-key") == 0
        assert not frames.permit.locked()
        assert all(task.done() for task in tasks)
    finally:
        await _cleanup(frames, response, tasks)


@pytest.mark.parametrize("pending", [False, True], ids=["prefetched", "grace-pending"])
async def test_complete_asgi_stream_keeps_all_frames_and_closes_once(monkeypatch, pending):
    frames, response, tasks = await _prepared(monkeypatch, pending)
    sent = []

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.start":
            frames.ready.set()

    try:
        await response(SCOPE, _receive, send)
        assert sent[0]["status"] == 200
        body = b"".join(message.get("body", b"") for message in sent)
        assert body == b"data: first\n\ndata: second\n\n"
        assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        assert frames.close_calls == 1
        assert frames.guard.inflight("test-key") == 0
        assert frames.closed.is_set()
        assert not frames.permit.locked()
        assert all(task.done() for task in tasks)
    finally:
        await _cleanup(frames, response, tasks)


@pytest.mark.parametrize("failure", [RuntimeError("prefetch failed"), asyncio.CancelledError()])
async def test_prefetch_failure_closes_owned_source(monkeypatch, failure):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 10)
    frames = _Frames(failure=failure)
    try:
        with pytest.raises(type(failure)):
            await streaming.respond_with_stream_start(frames, {})
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert not frames.permit.locked()
    finally:
        if not frames.closed.is_set():
            await frames.aclose()


@pytest.mark.parametrize("pending", [False, True], ids=["prefetched", "grace-pending"])
async def test_header_failure_also_finishes_keepalive_timer_and_source(monkeypatch, pending):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 0.001)
    before = asyncio.all_tasks()
    frames = _Frames(pending=pending)
    body = streaming.with_keepalive(frames, interval=60)
    response = await streaming.respond_with_stream_start(body, {})
    tasks = asyncio.all_tasks() - before

    async def send(message):
        assert message["type"] == "http.response.start"
        raise OSError("header send failed with keepalive enabled")

    try:
        with pytest.raises(ClientDisconnect):
            await response(SCOPE, _receive, send)
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert all(task.done() for task in tasks)
    finally:
        await _cleanup(frames, response, tasks)


async def test_cancel_scope_while_prefetching_finishes_task_and_source(monkeypatch):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 10)
    before = asyncio.all_tasks()
    frames = _Frames(pending=True)
    try:
        with anyio.CancelScope() as cancel:
            async def cancel_when_entered():
                await frames.entered.wait()
                cancel.cancel()

            async with anyio.create_task_group() as group:
                group.start_soon(cancel_when_entered)
                await streaming.respond_with_stream_start(frames, {})
        assert cancel.cancel_called
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert not frames.permit.locked()
        assert not (asyncio.all_tasks() - before)
    finally:
        tasks = asyncio.all_tasks() - before
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if not frames.closed.is_set():
            await frames.aclose()


async def test_keepalive_scheduling_failure_still_closes_unstarted_source(monkeypatch):
    async def source():
        # 此用例在调度阶段失败，不调用源流的取帧逻辑。
        yield b"unused"

    frames = source()

    def reject_read(coroutine):
        coroutine.close()
        raise RuntimeError("cannot schedule upstream read")

    monkeypatch.setattr(streaming.asyncio, "ensure_future", reject_read)
    with pytest.raises(RuntimeError, match="cannot schedule"):
        await anext(streaming.with_keepalive(frames))
    assert frames.ag_frame is None


async def test_source_cleanup_error_preserves_single_release(monkeypatch):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 0.001)
    frames = _Frames(close_failure=RuntimeError("source close failed"))
    response = await streaming.respond_with_stream_start(frames, {})

    async def send(message):
        assert message["type"] == "http.response.start"
        raise OSError("disconnected")

    with pytest.raises(RuntimeError, match="source close failed"):
        await response(SCOPE, _receive, send)
    assert frames.closed.is_set()
    assert frames.close_calls == 1
    assert not frames.permit.locked()
    await response.body_iterator.aclose()


@pytest.mark.parametrize("keepalive", [False, True],
                         ids=["pending-prefetch", "prefetched-heartbeat"])
async def test_prefetch_finishes_with_error_during_headers_is_retrieved(monkeypatch, keepalive):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 0.1 if keepalive else 0.001)
    frames = _Frames(pending=True, failure=RuntimeError("late prefetch error"))
    body = streaming.with_keepalive(frames, interval=0.001) if keepalive else frames
    response = await streaming.respond_with_stream_start(body, {})

    async def send(message):
        assert message["type"] == "http.response.start"
        frames.ready.set()
        await frames.settled.wait()
        await asyncio.sleep(0)
        raise OSError("disconnected after prefetch failed")

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        with pytest.raises(ClientDisconnect):
            await response(SCOPE, _receive, send)
        assert frames.closed.is_set()
        assert frames.close_calls == 1
        assert not frames.permit.locked()
        del response
        gc.collect()
        await asyncio.sleep(0)
        assert errors == []  # 不能留下 Task exception was never retrieved。
    finally:
        loop.set_exception_handler(previous_handler)


async def test_cancelled_prefetch_before_source_enters_returns_admitted_quota(monkeypatch):
    guard = KeyQuotaGuard()
    lease = guard.admit("test-key", KeyQuota(max_concurrency=1))
    entered = False

    async def source():
        nonlocal entered
        entered = True
        try:
            yield b"data: unused\n\n"
        finally:
            lease.release()

    schedule = asyncio.ensure_future
    calls = 0

    def cancel_first(coroutine):
        nonlocal calls
        task = schedule(coroutine)
        calls += 1
        if calls == 1:
            task.cancel()
        return task

    monkeypatch.setattr(streaming.asyncio, "ensure_future", cancel_first)
    with pytest.raises(asyncio.CancelledError):
        await streaming.respond_with_stream_start(source(), {}, release=lease.release)
    assert not entered
    assert lease.released
    assert guard.inflight("test-key") == 0
    guard.admit("test-key", KeyQuota(max_concurrency=1)).release()


async def test_playground_unstarted_prefetch_releases_admitted_user_quota(monkeypatch):
    from types import SimpleNamespace

    from starlette.requests import Request

    from src.api import playground

    guard = KeyQuotaGuard()
    entered = False
    preflight = []

    async def source(_request, **_kwargs):
        nonlocal entered
        entered = True
        yield b"data: unused\n\n"

    services = SimpleNamespace(
        executor=SimpleNamespace(
            preflight=lambda *_args, **_kwargs: preflight.append("checked"),
            stream_guarded=source),
        user_repo=SimpleNamespace(get=lambda _username: {"quota": '{"max_concurrency":1}'}),
        user_quota=guard, settings=SimpleNamespace(default_model="chat"))
    route = next(route for route in playground.create_router(services).routes
                 if route.path == "/api/playground/chat/completions")

    async def receive():
        return {"type": "http.request", "body": (
            b'{"model":"chat","messages":[{"role":"user","content":"hi"}],"stream":true}'),
            "more_body": False}

    request = Request({"type": "http"}, receive)
    schedule = asyncio.ensure_future

    def cancel_read(coroutine):
        task = schedule(coroutine)
        if coroutine.cr_code.co_name == "__anext__":
            task.cancel()
        return task

    monkeypatch.setattr(streaming.asyncio, "ensure_future", cancel_read)
    with pytest.raises(asyncio.CancelledError):
        await route.endpoint(request, _csrf=None, principal=SimpleNamespace(username="alice"))
    assert preflight == ["checked"]
    assert not entered
    assert guard.inflight("alice") == 0
    guard.admit("alice", KeyQuota(max_concurrency=1)).release()


@pytest.mark.parametrize("failed_schedule", [1, 2], ids=["prefetch", "grace"])
async def test_response_scheduling_failure_returns_quota_and_closes_source(monkeypatch,
                                                                         failed_schedule):
    guard = KeyQuotaGuard()
    lease = guard.admit("test-key", KeyQuota(max_concurrency=1))
    closed = asyncio.Event()
    schedule = asyncio.ensure_future
    calls = 0

    class Source:
        def __aiter__(self):
            return self

        async def __anext__(self):
            # 两个调度失败用例都在事件循环分派取帧前失败。
            raise AssertionError("source must remain unstarted")

        async def aclose(self):
            closed.set()

    def fail_schedule(coroutine):
        nonlocal calls
        calls += 1
        if calls == failed_schedule:
            raise RuntimeError("schedule failed")
        return schedule(coroutine)

    monkeypatch.setattr(streaming.asyncio, "ensure_future", fail_schedule)
    with pytest.raises(RuntimeError, match="schedule failed"):
        await streaming.respond_with_stream_start(Source(), {}, release=lease.release)
    assert closed.is_set()
    assert guard.inflight("test-key") == 0


@pytest.mark.parametrize("failure", [RuntimeError("read failed"), StopAsyncIteration()])
async def test_prefetch_and_close_failures_still_return_admitted_quota(monkeypatch, failure):
    guard = KeyQuotaGuard()
    lease = guard.admit("test-key", KeyQuota(max_concurrency=1))
    frames = _Frames(failure=failure, close_failure=RuntimeError("close failed"))
    with pytest.raises(RuntimeError, match="close failed"):
        await streaming.respond_with_stream_start(frames, {}, release=lease.release)
    assert frames.close_calls == 1
    assert frames.closed.is_set()
    assert guard.inflight("test-key") == 0


@pytest.mark.parametrize("wrapper", ["endpoints", "playground"])
async def test_normal_stream_wrapper_and_response_release_only_its_lease(wrapper):
    from src.api import endpoints, playground

    guard = KeyQuotaGuard()
    quota = KeyQuota(max_concurrency=2)
    lease = guard.admit("test-key", quota)
    other = guard.admit("test-key", quota)
    frames = _Frames()
    module = endpoints if wrapper == "endpoints" else playground
    response = await streaming.respond_with_stream_start(
        module._release_when_done(frames, lease), {}, release=lease.release)
    sent = []

    async def send(message):
        sent.append(message)

    await response(SCOPE, _receive, send)
    assert b"".join(message.get("body", b"") for message in sent) == (
        b"data: first\n\ndata: second\n\n")
    assert frames.close_calls == 1
    assert lease.released
    assert guard.inflight("test-key") == 1  # 双重释放不能释放另一条在途请求。
    other.release()


async def test_cancellation_during_grace_cleanup_closes_owner_and_returns_quota(monkeypatch):
    guard = KeyQuotaGuard()
    lease = guard.admit("test-key", KeyQuota(max_concurrency=1))
    frames = _Frames()
    gather = asyncio.gather
    calls = 0

    async def cancel_after_grace(*tasks, **kwargs):
        nonlocal calls
        calls += 1
        result = await gather(*tasks, **kwargs)
        if calls == 1:
            raise asyncio.CancelledError
        return result

    monkeypatch.setattr(streaming.asyncio, "gather", cancel_after_grace)
    with pytest.raises(asyncio.CancelledError):
        await streaming.respond_with_stream_start(frames, {}, release=lease.release)
    assert frames.closed.is_set()
    assert frames.close_calls == 1
    assert guard.inflight("test-key") == 0
