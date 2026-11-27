"""取帧超时/取消后的异步清理必须完整执行，并取走已完成任务的异常。"""

import asyncio
import gc

import anyio
import pytest

from src.engine.executor import _TimedReader
from src.provider.base import Event, EventKind


async def test_reader_close_retrieves_error_completed_after_timeout():
    class Source:
        def __init__(self):
            self.ready = asyncio.Event()
            self.closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.ready.wait()
            raise RuntimeError("source failed after timeout")

        async def aclose(self):
            self.closed = True

    source = Source()
    reader = _TimedReader(source)
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    contexts = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        with pytest.raises(TimeoutError):
            await reader.next(.001)
        pending = reader._pending
        source.ready.set()
        await asyncio.wait({pending})  # 只等完成，不取走异常，模拟超时后到达的失败。
        assert pending.done()
        await reader.aclose()
        assert source.closed and reader._pending is None
        del pending, reader
        gc.collect()
        await asyncio.sleep(0)
        assert not contexts
    finally:
        loop.set_exception_handler(previous)


async def test_cancel_scope_cannot_interrupt_reader_source_cleanup():
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def source():
        try:
            entered.set()
            await asyncio.Event().wait()
            yield Event(kind=EventKind.CONTENT, content="late")
        finally:
            await asyncio.sleep(0)
            closed.set()

    reader = _TimedReader(source())
    with anyio.CancelScope() as scope:
        async def cancel_after_entered():
            await entered.wait()
            scope.cancel()

        cancellation = asyncio.create_task(cancel_after_entered())
        try:
            await reader.next(10)
        finally:
            await reader.aclose()
    await cancellation
    assert closed.is_set() and reader._pending is None
