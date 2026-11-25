"""Miora ACP 的双通道 HTTP 传输：GET 通知流 + POST JSON-RPC。

控制请求通常返回 202，实际结果在 GET 流；prompt 的结果也可能通过
POST SSE 返回。两个流汇入同一有界队列，消费端按请求 id 匹配结果。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Any

import httpx

from ...engine.sse import iter_frames
from ..base import ErrKind, UpstreamHTTPError, UpstreamProtocolViolation, read_body_bounded
from .events import classify_status


class AcpHTTPError(UpstreamHTTPError):
    """运行时短票失败不作为账号长期登录票失效处理。"""

    @staticmethod
    def classify_status(status: int, body: bytes) -> ErrKind:
        return ErrKind.SOFT if status == 401 else classify_status(status, body)


async def jsonrpc_frames(response: httpx.Response) -> AsyncIterator[dict]:
    async for frame in iter_frames(response.aiter_bytes()):
        if frame.event not in ("", "message"):
            continue
        try:
            value = json.loads(frame.data)
        except ValueError as error:
            raise UpstreamProtocolViolation("Miora ACP 返回了无效 JSON") from error
        # GET 流也会给连接控制/心跳 JSON；官方传输仅派发 JSON-RPC 帧。
        if not isinstance(value, dict) or "jsonrpc" not in value:
            continue
        if value["jsonrpc"] == "":
            continue  # Miora session/load 心跳与快照控制通知使用空版本字段。
        if value["jsonrpc"] != "2.0":
            raise UpstreamProtocolViolation("Miora ACP 返回了无效 JSON-RPC 帧")
        yield value


class AcpConnection:
    """单请求连接；关闭只释放连接，不删除项目、运行时或会话。"""

    def __init__(self, client: httpx.AsyncClient, endpoint: str, token: str,
                 *, timeout: float = 300) -> None:
        self.client = client
        self.endpoint = endpoint
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout
        self._queue: asyncio.Queue[dict | Exception] = asyncio.Queue(maxsize=256)
        self._next_id = 0
        self._context: Any = None
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> AcpConnection:
        self._context = self.client.stream(
            "GET", self.endpoint, headers={**self.headers, "Accept": "text/event-stream"})
        response = await self._context.__aenter__()
        try:
            if response.status_code >= 400:
                raise AcpHTTPError(response.status_code, await read_body_bounded(response))
            connection = response.headers.get("Acp-Connection-Id")
            if not connection:
                raise UpstreamProtocolViolation("Miora ACP 缺少连接标识")
            self.headers["Acp-Connection-Id"] = connection
            self._reader = asyncio.create_task(self._read(response))
            return self
        except Exception:
            await self._context.__aexit__(None, None, None)
            raise

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader
        if self._context is not None:
            await self._context.__aexit__(exc_type, exc, traceback)
        if "Acp-Connection-Id" in self.headers:
            with suppress(httpx.HTTPError):
                await self.client.delete(self.endpoint, headers=self.headers, timeout=5)

    async def _read(self, response: httpx.Response) -> None:
        try:
            async for frame in jsonrpc_frames(response):
                await self._queue.put(frame)
            await self._queue.put(UpstreamProtocolViolation("Miora ACP 通知流提前结束"))
        except Exception as error:
            await self._queue.put(error)

    async def _post(self, request: dict) -> None:
        try:
            async with self.client.stream(
                    "POST", self.endpoint, headers={
                        **self.headers, "Accept": "application/json, text/event-stream"},
                    json=request) as response:
                if response.status_code >= 400:
                    raise AcpHTTPError(response.status_code, await read_body_bounded(response))
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    async for frame in jsonrpc_frames(response):
                        await self._queue.put(frame)
                elif "application/json" in content_type:
                    raw = await read_body_bounded(response)
                    try:
                        frame = json.loads(raw)
                    except ValueError as error:
                        raise UpstreamProtocolViolation("Miora ACP 返回了无效 JSON") from error
                    if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                        raise UpstreamProtocolViolation("Miora ACP 返回了无效响应")
                    await self._queue.put(frame)
                elif response.status_code != 202:
                    raise UpstreamProtocolViolation("Miora ACP 返回了未知响应类型")
        except Exception as error:
            await self._queue.put(error)

    async def _reject_request(self, frame: dict) -> None:
        """从不授权云端工具代替客户端执行；未知扩展按 JSON-RPC 拒绝。"""
        result = {"outcome": {"outcome": "cancelled"}}
        response = {"jsonrpc": "2.0", "id": frame["id"], "result": result}
        await self._post(response)

    async def notify(self, method: str, params: dict) -> None:
        await self._post({"jsonrpc": "2.0", "method": method, "params": params})

    async def rpc_frames(self, method: str, params: dict) -> AsyncIterator[dict]:
        self._next_id += 1
        request_id = self._next_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        task = asyncio.create_task(self._post(request))
        result: dict | None = None
        ended = False
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    frame = await self._queue.get()
                    if isinstance(frame, Exception):
                        raise frame
                    if "method" in frame and "id" in frame:
                        await self._reject_request(frame)
                        continue
                    if frame.get("id") == request_id:
                        result = frame
                        if method != "session/prompt" or "error" in frame:
                            yield frame
                            return
                    else:
                        if frame.get("method") == "session/endTurn":
                            end_params = frame.get("params")
                            if (isinstance(end_params, dict)
                                    and end_params.get("sessionId") == params.get("sessionId")):
                                ended = True
                        yield frame
                    # GET endTurn 是内容已全部送达的栅栏；POST result 先到也不丢尾帧。
                    if result is not None and ended:
                        yield result
                        return
        except TimeoutError as error:
            raise UpstreamProtocolViolation("Miora ACP 请求超时") from error
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def call(self, method: str, params: dict) -> dict:
        result: dict | None = None
        async for frame in self.rpc_frames(method, params):
            if "error" in frame:
                raise UpstreamProtocolViolation("Miora ACP 控制请求失败")
            value = frame.get("result")
            if isinstance(value, dict):
                result = value
        if result is not None:
            return result
        # rpc_frames 只会返回包含 result/error 的终态；按协议约束不可达。
        raise UpstreamProtocolViolation("Miora ACP 控制请求缺少结果")
