from __future__ import annotations

import httpx
import pytest

from src.compat.openai.request import InvalidRequest
from src.provider.base import ErrKind, Event, EventKind, Model, Quota, UpstreamProtocolViolation
from src.provider.miora import MioraProvider
from src.provider.miora.client import MioraHTTPError
from src.provider.miora.credential import MioraCredential
from src.tasks.pacer import Pacer

pytestmark = pytest.mark.real_upstream("miora_media")


class Client:
    def __init__(self):
        self.seen = []
        self.closed = False

    async def fetch_models(self, credential):
        self.seen.append(credential)
        return [Model(id="hy4flash-free")]

    async def _request(self, credential, method, path, **kwargs):
        # 由媒体目录调用；本用例不调用 Film 生成接口。
        assert method == "GET" and path.endswith("/media-models")
        return {
            "data": [
                {
                    "modelId": "gem-3.1",
                    "label": "Nano Banana 2",
                    "contentType": "image",
                    "visible": True,
                    "available": True,
                    "capabilities": {"supportedTools": ["text_to_image"]},
                }
            ]
        }

    async def probe_quota(self, credential):
        self.seen.append(credential)
        return Quota(remaining=250, total=250)

    async def refresh_token(self, credential):
        self.seen.append(credential)
        return {"access_token": "new", "refresh_token": "new-refresh"}

    async def stream_chat(self, credential, payload, model):
        self.seen.append((credential, payload, model))
        yield Event(kind=EventKind.CONTENT, content="hello")
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    async def aclose(self):
        self.closed = True


async def test_provider_lifecycle():
    client = Client()
    provider = MioraProvider(client=client)
    raw = {"authToken": "token", "refreshToken": "r", "uid": "u", "expires_at": 2000}
    normalized = provider.import_credential(raw)
    assert provider.id == "miora"
    assert normalized["access_token"] == "token"
    assert provider.credential_from(raw) == MioraCredential("token", "r", "u", 2000)
    assert provider.credential_expires_at(raw) == 2000
    assert provider.login_identity(raw) == "u"
    assert provider.classify(401, b"") == ErrKind.DEAD
    models = await provider.list_models(raw)
    assert [model.id for model in models] == ["hy4flash-free", "gem-3.1"]
    assert models[1].gateway_endpoints == ("/v1/images/generations",)
    assert models[1].output_modalities == ("image",)
    assert provider.media_client.client is client
    assert (await provider.probe_quota(raw)).remaining == 250
    assert (await provider.refresh(raw))["access_token"] == "new"
    payload = {"messages": [{"role": "user", "content": "hello"}]}
    events = [event async for event in provider.stream_chat(raw, payload, "hy4flash-free")]
    assert [event.kind for event in events] == [EventKind.CONTENT, EventKind.FINISH]
    assert client.seen[-1] == (provider.credential_from(raw), payload, "hy4flash-free")
    await provider.aclose()
    assert client.closed


async def test_missing_credential_and_pacing():
    provider = MioraProvider(client=Client())
    with pytest.raises(InvalidRequest, match="authToken"):
        [event async for event in provider.stream_chat({}, {}, "hy4flash-free")]
    provider.pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1)
    assert (
        len(
            [
                event
                async for event in provider.stream_chat({"access_token": "t"}, {}, "hy4flash-free")
            ]
        )
        == 2
    )
    assert provider.pacer._inflight == {}


async def test_default_client():
    provider = MioraProvider()
    await provider.aclose()


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("directory unavailable"),
        MioraHTTPError(500, b"failure"),
        UpstreamProtocolViolation("bad directory"),
    ],
)
async def test_media_catalog_failure_preserves_text_and_clears_degraded_on_recovery(
    monkeypatch,
    caplog,
    failure,
):
    provider = MioraProvider(client=Client())

    async def fail(credential):
        raise failure

    monkeypatch.setattr(provider.media_client, "catalog", fail)
    assert [item.id for item in await provider.list_models({"authToken": "test"})] == [
        "hy4flash-free"
    ]
    assert provider.media_catalog_error and "保留文本" in provider.media_catalog_error
    assert "保留文本" in caplog.text

    async def recover(credential):
        return [Model(id="gem-3.1", gateway_endpoints=("/v1/images/generations",))]

    monkeypatch.setattr(provider.media_client, "catalog", recover)
    assert len(await provider.list_models({"authToken": "test"})) == 2
    assert provider.media_catalog_error is None


async def test_outer_close_synchronously_cancels_client_before_releasing_slot():
    order = []

    class ClosingClient(Client):
        async def stream_chat(self, credential, payload, model):
            try:
                yield Event(kind=EventKind.CONTENT, content="first")
            finally:
                order.append("client_cancelled")

    class RecordingPacer:
        async def wait_turn(self, key):
            order.append("slot_reserved")

        def release(self, key):
            assert order[-1] == "client_cancelled"
            order.append("slot_released")

    provider = MioraProvider(client=ClosingClient(), pacer=RecordingPacer())
    stream = provider.stream_chat({"access_token": "t"}, {}, "hy4flash-free")
    assert (await anext(stream)).content == "first"
    assert order == ["slot_reserved"]
    await stream.aclose()
    assert order == ["slot_reserved", "client_cancelled", "slot_released"]
