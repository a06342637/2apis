"""真实HTTP连接池：通知GET占满容量后，独立RPC池仍可初始化、取消和恢复。"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import httpx
import pytest

from src.provider.miora.acp import AcpConnection, AcpHTTPError
from tests.provider.miora.test_acp import packet


class Server:
    def __init__(self):
        self.requests = []
        self.writers = set()
        self.tasks = set()
        self.get_writer = None
        self.fail_once = None
        self.stream_prompt = False
        self.prompt_stream_open = False
        self.callback_ack = asyncio.Event()
        self.resume_prompt = asyncio.Event()
        self.resume_prompt.set()

    async def send_get(self, frame):
        data = packet(frame)
        self.get_writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        await self.get_writer.drain()

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method = lines[0].split()[0]
            headers = dict(line.lower().split(": ", 1) for line in lines[1:] if ": " in line)
            body = await reader.readexactly(int(headers.get("content-length", 0)))
            frame = json.loads(body) if body else {}
            action = frame.get(
                "method", "permission_ack" if frame.get("id") == "callback" else method
            )
            self.requests.append(action)
            if method == "GET":
                self.get_writer = writer
                writer.write(b"HTTP/1.1 200 OK\r\nAcp-Connection-Id: capacity-test\r\n"
                             b"Content-Type: text/event-stream\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                await reader.read()
                return
            if action == self.fail_once:
                self.fail_once = None
                writer.write(b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
            elif action == "session/cancel":
                writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
            elif action == "permission_ack":
                assert self.prompt_stream_open
                assert frame["result"]["outcome"]["outcome"] == "cancelled"
                self.callback_ack.set()
                writer.write(b"HTTP/1.1 202 Accepted\r\nContent-Length: 0\r\n"
                             b"Connection: close\r\n\r\n")
            elif method == "DELETE":
                writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            elif action == "session/prompt" and self.stream_prompt:
                self.prompt_stream_open = True
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                             b"Transfer-Encoding: chunked\r\n\r\n")
                await writer.drain()
                await self.send_get({"jsonrpc": "2.0", "id": "callback",
                                     "method": "session/request_permission"})
                await self.callback_ack.wait()
                await self.resume_prompt.wait()
                await self.send_get({"jsonrpc": "2.0", "method": "session/update",
                                     "params": {"text": "answer"}})
                await self.send_get({"jsonrpc": "2.0", "method": "session/endTurn",
                                     "params": {"sessionId": "s", "stopReason": "end_turn"}})
                data = packet({"jsonrpc": "2.0", "id": frame["id"],
                               "result": {"stopReason": "end_turn"}})
                writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n0\r\n\r\n")
                self.prompt_stream_open = False
            else:
                result = {"prepared": True}
                if action == "session/prompt":
                    result = {"stopReason": "end_turn"}
                    for event in [
                        {"jsonrpc": "2.0", "method": "session/update",
                         "params": {"text": "answer"}},
                        {"jsonrpc": "2.0", "method": "session/endTurn",
                         "params": {"sessionId": "s", "stopReason": "end_turn"}},
                    ]:
                        data = packet(event)
                        self.get_writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    await self.get_writer.drain()
                response = json.dumps({"jsonrpc": "2.0", "id": frame["id"], "result": result})
                data = response.encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(data)}\r\n".encode()
                             + b"Connection: close\r\n\r\n" + data)
            await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)


@pytest.fixture
async def server():
    state = Server()
    listener = await asyncio.start_server(state.handle, "127.0.0.1", 0)
    state.url = f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}/acp"
    try:
        yield state
    finally:
        listener.close()
        for writer in list(state.writers):
            writer.close()
        for task in list(state.tasks):
            task.cancel()
        await asyncio.gather(*state.tasks, return_exceptions=True)
        await listener.wait_closed()


def client():
    # 容量1是正式池容量100被100条GET占满的最小等价复现，使用真实HTTPTransport。
    return httpx.AsyncClient(limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
                             timeout=httpx.Timeout(connect=1, read=None, write=1, pool=.05),
                             trust_env=False)


async def test_shared_pool_is_starved_by_one_long_notification_connection(server):
    async with client() as shared:
        async with AcpConnection(shared, server.url, "synthetic", timeout=1) as connection:
            with pytest.raises(httpx.PoolTimeout):
                await connection.call("initialize", {})
            await connection.notify("session/cancel", {"sessionId": "s"})
            assert server.requests == ["GET"]
        assert connection._reader.done()
        assert server.requests == ["GET", "DELETE"]


async def test_separate_rpc_pool_initializes_prompts_and_cancels_with_get_pool_full(server):
    async with client() as notifications, client() as rpc:
        async with AcpConnection(notifications, server.url, "synthetic", timeout=1,
                                 post_client=rpc) as connection:
            assert await connection.call("initialize", {}) == {"prepared": True}
            frames = [frame async for frame in connection.rpc_frames(
                "session/prompt", {"sessionId": "s"})]
            assert frames[0]["params"]["text"] == "answer"
            assert frames[-1]["result"] == {"stopReason": "end_turn"}
            await connection.notify("session/cancel", {"sessionId": "s"})
        assert connection._reader.done()
        assert server.requests == [
            "GET", "initialize", "session/prompt", "session/cancel", "DELETE",
        ]
    assert notifications.is_closed and rpc.is_closed


@pytest.mark.parametrize("failure", ["initialize", "session/prompt"])
async def test_rpc_error_closes_get_and_next_connection_recovers_without_replaying_prompt(
    server, failure
):
    server.fail_once = failure
    async with client() as notifications, client() as rpc:
        async with AcpConnection(notifications, server.url, "synthetic", timeout=1,
                                 post_client=rpc) as failed:
            with pytest.raises(AcpHTTPError):
                if failure == "initialize":
                    await failed.call("initialize", {})
                else:
                    _ = [frame async for frame in failed.rpc_frames(
                        "session/prompt", {"sessionId": "s"})]
            await failed.notify("session/cancel", {"sessionId": "s"})
        assert failed._reader.done()
        async with AcpConnection(notifications, server.url, "synthetic", timeout=1,
                                 post_client=rpc) as recovered:
            assert await recovered.call("initialize", {}) == {"prepared": True}
            frames = [frame async for frame in recovered.rpc_frames(
                "session/prompt", {"sessionId": "s"})]
            assert frames[-1]["result"]["stopReason"] == "end_turn"
        assert recovered._reader.done()
    assert server.requests.count("session/prompt") == (1 if failure == "initialize" else 2)
    assert server.requests.count("DELETE") == 2


@pytest.mark.parametrize("separate_control", [False, True])
async def test_prompt_sse_at_rpc_capacity_needs_independent_permission_ack_pool(server,
                                                                             separate_control):
    server.stream_prompt = True
    async with client() as notifications, client() as rpc, client() as control:
        async with AcpConnection(notifications, server.url, "synthetic", timeout=1,
            post_client=rpc, control_client=control if separate_control else None) as connection:
            if not separate_control:
                with pytest.raises(httpx.PoolTimeout):
                    _ = [frame async for frame in connection.rpc_frames(
                        "session/prompt", {"sessionId": "s"})]
                assert not server.callback_ack.is_set()
            else:
                frames = [frame async for frame in connection.rpc_frames(
                    "session/prompt", {"sessionId": "s"})]
                assert server.callback_ack.is_set()
                assert frames[-1]["result"]["stopReason"] == "end_turn"
        assert connection._reader.done()
    assert server.requests.count("session/prompt") == 1
    assert server.requests.count("permission_ack") == int(separate_control)


async def test_control_cancel_and_delete_are_not_starved_by_active_get_and_prompt_sse(server):
    server.stream_prompt = True
    server.resume_prompt.clear()
    async with client() as notifications, client() as rpc, client() as control:
        async with AcpConnection(notifications, server.url, "synthetic", timeout=2,
                                 post_client=rpc, control_client=control) as connection:
            async def collect():
                return [frame async for frame in connection.rpc_frames(
                    "session/prompt", {"sessionId": "s"})]

            pending = asyncio.create_task(collect())
            try:
                await asyncio.wait_for(server.callback_ack.wait(), 1)
                assert server.prompt_stream_open
                await connection.notify("session/cancel", {"sessionId": "s"})
                response = await connection.control_client.delete(server.url)
                assert response.status_code == 204 and server.prompt_stream_open
                server.resume_prompt.set()
                assert (await pending)[-1]["result"]["stopReason"] == "end_turn"
            finally:
                pending.cancel()
                with suppress(asyncio.CancelledError):
                    await pending
        assert connection._reader.done()
    assert server.requests.count("session/prompt") == 1
    assert server.requests.count("session/cancel") == 1 and server.requests.count("DELETE") == 2
