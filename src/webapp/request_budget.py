"""推理入口的并发与输入缓冲预算，先排队再允许下游读取请求体。

只保存活跃计数和有界 FIFO 等待票，不缓存用户名、API Key 或请求正文。
两个限制分别可关闭；已放行的流持续占用名额直到正文结束或取消。
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass

from anyio import CancelScope
from starlette.responses import JSONResponse

from .limits import _body_limit

MIB = 1024 * 1024
POLL_SECONDS = 1.0
INFERENCE_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/responses",
        "/v1/messages",
        "/messages",
        "/v1/images/generations",
        "/v1/videos",
        "/api/playground/chat/completions",
    }
)


def inference_request(scope: dict) -> bool:
    if scope["type"] != "http" or scope.get("method") != "POST":
        return False
    path = scope.get("path", "").rstrip("/")
    return path in INFERENCE_PATHS or (
        path.startswith("/v1beta/models/")
        and path.endswith((":generateContent", ":streamGenerateContent"))
    )


def request_weight_mb(scope: dict) -> int:
    headers = scope.get("headers", ())
    lengths = [value.strip() for name, value in headers if name.lower() == b"content-length"]
    ambiguous = any(name.lower() == b"transfer-encoding" for name, _ in headers)
    if not ambiguous and len(lengths) == 1 and lengths[0].isdigit() and len(lengths[0]) <= 20:
        size = int(lengths[0])
    else:
        size = _body_limit(scope.get("path", ""))
    return max(1, (size + MIB - 1) // MIB)


class BudgetRejected(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(slots=True)
class _Reservation:
    budget: RequestBudget
    weight: int
    released: bool = False

    async def release(self) -> None:
        # StreamingResponse 的断连取消 scope 不能打断计数归还。
        with CancelScope(shield=True):
            async with self.budget._condition:
                if self.released:
                    return
                self.released = True
                self.budget.active_requests -= 1
                self.budget.active_input_mb -= self.weight
                self.budget._condition.notify_all()


class RequestBudget:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.active_requests = 0
        self.active_input_mb = 0
        self._condition = asyncio.Condition()
        self._waiters: deque[object] = deque()

    @property
    def queued_requests(self) -> int:
        return len(self._waiters)

    async def notify(self) -> None:
        """配置热更可主动唤醒；未接通知时等待者至多一秒重新读取配置。"""
        async with self._condition:
            self._condition.notify_all()

    def _input_limit(self, weight: int) -> int:
        limit = max(0, int(self.settings.resource_input_budget_mb))
        if limit and weight > limit:
            raise BudgetRejected(
                413,
                "resource_input_limit",
                f"request input reservation {weight} MiB exceeds budget {limit} MiB",
            )
        return limit

    def _fits(self, weight: int, input_limit: int) -> bool:
        active_limit = max(0, int(self.settings.resource_max_active_requests))
        return (not active_limit or self.active_requests < active_limit) and (
            not input_limit or self.active_input_mb + weight <= input_limit
        )

    def _reserve(self, weight: int) -> _Reservation:
        self.active_requests += 1
        self.active_input_mb += weight
        return _Reservation(self, weight)

    async def acquire(self, weight: int) -> _Reservation:
        async with self._condition:
            input_limit = self._input_limit(weight)
            if not self._waiters and self._fits(weight, input_limit):
                return self._reserve(weight)
            maximum = max(0, int(self.settings.resource_max_queued_requests))
            if len(self._waiters) >= maximum:
                raise BudgetRejected(503, "resource_queue_full", "server inference queue is full")
            ticket = object()
            self._waiters.append(ticket)
            loop = asyncio.get_running_loop()
            started = loop.time()
            try:
                while True:
                    input_limit = self._input_limit(weight)
                    timeout = max(0.0, float(self.settings.resource_queue_timeout_seconds))
                    remaining = started + timeout - loop.time()
                    if remaining <= 0:
                        raise BudgetRejected(
                            503, "resource_queue_timeout", "server inference queue wait timed out"
                        )
                    if self._waiters[0] is ticket and self._fits(weight, input_limit):
                        return self._reserve(weight)
                    try:
                        async with asyncio.timeout(min(POLL_SECONDS, remaining)):
                            await self._condition.wait()
                    except TimeoutError:
                        continue
            finally:
                self._waiters.remove(ticket)
                self._condition.notify_all()


class RequestBudgetMiddleware:
    def __init__(self, app, *, settings) -> None:
        self.app = app
        self.budget = RequestBudget(settings)

    async def __call__(self, scope, receive, send) -> None:
        if not inference_request(scope):
            await self.app(scope, receive, send)
            return
        try:
            reservation = await self.budget.acquire(request_weight_mb(scope))
        except BudgetRejected as error:
            response = JSONResponse(
                status_code=error.status,
                content={
                    "error": {
                        "type": "api_error",
                        "code": error.code,
                        "message": str(error),
                        "status": error.status,
                    }
                },
                headers={"Retry-After": "1"} if error.status == 503 else {},
            )
            await response(scope, receive, send)
            return

        async def budget_send(message):
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                await reservation.release()

        try:
            await self.app(scope, receive, budget_send)
        finally:
            await reservation.release()
