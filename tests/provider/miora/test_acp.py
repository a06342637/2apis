from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from src.provider.base import ErrKind, UpstreamProtocolViolation
from src.provider.miora.acp import AcpConnection, AcpHTTPError, _PromptPostEOF, jsonrpc_frames

URL = "https://unit.sandbox.cloudstudio.club/acp"


def packet(frame):
    return f"data: {json.dumps(frame)}\n\n".encode()


class QueueStream(httpx.AsyncByteStream):
    def __init__(self):
        self.queue = asyncio.Queue()
        self.closed = False

    async def __aiter__(self):
        while True:
            chunk = await self.queue.get()
            if chunk is None:
                return
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("status,kind", [(401, ErrKind.SOFT), (402, ErrKind.CREDIT)])
def test_short_ticket_classification(status, kind):
    assert AcpHTTPError(status, b"").kind() == kind


@pytest.mark.parametrize("method,header", [
    ("GET", True), ("POST", True), ("GET", False), ("POST", False)])
async def test_acp_429_keeps_account_retry_hints(method, header, monkeypatch):
    monkeypatch.setattr("src.provider.base.time.time", lambda: 1000)
    async def handle(request):
        return httpx.Response(429, json={"message": "usage reset at 1970-01-01 00:17:10 UTC+0"},
                              headers={"Retry-After": "20"} if header else {})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        connection = AcpConnection(http, URL, "synthetic")
        if method == "GET":
            with pytest.raises(AcpHTTPError) as caught:
                await connection.__aenter__()
            error = caught.value
        else:
            await connection._post({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            error = await connection._queue.get()
        assert error.kind() == ErrKind.SOFT
        assert error.retry_after_seconds == (20 if header else 30)
        assert error.retry_at == (1020 if header else 1030)


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


@pytest.mark.parametrize("post_last", [False, True])
async def test_post_sse_eof_recovers_only_after_complete_get_end_turn(post_last):
    incoming = QueueStream()
    end_read = asyncio.Event()

    class PostStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            if post_last:
                await end_read.wait()
            yield b": POST stream ended without a result\n\n"

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": "s", "update": {"sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "complete answer"}}}}))
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/endTurn",
                                         "params": {"sessionId": "s", "stopReason": "end_turn"}}))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=PostStream())

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private", timeout=.2) as connection):
        # 旧请求残留的内部 EOF 标识不能完成当前请求。
        await connection._queue.put(_PromptPostEOF(999))
        frames = []
        async for frame in connection.rpc_frames("session/prompt", {"sessionId": "s"}):
            frames.append(frame)
            if frame.get("method") == "session/endTurn":
                end_read.set()
        assert frames[0]["params"]["update"]["content"]["text"] == "complete answer"
        assert frames[-1] == {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}


@pytest.mark.parametrize("mode", ["failed", "missing", "wrong_session", "post_error", "get_error"])
async def test_post_eof_does_not_hide_failed_missing_or_rpc_error_end_turn(mode):
    incoming = QueueStream()

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        if mode != "missing":
            await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/endTurn",
                "params": {"sessionId": "other" if mode == "wrong_session" else "s",
                           "stopReason": "error" if mode == "failed" else "end_turn"}}))
        error = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32001, "message": "failed"}}
        if mode == "get_error":
            await incoming.queue.put(packet(error))
        body = packet(error) if mode == "post_error" else b": EOF\n\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=body)

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private", timeout=.03) as connection):
        if mode in ("get_error", "post_error"):
            frames = [frame async for frame in connection.rpc_frames(
                "session/prompt", {"sessionId": "s"})]
            assert "error" in frames[-1]
            assert not any("result" in frame for frame in frames)
        else:
            with pytest.raises(UpstreamProtocolViolation):
                _ = [frame async for frame in connection.rpc_frames(
                    "session/prompt", {"sessionId": "s"})]


async def test_failed_get_end_turn_after_post_eof_is_rejected_without_timer_race(monkeypatch):
    """精确控制两条通道的完成顺序，不能靠超时碰巧证明失败 endTurn 被拒绝。"""
    async with httpx.AsyncClient() as client:
        connection = AcpConnection(client, URL, "private")

        async def post(request):
            connection._queue.put_nowait({"jsonrpc": "2.0", "method": "session/endTurn",
                "params": {"sessionId": "s", "stopReason": "error"}})
            connection._queue.put_nowait(_PromptPostEOF(request["id"]))

        monkeypatch.setattr(connection, "_post", post)
        with pytest.raises(UpstreamProtocolViolation, match="未成功结束"):
            _ = [frame async for frame in connection.rpc_frames(
                "session/prompt", {"sessionId": "s"})]


