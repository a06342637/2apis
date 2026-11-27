"""连接失败才换官方连接地址，保留 TLS/Host/请求体和原账号域名。"""
import ssl

import httpx
import pytest

from src.provider.codebuddy import routing
from src.provider.codebuddy.client import CodeBuddyClient
from src.provider.codebuddy.oauth import CodeBuddyOAuth


class Wire(httpx.AsyncBaseTransport):
    def __init__(self, failures=()):
        self.calls = []
        self.failures = list(failures)
        self.closed = False

    async def handle_async_request(self, request):
        self.calls.append(request)
        failure = self.failures.pop(0) if self.failures else None
        if failure:
            raise failure
        body = await request.aread()
        return httpx.Response(200, content=body or b"ok")

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("url", ["http://www.codebuddy.ai/x", "https://copilot.tencent.com/x",
                                 "https://www.codebuddy.ai:8443/x"])
async def test_noncanonical_endpoints_are_never_redirected(url):
    wire = Wire()
    wrapped = routing.IntlRoutingTransport(wire)
    request = httpx.Request("GET", url)
    response = await wrapped.handle_async_request(request)
    assert wire.calls == [request] and "codebuddy_connect_alias" not in response.extensions


async def test_alias_preserves_body_host_sni_and_cookie_origin_and_caches_connection_choice():
    wire = Wire([httpx.ConnectTimeout("synthetic DNS failure")])
    clock = [100.0]
    wrapped = routing.IntlRoutingTransport(wire, now=lambda: clock[0])
    async with httpx.AsyncClient(transport=wrapped, timeout=httpx.Timeout(30)) as client:
        response = await client.post(
            "https://www.codebuddy.ai/v2/chat?x=%2F", content=b"private prompt",
            headers={"X-Domain": "www.codebuddy.ai", "Authorization": "Bearer qa"})
        assert response.content == b"private prompt"
        original, alias = wire.calls
        assert original.url.host == "www.codebuddy.ai" and alias.url.host == "api.codebuddy.ai"
        assert alias.headers["Host"] == alias.headers["X-Domain"] == "www.codebuddy.ai"
        assert alias.headers["Authorization"] == "Bearer qa"
        assert alias.extensions["sni_hostname"] == "www.codebuddy.ai"
        assert alias.url.query == original.url.query == b"x=%2F"
        assert response.request.url.host == "www.codebuddy.ai"
        assert original.extensions["timeout"]["connect"] == 2
        assert response.extensions["codebuddy_connect_alias"]
        await client.get("https://www.codebuddy.ai/v2/plugin/auth/token")
        assert len(wire.calls) == 3 and wire.calls[-1].url.host == "api.codebuddy.ai"
        clock[0] = 401
        response = await client.get("https://www.codebuddy.ai/v3/config")
        assert wire.calls[-1].url.host == "www.codebuddy.ai"
        assert not response.extensions["codebuddy_connect_alias"]
    assert wire.closed


@pytest.mark.parametrize("failure", [httpx.ReadTimeout("read"), httpx.WriteTimeout("write"),
                                     httpx.ProxyError("proxy")])
async def test_uncertain_or_proxy_failure_never_replays_prompt(failure):
    wire = Wire([failure])
    wrapped = routing.IntlRoutingTransport(wire)
    async with httpx.AsyncClient(transport=wrapped) as client:
        with pytest.raises(type(failure)):
            await client.post("https://www.codebuddy.ai/v2/chat", content="one prompt")
    assert len(wire.calls) == 1


async def test_both_connections_fail_without_fake_response_and_cached_alias_can_recover_origin():
    wire = Wire([httpx.ConnectError("one"), httpx.ConnectError("two")])
    wrapped = routing.IntlRoutingTransport(wire, now=lambda: 100)
    async with httpx.AsyncClient(transport=wrapped) as client:
        with pytest.raises(httpx.ConnectError, match="two"):
            await client.get("https://www.codebuddy.ai/x")
        assert wrapped.alias_until == 0
        wrapped.alias_until = 500
        wire.failures = [httpx.ConnectError("alias unavailable")]
        response = await client.get("https://www.codebuddy.ai/x", timeout=httpx.Timeout(2))
        assert response.status_code == 200 and not response.extensions["codebuddy_connect_alias"]
        assert wire.calls[-1].extensions["timeout"]["connect"] == 2
        assert wire.calls[-1].url.host == "www.codebuddy.ai"


async def test_missing_connect_timeout_is_bounded_and_http_status_is_not_retried():
    wire = Wire()
    wrapped = routing.IntlRoutingTransport(wire)
    await wrapped.handle_async_request(httpx.Request("GET", "https://www.codebuddy.ai/x"))
    assert wire.calls[0].extensions["timeout"]["connect"] == 2
    await wrapped.aclose()


async def test_factory_keeps_tls_verification_and_explicit_proxy_and_client_pools(monkeypatch):
    client = routing.build_codebuddy_client(timeout=httpx.Timeout(30))
    assert isinstance(client._transport, routing.IntlRoutingTransport)
    assert client._transport.transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
    await client.aclose()
    calls = []
    original = routing.build_client
    def capture(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(routing, "build_client", capture)
    proxy_client = routing.build_codebuddy_client(timeout=httpx.Timeout(30), proxy="http://proxy.example:8080")
    assert calls[-1]["proxy"] == "http://proxy.example:8080" and "transport" not in calls[-1]
    await proxy_client.aclose()
    cb = CodeBuddyClient(endpoint="https://www.codebuddy.ai")
    assert isinstance(cb._short._transport, routing.IntlRoutingTransport)
    assert isinstance(cb._stream._transport, routing.IntlRoutingTransport)
    await cb.aclose()


async def test_oauth_keeps_canonical_auth_url_and_state_when_connection_used_alias():
    async def handler(_request):
        return httpx.Response(200, extensions={"codebuddy_connect_alias": True}, json={
            "code": 0, "data": {"state": "remote", "authUrl": "https://www.codebuddy.ai/login?state=remote"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        oauth = CodeBuddyOAuth("https://www.codebuddy.ai", client=client)
        session = await oauth.start("alice")
        assert session.auth_url == "https://www.codebuddy.ai/login?state=remote"
        assert oauth.store.upstream_state(session.state, "alice") == "remote"
