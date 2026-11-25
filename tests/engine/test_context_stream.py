from __future__ import annotations

import asyncio
from contextvars import ContextVar

import pytest

from src.engine.context_stream import ContextBoundIterator
from src.request_context import credential_scope, get_credential_id


async def test_raw_stream_transfer_reproduces_contextvar_token_error():
    probe = ContextVar("raw_stream_probe", default="parent")

    async def source():
        token = probe.set("inside")
        try:
            yield "first"
        finally:
            probe.reset(token)

    stream = source()
    assert await asyncio.create_task(anext(stream)) == "first"
    with pytest.raises(ValueError, match="different Context"):
        await asyncio.create_task(stream.aclose())
    assert probe.get() == "parent"


async def test_context_bound_pulls_and_close_can_move_between_tasks():
    probe = ContextVar("bound_stream_probe", default="parent")
    observed = []

    async def source():
        token = probe.set("inside")
        with credential_scope("stream-credential"):
            try:
                for value in ("first", "second"):
                    observed.append((probe.get(), get_credential_id()))
                    yield value
            finally:
                probe.reset(token)
                observed.append((probe.get(), get_credential_id()))

    with credential_scope("parent-credential"):
        stream = ContextBoundIterator(source())
        assert stream.__aiter__() is stream
        assert await asyncio.create_task(anext(stream)) == "first"
        assert await asyncio.create_task(anext(stream)) == "second"
        await asyncio.create_task(stream.aclose())
        assert probe.get() == "parent"
        assert get_credential_id() == "parent-credential"
    assert observed == [("inside", "stream-credential"), ("inside", "stream-credential"),
                        ("parent", "stream-credential")]


async def test_context_bound_iterator_remains_lazy_and_preserves_real_errors():
    probe = ContextVar("error_stream_probe", default="parent")
    advances = []
    error = RuntimeError("real upstream error")

    async def source():
        token = probe.set("inside")
        try:
            advances.append("first")
            yield "value"
            advances.append("failure")
            raise error
        finally:
            probe.reset(token)
            advances.append("closed")

    stream = ContextBoundIterator(source())
    assert advances == []
    assert await anext(stream) == "value"
    assert advances == ["first"]
    with pytest.raises(RuntimeError) as caught:
        await asyncio.create_task(anext(stream))
    assert caught.value is error
    assert advances == ["first", "failure", "closed"]
    await stream.aclose()


async def test_context_bound_cancelled_pull_restores_scope_before_close():
    probe = ContextVar("cancel_stream_probe", default="parent")
    started = asyncio.Event()
    blocker = asyncio.Event()
    restored = []

    async def source():
        token = probe.set("inside")
        try:
            yield "first"
            started.set()
            await blocker.wait()
        finally:
            probe.reset(token)
            restored.append(probe.get())

    stream = ContextBoundIterator(source())
    assert await anext(stream) == "first"
    pending = asyncio.create_task(anext(stream))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    await asyncio.create_task(stream.aclose())
    assert restored == ["parent"]
    assert probe.get() == "parent"


async def test_context_bound_plain_iterator_without_aclose_is_supported():
    class Empty:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    stream = ContextBoundIterator(Empty())
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    await stream.aclose()