@pytest.mark.parametrize("reason", ["error", "cancelled", "canceled"])
@pytest.mark.parametrize("result_first", [False, True])
async def test_failed_get_end_turn_cannot_be_hidden_by_successful_post_result(
    monkeypatch, reason, result_first
):
    async with httpx.AsyncClient() as client:
        connection = AcpConnection(client, URL, "synthetic")
        terminal = {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}
        end = {"jsonrpc": "2.0", "method": "session/endTurn",
               "params": {"sessionId": "s", "stopReason": reason}}

        async def post(_request):
            connection._queue.put_nowait({"jsonrpc": "2.0", "method": "session/update",
                                         "params": {"value": "partial answer"}})
            for frame in ([terminal, end] if result_first else [end, terminal]):
                connection._queue.put_nowait(frame)

        monkeypatch.setattr(connection, "_post", post)
        seen = []
        with pytest.raises(UpstreamProtocolViolation, match="未成功结束"):
            async for frame in connection.rpc_frames("session/prompt", {"sessionId": "s"}):
                seen.append(frame)
        assert seen[0]["params"]["value"] == "partial answer"
        assert not any("result" in frame for frame in seen)
        assert connection._active_method == ""


async def test_post_eof_needs_an_explicit_success_reason_in_get_end_turn(monkeypatch):
    async with httpx.AsyncClient() as client:
        connection = AcpConnection(client, URL, "synthetic")

        async def post(request):
            connection._queue.put_nowait({"jsonrpc": "2.0", "method": "session/endTurn",
                                         "params": {"sessionId": "s"}})
            connection._queue.put_nowait(_PromptPostEOF(request["id"]))

        monkeypatch.setattr(connection, "_post", post)
        with pytest.raises(UpstreamProtocolViolation, match="未成功结束"):
            _ = [frame async for frame in connection.rpc_frames(
                "session/prompt", {"sessionId": "s"})]


async def test_post_eof_and_colliding_server_request_does_not_count_as_rpc_result():
    incoming = QueueStream()
    replies = []

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        body = json.loads(request.content)
        if "method" not in body:
            replies.append(body)
            return httpx.Response(202)
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/endTurn",
                                         "params": {"sessionId": "s", "stopReason": "end_turn"}}))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=packet({
            "jsonrpc": "2.0", "id": 1, "method": "client/callback", "params": {}}))

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private") as connection):
        frames = [frame async for frame in connection.rpc_frames(
            "session/prompt", {"sessionId": "s"})]
        assert frames[-1]["result"] == {"stopReason": "end_turn"}
    assert replies[0]["result"]["outcome"]["outcome"] == "cancelled"


async def test_sse_notification_response_does_not_mark_a_prompt_eof():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=packet({
                "jsonrpc": "2.0", "id": None, "result": {}})))) as client:
        connection = AcpConnection(client, URL, "private")
        await connection.notify("session/cancel", {"sessionId": "s"})
        # 通知ACK不属于对话结果，也不能塞住没有消费者的队列。
        assert connection._queue.empty()


async def test_connection_timeout_bounds_wait_for_get_response_headers():
    blocker = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request):
        try:
            await blocker.wait()
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(UpstreamProtocolViolation, match="建链超时"):
            await AcpConnection(client, URL, "private", timeout=.01).__aenter__()
    assert cancelled.is_set()


async def test_connection_timeout_also_closes_stalled_error_response_body():
    incoming = QueueStream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(401, stream=incoming))) as client:
        with pytest.raises(UpstreamProtocolViolation, match="建链超时"):
            await AcpConnection(client, URL, "private", timeout=.01).__aenter__()
    assert incoming.closed


async def test_cancellation_notification_cannot_hold_connection_forever():
    incoming = QueueStream()
    blocker = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        try:
            await blocker.wait()
        finally:
            cancelled.set()

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private", timeout=.01) as connection):
        await connection.notify("session/cancel", {"sessionId": "s"})
    assert cancelled.is_set()


@pytest.mark.parametrize(
    "error_type", [httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout])
@pytest.mark.parametrize("terminal", [None, "result", "error"])
async def test_prompt_post_transport_interruption_preserves_rpc_terminal_or_get_completion(
        error_type, terminal):
    incoming = QueueStream()

    class InterruptedPost(httpx.AsyncByteStream):
        async def __aiter__(self):
            if terminal is not None:
                value = {"stopReason": "end_turn"} if terminal == "result" else {"code": -32001}
                yield packet({"jsonrpc": "2.0", "id": 1, terminal: value})
            raise error_type("POST response interrupted")

    async def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        await incoming.queue.put(packet({"jsonrpc": "2.0", "method": "session/endTurn",
                                         "params": {"sessionId": "s", "stopReason": "end_turn"}}))
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=InterruptedPost())

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private") as connection):
        frames = [frame async for frame in connection.rpc_frames(
            "session/prompt", {"sessionId": "s"})]
        if terminal == "error":
            assert "error" in frames[-1]
            assert not any("result" in frame for frame in frames)
        else:
            assert frames[-1]["result"] == {"stopReason": "end_turn"}


async def test_control_post_transport_interruption_is_not_recovered_as_prompt():
    incoming = QueueStream()

    class InterruptedControl(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b": no result\n\n"
            raise httpx.ReadError("control failed")

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, headers={"Acp-Connection-Id": "c"}, stream=incoming)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              stream=InterruptedControl())

    async with (httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client,
                AcpConnection(client, URL, "private") as connection):
        with pytest.raises(httpx.ReadError):
            await connection.call("initialize", {})
