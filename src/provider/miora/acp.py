"""Miora ACP 的双通道 HTTP 传输：GET 通知流 + POST JSON-RPC。

控制请求通常返回 202，实际结果在 GET 流；prompt 的结果也可能通过
POST SSE 返回。两个流汇入同一有界队列，消费端按请求 id 匹配结果。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx
from anyio import CancelScope

from ...engine.sse import iter_frames
from ..base import (
    ErrKind,
    UpstreamHTTPError,
    UpstreamProtocolViolation,
    read_body_bounded,
    retry_hints,
)
from .events import classify_status


class AcpHTTPError(UpstreamHTTPError):
    """运行时短票失败不作为账号长期登录票失效处理。"""

    @staticmethod
    def classify_status(status: int, body: bytes) -> ErrKind:
        return ErrKind.SOFT if status == 401 else classify_status(status, body)


@dataclass(frozen=True, slots=True)
class _PromptPostEOF:
    """POST prompt SSE 已结束，但该流没有给出当前请求的 RPC 终态。"""

    request_id: int


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
                 *, timeout: float = 300, post_client: httpx.AsyncClient | None = None,
                 control_client: httpx.AsyncClient | None = None) -> None:
        self.client = client
        self.post_client = post_client if post_client is not None else client
        self.control_client = control_client if control_client is not None else self.post_client
        self.endpoint = endpoint
        self.headers = {"Authorization": f"Bearer {token}"}
        self.timeout = timeout
        # 单帧上限仍沿用 SSE 的8MiB；4帧队列施加背压，避免低内存机器堆积大缓冲。
        self._queue: asyncio.Queue[dict | Exception | _PromptPostEOF] = asyncio.Queue(maxsize=4)
        self._next_id = 0
        self._active_method = ""
        self._context: Any = None
        self._reader: asyncio.Task | None = None

    async def __aenter__(self) -> AcpConnection:
        self._context = self.client.stream(
            "GET", self.endpoint, headers={**self.headers, "Accept": "text/event-stream"})
        response = None
        entered = False
        try:
            # 流式池 read=None；建链阶段须另设截止时间，不能无限等待响应头。
            async with asyncio.timeout(min(self.timeout, 30)):
                response = await self._context.__aenter__()
                if response.status_code >= 400:
                    body = await read_body_bounded(response)
                    raise AcpHTTPError(response.status_code, body,
                                       **retry_hints(response.headers, body=body))
                connection = response.headers.get("Acp-Connection-Id")
                if not connection:
                    raise UpstreamProtocolViolation("Miora ACP 缺少连接标识")
                self.headers["Acp-Connection-Id"] = connection
                self._reader = asyncio.create_task(self._read(response))
            entered = True
            return self
        except TimeoutError as error:
            raise UpstreamProtocolViolation("Miora ACP 建链超时") from error
        finally:
            if not entered and response is not None:
                with CancelScope(shield=True):
                    await self._context.__aexit__(None, None, None)

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        # ASGI scope 会重复取消 await；先关闭本地流和远端连接再归还账号名额。
        with CancelScope(shield=True):
            if self._reader is not None:
                self._reader.cancel()
                with suppress(asyncio.CancelledError):
                    await self._reader
            if self._context is not None:
                await self._context.__aexit__(exc_type, exc, traceback)
            if "Acp-Connection-Id" in self.headers:
                with suppress(httpx.HTTPError):
                    await self.control_client.delete(self.endpoint, headers=self.headers, timeout=5)

    async def _read(self, response: httpx.Response) -> None:
        try:
            async for frame in jsonrpc_frames(response):
                await self._publish(frame)
            await self._queue.put(UpstreamProtocolViolation("Miora ACP 通知流提前结束"))
        except Exception as error:
            await self._queue.put(error)

    async def _publish(self, frame: dict) -> None:
        # session/load 等准备期快照不供 API 消费。丢掉无id噪声，保留GET控制结果及
        # 服务端回调；prompt阶段按原顺序保留全部通知，慢客户端会反压两条流。
        if "id" in frame or self._active_method == "session/prompt":
            await self._queue.put(frame)

    async def _post(self, request: dict) -> None:
        expects_result = "id" in request and "method" in request
        client = self.post_client if expects_result else self.control_client
        try:
            async with client.stream(
                    "POST", self.endpoint, headers={
                        **self.headers, "Accept": "application/json, text/event-stream"},
                    json=request) as response:
                if response.status_code >= 400:
                    body = await read_body_bounded(response)
                    raise AcpHTTPError(response.status_code, body,
                                       **retry_hints(response.headers, body=body))
                content_type = response.headers.get("Content-Type", "")
                if "text/event-stream" in content_type:
                    received_result = False
                    try:
                        async for frame in jsonrpc_frames(response):
                            if (frame.get("id") == request.get("id")
                                    and ("result" in frame or "error" in frame)):
                                received_result = True
                            if expects_result:
                                await self._publish(frame)
                    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ReadTimeout):
                        # 200 SSE 的 POST 读取中断与缺少终态的 EOF 同义；GET 决定能否恢复。
                        # 已派发的 RPC error 不会消失；非 prompt 控制请求仍上抛原异常。
                        if request.get("method") != "session/prompt":
                            raise
                    if request.get("method") == "session/prompt" and not received_result:
                        await self._queue.put(_PromptPostEOF(request["id"]))
                elif "application/json" in content_type:
                    raw = await read_body_bounded(response)
                    try:
                        frame = json.loads(raw)
                    except ValueError as error:
                        raise UpstreamProtocolViolation("Miora ACP 返回了无效 JSON") from error
                    if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                        raise UpstreamProtocolViolation("Miora ACP 返回了无效响应")
                    if expects_result:
                        await self._publish(frame)
                elif response.status_code != 202:
                    raise UpstreamProtocolViolation("Miora ACP 返回了未知响应类型")
        except Exception as error:
            if expects_result:
                await self._queue.put(error)
            else:
                # 回调响应和取消通知的ACK不是新的RPC结果；消费端同步回复回调时
                # 不能往自己正在等待的满队列入队。回复错误由调用者直接处理。
                raise

    async def _reject_request(self, frame: dict) -> None:
        """从不授权云端工具代替客户端执行；未知扩展按 JSON-RPC 拒绝。"""
        result = {"outcome": {"outcome": "cancelled"}}
        response = {"jsonrpc": "2.0", "id": frame["id"], "result": result}
        await self._post(response)

    async def notify(self, method: str, params: dict) -> None:
        try:
            # 取消是尽力通知，不能让失败请求的清理继续无限占住账号锁。
            async with asyncio.timeout(min(self.timeout, 5)):
                await self._post({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception:  # noqa: BLE001 - 尽力取消：网络/协议失败均不能拖住清理
            pass

    async def rpc_frames(self, method: str, params: dict) -> AsyncIterator[dict]:
        self._next_id += 1
        request_id = self._next_id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self._active_method = method
        task = asyncio.create_task(self._post(request))
        result: dict | None = None
        end_params: dict | None = None
        post_eof = False
        try:
            async with asyncio.timeout(self.timeout):
                while True:
                    frame = await self._queue.get()
                    if isinstance(frame, Exception):
                        raise frame
                    if isinstance(frame, _PromptPostEOF):
                        if frame.request_id == request_id:
                            post_eof = True
                    else:
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
                                candidate = frame.get("params")
                                if (isinstance(candidate, dict)
                                        and candidate.get("sessionId") == params.get("sessionId")):
                                    end_params = candidate
                                    reason = candidate.get("stopReason")
                                    if reason is not None and reason not in (
                                        "end_turn", "stop", "max_tokens", "length"
                                    ):
                                        # GET 明确宣告失败时立即结束；不能等待或借用
                                        # POST 的成功 result 将本轮部分回答收成成功。
                                        raise UpstreamProtocolViolation("Miora ACP 对话未成功结束")
                            yield frame
                    # GET endTurn 是内容已全部送达的栅栏；POST result 先到也不丢尾帧。
                    if result is not None and end_params is not None:
                        yield result
                        return
                    if (post_eof and end_params is not None and self._queue.empty()):
                        # 与网页客户端相同：POST 断尾可由完整、成功的 GET endTurn 收尾。
                        # 先消费已有队列，确保不吞掉已抵达的 RPC error。
                        if end_params.get("stopReason") != "end_turn":
                            raise UpstreamProtocolViolation("Miora ACP 对话未成功结束")
                        yield {"jsonrpc": "2.0", "id": request_id,
                               "result": {"stopReason": "end_turn"}}
                        return
        except TimeoutError as error:
            raise UpstreamProtocolViolation("Miora ACP 请求超时") from error
        finally:
            self._active_method = ""
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
