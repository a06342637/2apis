"""登录链接申请超时不登记 state；保持国内/国际端点和传输错误语义。"""
import asyncio

import httpx
import pytest

from src.provider.codebuddy import oauth as module
from src.provider.codebuddy.oauth import CodeBuddyOAuth


@pytest.mark.parametrize("endpoint", ["https://copilot.tencent.com", "https://www.codebuddy.ai"])
async def test_auth_start_has_short_connect_timeout_and_registers_only_complete_response(endpoint):
    async def handler(request):
        assert request.extensions["timeout"]["connect"] == 8
        assert request.extensions["timeout"]["read"] == 15
        assert request.headers["X-Domain"] == request.url.host
        return httpx.Response(200, json={"code": 0, "data": {
            "authUrl": "https://login.example/", "state": "remote-one"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth = CodeBuddyOAuth(endpoint, client=client)
        session = await oauth.start("alice")
        assert oauth.store.upstream_state(session.state, "alice") == "remote-one"


async def test_auth_start_total_deadline_cancels_pending_transport_and_keeps_store_empty(
        monkeypatch):
    monkeypatch.setattr(module, "AUTH_START_TIMEOUT_SECONDS", 0.01)
    cancelled = []
    async def handler(_request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth = CodeBuddyOAuth("https://www.codebuddy.ai", client=client)
        with pytest.raises(httpx.ReadTimeout, match="限定时间"):
            await oauth.start("alice")
        assert cancelled == [True] and oauth.store._entries == {}


async def test_connection_failure_stays_retryable_transport_error_without_fake_session():
    async def handler(request):
        raise httpx.ConnectTimeout("synthetic-private-network-detail", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth = CodeBuddyOAuth("https://www.codebuddy.ai", client=client)
        with pytest.raises(httpx.ConnectTimeout):
            await oauth.start("alice")
        assert oauth.store._entries == {}
