"""国际站 DNS 故障的官方连接别名；逻辑域名、Host、TLS 身份始终保持原站。"""
from __future__ import annotations

import time

import httpx

from ..proxy import UPSTREAM_LIMITS, build_client

ORIGIN_HOST = "www.codebuddy.ai"
CONNECT_HOST = "api.codebuddy.ai"
ALIAS_CACHE_SECONDS = 300


class IntlRoutingTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport: httpx.AsyncBaseTransport, now=time.monotonic):
        self.transport = transport
        self.now = now
        self.alias_until = 0.0

    async def _send(self, request: httpx.Request, alias: bool) -> httpx.Response:
        extensions = dict(request.extensions)
        timeout = dict(extensions.get("timeout", {}))
        connect = timeout.get("connect")
        # 原入口的短连接探测给 5 秒模型探活留出切换入口的时间。
        limit = 8.0 if alias else 2.0
        timeout["connect"] = limit if connect is None else min(connect, limit)
        extensions["timeout"] = timeout
        headers = request.headers.copy()
        url = request.url
        if alias:
            url = url.copy_with(host=CONNECT_HOST)
            headers["Host"] = request.url.netloc.decode("ascii")
            extensions["sni_hostname"] = ORIGIN_HOST
        physical = httpx.Request(request.method, url, headers=headers,
                                 stream=request.stream, extensions=extensions)
        response = await self.transport.handle_async_request(physical)
        response.extensions["codebuddy_connect_alias"] = alias
        return response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if (request.url.scheme != "https" or request.url.host != ORIGIN_HOST
                or request.url.port not in (None, 443)):
            return await self.transport.handle_async_request(request)
        alias = self.alias_until > self.now()
        try:
            response = await self._send(request, alias)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # 仅 TCP/TLS 尚未连接的失败允许换连接地址；读/写失败不重放请求。
            self.alias_until = 0.0
            alias = not alias
            response = await self._send(request, alias)
        if alias:
            self.alias_until = self.now() + ALIAS_CACHE_SECONDS
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()


def build_codebuddy_client(*, timeout: httpx.Timeout,
                           proxy: str | None = None) -> httpx.AsyncClient:
    # 显式代理继续使用原出口，不把代理的 DNS/CONNECT 行为改为直连别名。
    if proxy is not None:
        return build_client(timeout=timeout, proxy=proxy)
    transport = httpx.AsyncHTTPTransport(trust_env=False, limits=UPSTREAM_LIMITS)
    return build_client(timeout=timeout, proxy=proxy, transport=IntlRoutingTransport(transport))
