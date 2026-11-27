"""Miora RPC池接线、代理粘性与统一关闭，显式测试client保持兼容。"""

import httpx

from src.provider.miora.client import MioraClient
from src.request_context import credential_scope


async def test_injected_stream_client_keeps_rpc_transport_compatible():
    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(202)))
    client = MioraClient(stream_client=injected)
    assert client._control is client._rpc is client._stream is injected
    await client.aclose()
    assert injected.is_closed


async def test_notification_and_rpc_pools_are_distinct_per_proxy_and_closed_together(monkeypatch):
    created = []

    def build(*, timeout, proxy):
        pool = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(202)))
        created.append((proxy, pool))
        return pool

    proxies = {"account-one": "http://first-proxy", "account-two": "http://second-proxy"}
    monkeypatch.setattr("src.provider.lazy_clients.build_client", build)
    monkeypatch.setattr("src.provider.lazy_clients.credential_proxy", proxies.get)
    client = MioraClient()
    for account, proxy in proxies.items():
        with credential_scope(account):
            stream, rpc, control = client._stream, client._rpc, client._control
            assert stream is not rpc
            assert control is not stream and control is not rpc
            assert client._stream is stream and client._rpc is rpc
            assert client._control is control
            assert client._pools[("stream", proxy)] is stream
            assert client._pools[("miora-rpc", proxy)] is rpc
            assert client._pools[("miora-control", proxy)] is control
    assert len(created) == 6
    await client.aclose()
    assert all(pool.is_closed for _proxy, pool in created)
