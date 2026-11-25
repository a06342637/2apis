"""小型 ACP 缓冲确实施加背压，且满队列不妨碍控制/取消/退出。"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.provider.miora.acp import AcpConnection, AcpHTTPError
from tests.provider.miora.test_acp import URL, QueueStream, packet


def update(index):
    return {"jsonrpc": "2.0", "method": "session/update", "params": {"index": index}}


def end_turn():
    return {"jsonrpc": "2.0", "method": "session/endTurn",
            "params": {"sessionId": "s", "stopReason": "end_turn"}}


class CountedStream(QueueStream):
    def __init__(self):
        super().__init__()
        self.pulled = 0
        self.blocked = asyncio.Event()

    async def __aiter__(self):
        async for chunk in super().__aiter__():
            self.pulled += 1
            if self.pulled == 6:
                self.blocked.set()
            yield chunk


@pytest.mark.parametrize("close_early", [False, True])
async def test_get_producer_blocks_at_four_frames_then_resumes_or_closes(close_early):
    incoming = CountedStream()

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        for index in range(8):
            await incoming.queue.put(packet(update(index)))
        await incoming.queue.put(packet(end_turn()))
        await incoming.queue.put(packet({"jsonrpc": "2.0", "id": 1,
                                         "result": {"stopReason": "end_turn"}}))
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        async with (asyncio.timeout(2), AcpConnection(client, URL, "synthetic") as connection):
            frames = connection.rpc_frames("session/prompt", {"sessionId": "s"})
            first = await anext(frames)
            assert first["params"]["index"] == 0
            await asyncio.wait_for(incoming.blocked.wait(), 1)
            # 消费者停在第1次yield：4帧已排队，第6帧等待put，原流剩余4帧没读取。
            assert connection._queue.maxsize == connection._queue.qsize() == 4
            await asyncio.sleep(0)
            assert incoming.pulled == 6 and incoming.queue.qsize() == 4
            if close_early:
                await asyncio.wait_for(frames.aclose(), 1)
            else:
                received = [first] + [frame async for frame in frames]
                assert [frame["params"]["index"] for frame in received
                        if frame.get("method") == "session/update"] == list(range(8))
                assert received[-1]["result"] == {"stopReason": "end_turn"}
                assert incoming.pulled == 10
        assert connection._reader.done() and incoming.closed


async def test_full_post_producer_is_cancelled_and_stream_closed_when_consumer_stops():
    incoming = QueueStream()
    outgoing = CountedStream()

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        for index in range(8):
            await outgoing.queue.put(packet(update(index)))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=outgoing)

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "synthetic") as connection):
        frames = connection.rpc_frames("session/prompt", {"sessionId": "s"})
        assert (await anext(frames))["params"]["index"] == 0
        await asyncio.wait_for(outgoing.blocked.wait(), 1)
        assert connection._queue.qsize() == 4 and outgoing.pulled == 6
        await asyncio.wait_for(frames.aclose(), 1)
        assert outgoing.closed and connection._active_method == ""


@pytest.mark.parametrize("method", ["initialize", "session/load", "session/set_model"])
async def test_get_only_control_result_survives_prepare_phase_notification_noise(method):
    incoming = QueueStream()

    async def handle(request):
        if request.method == "GET":
            for index in range(20):
                await incoming.queue.put(packet(update(index)))
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        request_id = json.loads(request.content)["id"]
        for index in range(20):
            await incoming.queue.put(packet(update(index)))
        await incoming.queue.put(packet({"jsonrpc": "2.0", "id": request_id,
                                         "result": {"prepared": method}}))
        return httpx.Response(202)

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "synthetic") as connection):
        assert await asyncio.wait_for(connection.call(method, {}), 1) == {"prepared": method}
        assert connection._queue.empty() and connection._active_method == ""


@pytest.mark.parametrize("content_type", ["application/json", "text/event-stream"])
async def test_callback_ack_cannot_enqueue_into_its_consumers_full_queue(content_type):
    incoming = CountedStream()
    callback_started = asyncio.Event()

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        frame = json.loads(request.content)
        if "method" in frame:
            await incoming.queue.put(packet({"jsonrpc": "2.0", "id": "callback",
                                             "method": "session/request_permission"}))
            for index in range(4):
                await incoming.queue.put(packet(update(index)))
            await incoming.queue.put(packet(end_turn()))
            await incoming.queue.put(packet({"jsonrpc": "2.0", "id": 1,
                                             "result": {"stopReason": "end_turn"}}))
            return httpx.Response(202)
        callback_started.set()
        await incoming.blocked.wait()
        # 回调消费端正同步等待HTTP响应，队列已经满；ACK不应再次写入它。
        assert connection._queue.qsize() == 4
        ack = {"jsonrpc": "2.0", "id": "callback", "result": {}}
        return httpx.Response(200, headers={"Content-Type": content_type},
            content=json.dumps(ack).encode() if content_type == "application/json" else packet(ack))

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "synthetic") as connection):
        async def collect():
            return [frame async for frame in connection.rpc_frames("session/prompt",
                                                                  {"sessionId": "s"})]

        frames = await asyncio.wait_for(collect(), 1)
        assert callback_started.is_set()
        assert [frame["params"]["index"] for frame in frames
                if frame.get("method") == "session/update"] == list(range(4))
        assert frames[-1]["result"] == {"stopReason": "end_turn"}


@pytest.mark.parametrize("mode", ["json", "sse", "http_error", "invalid"])
async def test_cancel_ack_or_failure_never_blocks_full_queue_with_no_consumer(mode):
    ack = {"jsonrpc": "2.0", "id": None, "result": {}}
    response = {
        "json": httpx.Response(200, json=ack),
        "sse": httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=packet(ack)),
        "http_error": httpx.Response(503),
        "invalid": httpx.Response(200, headers={"Content-Type": "application/json"},
                                  content=b"bad"),
    }[mode]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
        connection = AcpConnection(client, URL, "synthetic", timeout=.2)
        for index in range(4):
            connection._queue.put_nowait(update(index))
        await asyncio.wait_for(connection.notify("session/cancel", {"sessionId": "s"}), .1)
        assert connection._queue.qsize() == 4


async def test_callback_http_failure_propagates_without_queueing_back_to_consumer():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(503))) as client:
        connection = AcpConnection(client, URL, "synthetic")
        for index in range(4):
            connection._queue.put_nowait(update(index))
        with pytest.raises(AcpHTTPError):
            await asyncio.wait_for(connection._reject_request({"id": "callback"}), .1)
        assert connection._queue.qsize() == 4


async def test_cancelling_consumer_during_callback_closes_full_get_stream():
    incoming = CountedStream()
    callback_cancelled = asyncio.Event()
    stalled_callback = asyncio.Event()

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        frame = json.loads(request.content)
        if "method" in frame:
            await incoming.queue.put(packet({"jsonrpc": "2.0", "id": "callback",
                                             "method": "session/request_permission"}))
            for index in range(5):
                await incoming.queue.put(packet(update(index)))
            return httpx.Response(202)
        try:
            await stalled_callback.wait()
        finally:
            callback_cancelled.set()

    async with (asyncio.timeout(2),
                httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client):
        async with AcpConnection(client, URL, "synthetic") as connection:
            async def consume():
                return [frame async for frame in connection.rpc_frames(
                    "session/prompt", {"sessionId": "s"})]

            consumer = asyncio.create_task(consume())
            await incoming.blocked.wait()
            assert connection._queue.qsize() == 4
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
            assert callback_cancelled.is_set() and connection._active_method == ""
        assert incoming.closed and connection._reader.done()
