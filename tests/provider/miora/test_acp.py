from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.provider.base import ErrKind, UpstreamProtocolViolation
from src.provider.miora.acp import AcpConnection, AcpHTTPError, jsonrpc_frames

URL = "https://unit.sandbox.cloudstudio.club/acp"


def packet(frame):
    return f"data: {json.dumps(frame)}\n\n".encode()


class QueueStream(httpx.AsyncByteStream):
    def __init__(self):
        self.queue = asyncio.Queue()

    async def __aiter__(self):
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            yield chunk


@pytest.mark.parametrize("status,kind", [(401, ErrKind.SOFT), (402, ErrKind.CREDIT)])
def test_short_ticket_classification(status, kind):
    assert AcpHTTPError(status, b"").kind() == kind


async def test_frame_control_json_and_event_filter():
    response = httpx.Response(200, content=(
        b'event: heartbeat\ndata: invalid\n\n'
        b'data: {"connected":true}\n\ndata: []\n\n'
        b'data: {"jsonrpc":"","method":"_codebuddy.ai/loadHeartbeat"}\n\n'
        + packet({"jsonrpc": "2.0", "id": 1, "result": {}})))
    assert [frame async for frame in jsonrpc_frames(response)] == [
        {"jsonrpc": "2.0", "id": 1, "result": {}}]


@pytest.mark.parametrize("raw", [b"data: bad\n\n", b'data: {"jsonrpc":"1.0"}\n\n'])
async def test_invalid_frames(raw):
    with pytest.raises(UpstreamProtocolViolation):
        _ = [frame async for frame in jsonrpc_frames(httpx.Response(200, content=raw))]


async def test_double_stream_call_prompt_and_unknown_method():
    incoming = QueueStream()
    requests = []

    async def handle(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "connection"},
                                  stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        frame = json.loads(request.content)
        assert request.headers["Acp-Connection-Id"] == "connection"
        if "method" not in frame:
            assert frame["result"]["outcome"]["outcome"] == "cancelled"
            return httpx.Response(202)
        if frame["method"] == "initialize":
            await incoming.queue.put(packet({"jsonrpc": "2.0", "id": "server-id",
                                             "method": "session/request_permission"}))
            await incoming.queue.put(packet({"jsonrpc": "2.0", "id": frame["id"],
                                             "result": {"protocolVersion": 1}}))
            return httpx.Response(202)
        if frame["method"] == "session/cancel":
            return httpx.Response(202)
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/update",
                                         "params": {"value": "tail"}}))
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/endTurn",
                                         "params": None}))
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/endTurn",
                                         "params": {"sessionId": "other"}}))
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/endTurn",
                                         "params": {"sessionId": "session"}}))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=
            packet({"jsonrpc": "2.0", "id": frame["id"], "result": {"stopReason": "end_turn"}}))

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private") as connection):
        assert await connection.call("initialize", {}) == {"protocolVersion": 1}
        frames = [x async for x in connection.rpc_frames(
            "session/prompt", {"sessionId": "session"})]
        assert frames[-1]["result"]["stopReason"] == "end_turn"
        assert frames[0]["params"]["value"] == "tail"
        await connection.notify("session/cancel", {"sessionId": "session"})
    assert requests[-1].method == "DELETE"
    assert requests[0].headers["Authorization"] == "Bearer private"


@pytest.mark.parametrize("status,headers", [(401, {}), (200, {})])
async def test_enter_failure_closes_stream(status, headers):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(status, headers=headers))) as client:
        with pytest.raises((AcpHTTPError, UpstreamProtocolViolation)):
            async with AcpConnection(client, URL, "private"):
                pytest.fail("connection should not enter")


async def test_exit_unentered_and_delete_network_error():
    incoming = QueueStream()

    def handle(request):
        if request.method == "DELETE":
            raise httpx.ConnectError("offline")
        return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        connection = AcpConnection(client, URL, "private")
        await connection.__aexit__(None, None, None)
        async with connection:
            pass


@pytest.mark.parametrize("response", [
    httpx.Response(500, content=b"upstream error"),
    httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"invalid"),
    httpx.Response(200, headers={"Content-Type": "application/json"}, json=[]),
    httpx.Response(200, headers={"Content-Type": "application/json"}, json={"jsonrpc": "1.0"}),
    httpx.Response(200, content=b"unrecognized"),
])
async def test_post_errors_are_propagated(response):
    incoming = QueueStream()

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        return response

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private") as connection):
        with pytest.raises((AcpHTTPError, UpstreamProtocolViolation)):
            await connection.call("initialize", {})


@pytest.mark.parametrize("raw", [b"", b"data: broken\n\n"])
async def test_get_stream_failure(raw):
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, content=raw)
        return httpx.Response(202)

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private") as connection):
        with pytest.raises(UpstreamProtocolViolation):
            await connection.call("initialize", {})


async def test_timeout():
    incoming = QueueStream()

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        return httpx.Response(202)

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private", timeout=.01) as connection):
        with pytest.raises(UpstreamProtocolViolation, match="超时"):
            await connection.call("initialize", {})


async def test_json_result_and_rpc_error():
    incoming = QueueStream()
    calls = 0

    def handle(request):
        nonlocal calls
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        calls += 1
        field = "result" if calls == 1 else "error"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": calls, field: {}})

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private") as connection):
        assert await connection.call("initialize", {}) == {}
        with pytest.raises(UpstreamProtocolViolation, match="控制请求失败"):
            await connection.call("initialize", {})


async def test_call_missing_result(monkeypatch):
    async def empty_frames(self, method, params):
        for frame in [{"notification": True}]:
            yield frame

    monkeypatch.setattr(AcpConnection, "rpc_frames", empty_frames)
    connection = AcpConnection(httpx.AsyncClient(), URL, "private")
    try:
        with pytest.raises(UpstreamProtocolViolation, match="缺少结果"):
            await connection.call("initialize", {})
    finally:
        await connection.client.aclose()
