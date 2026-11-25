"""真实 executor + 心跳/首帧包装器在跨 Task 接力后仍正确恢复作用域与租约。"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

import pytest

from src.api import streaming as streaming_module
from src.api.endpoints import _release_when_done
from src.api.streaming import respond_with_stream_start, with_keepalive
from src.compat.anthropic.response import AnthropicStreamTranslator
from src.compat.openai.request import parse_chat_request
from src.engine import executor as executor_module
from src.engine.executor import Executor, ExecutorDeps
from src.engine.inflight import InflightGuard
from src.engine.scheduler import Candidate, Scheduler
from src.provider.base import Event, EventKind, Usage
from src.request_context import credential_scope, get_credential_id


class Credentials:
    def __init__(self):
        self.successes = []

    def candidates(self, providers=None, *, selectable_only=False, model=None):
        return [Candidate(credential_id="credential", provider="trae")]

    def provider_of(self, credential_id):
        return "trae"

    def credential_data(self, credential_id):
        return {"accessToken": "test"}

    def save_success(self, credential_id, *, model=None):
        self.successes.append(credential_id)


class Records:
    def __init__(self):
        self.rows = []

    def record(self, **fields):
        self.rows.append(fields)


class Lease:
    def __init__(self):
        self.released = 0

    def release(self):
        self.released += 1


class ScopedProvider:
    id = "trae"

    def __init__(self, *, pause=False, start_paused=False):
        self.probe = ContextVar("provider_scope", default="parent")
        self.seen = []
        self.restored = []
        self.started = asyncio.Event()
        self.first_allowed = asyncio.Event()
        if not start_paused:
            self.first_allowed.set()
        self.waiting = asyncio.Event()
        self.blocker = asyncio.Event()
        self.pause = pause

    async def stream_chat(self, credential_data, payload, model):
        token = self.probe.set("inside")
        # 真实 token 跨 yield 保留；_TimedReader 的每次取帧也必须在原 Context。
        with credential_scope(get_credential_id()):
            try:
                self.started.set()
                await self.first_allowed.wait()
                for event in (Event(kind=EventKind.CONTENT, content="hello"),
                              Event(kind=EventKind.USAGE, usage=Usage(7, 3)),
                              Event(kind=EventKind.FINISH, finish_reason="stop")):
                    self.seen.append((self.probe.get(), get_credential_id()))
                    yield event
                    if self.pause:
                        self.waiting.set()
                        await self.blocker.wait()
            finally:
                self.probe.reset(token)
                self.restored.append(self.probe.get())


def build(provider):
    credentials, records = Credentials(), Records()
    inflight, lease = InflightGuard(lambda: 1), Lease()
    executor = Executor(ExecutorDeps(providers={"trae": provider}, credentials=credentials,
        scheduler=Scheduler(), stats=records, inflight=inflight))
    request = parse_chat_request({"model": "glm-5.2@trae", "stream": True,
                                   "messages": [{"role": "user", "content": "hello"}]})
    return executor, request, credentials, records, inflight, lease


async def response_for(executor, request, lease, anthropic):
    translator = AnthropicStreamTranslator("glm-5.2") if anthropic else None
    ping = translator.keepalive() if translator else b": keepalive\n\n"
    frames = with_keepalive(executor.stream_guarded(request, translator=translator),
                            interval=5, keepalive_frame=ping)
    return await respond_with_stream_start(_release_when_done(frames, lease), {})


@pytest.mark.parametrize("anthropic", [False, True])
@pytest.mark.parametrize("finish", ["normal", "first_frame", "done"])
async def test_complete_or_closed_stream_restores_context_and_releases_leases(
        anthropic, finish, caplog):
    provider = ScopedProvider()
    executor, request, credentials, records, inflight, lease = build(provider)
    with credential_scope("parent-credential"):
        response = await asyncio.create_task(response_for(executor, request, lease, anthropic))
        assert executor_module._candidate_scope.get() is None

        async def consume():
            frames = []
            async for frame in response.body_iterator:
                frames.append(frame)
                done = b"message_stop" in frame if anthropic else b"[DONE]" in frame
                if finish == "first_frame" or finish == "done" and done:
                    break
            await response.body_iterator.aclose()
            return frames

        frames = await asyncio.create_task(consume())
        assert get_credential_id() == "parent-credential"
        assert executor_module._candidate_scope.get() is None
    assert provider.restored == ["parent"]
    assert all(probe == "inside" and credential == "credential"
               for probe, credential in provider.seen)
    assert provider.probe.get() == "parent"
    assert inflight.count("credential") == 0
    assert lease.released == 1
    assert len(records.rows) == 1
    assert records.rows[0]["ok"] is (finish != "first_frame")
    assert not any(b"internal_error" in frame for frame in frames)
    assert "different Context" not in caplog.text
    if finish != "first_frame":
        assert credentials.successes == ["credential"]


@pytest.mark.parametrize("anthropic", [False, True])
async def test_client_cancel_while_upstream_waits_restores_context_and_releases_leases(
        anthropic, caplog):
    provider = ScopedProvider(pause=True)
    executor, request, _, records, inflight, lease = build(provider)
    response = await response_for(executor, request, lease, anthropic)
    while b"hello" not in await anext(response.body_iterator):
        pass  # Anthropic 的 message_start/content_block_start 先于正文。
    pending = asyncio.create_task(anext(response.body_iterator))
    await provider.waiting.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.create_task(response.body_iterator.aclose())
    assert provider.restored == ["parent"]
    assert inflight.count("credential") == 0
    assert lease.released == 1
    assert len(records.rows) == 1
    assert records.rows[0]["error_type"] == "client_disconnect"
    assert "different Context" not in caplog.text


async def test_grace_timeout_keeps_pending_context_and_slow_first_frame(monkeypatch):
    monkeypatch.setattr(streaming_module, "STREAM_START_GRACE_SECONDS", .01)
    provider = ScopedProvider(start_paused=True)
    executor, request, _, records, inflight, lease = build(provider)
    task = asyncio.create_task(response_for(executor, request, lease, False))
    await provider.started.wait()
    response = await task
    assert provider.restored == []
    assert inflight.count("credential") == 1
    provider.first_allowed.set()
    frames = [frame async for frame in response.body_iterator]
    assert any(b"hello" in frame for frame in frames)
    assert frames[-1] == b"data: [DONE]\n\n"
    assert provider.restored == ["parent"]
    assert inflight.count("credential") == 0
    assert lease.released == 1
    assert records.rows[0]["ok"] is True


async def test_immediate_first_frame_cancel_releases_preexisting_api_lease():
    provider = ScopedProvider(start_paused=True)
    executor, request, _, _, inflight, lease = build(provider)
    task = asyncio.create_task(response_for(executor, request, lease, False))
    await asyncio.sleep(0)  # 路由已开始首帧预取；不额外等待源生成器的调度层数。
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inflight.count("credential") == 0
    assert lease.released == 1


async def test_real_source_close_error_is_reported_but_inflight_lease_is_released(caplog):
    probe = ContextVar("close_failure_probe", default="parent")
    closed = []

    class CloseFailureSource:
        def __init__(self):
            self.started = False
            self.token = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.started:
                raise StopAsyncIteration
            self.started = True
            self.token = probe.set("inside")
            return Event(kind=EventKind.CONTENT, content="hello")

        async def aclose(self):
            probe.reset(self.token)
            closed.append(probe.get())
            raise RuntimeError("intentional source close failure")

    class CloseFailureProvider:
        id = "trae"

        def stream_chat(self, credential_data, payload, model):
            return CloseFailureSource()

    executor, request, _, _, inflight, lease = build(CloseFailureProvider())
    response = await response_for(executor, request, lease, False)
    frames = [frame async for frame in response.body_iterator]
    assert closed == ["parent"]
    assert probe.get() == "parent"
    assert inflight.count("credential") == 0
    assert lease.released == 1
    assert any(b"internal_error" in frame for frame in frames)
    assert "intentional source close failure" in caplog.text
    assert "different Context" not in caplog.text
