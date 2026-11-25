"""媒体自动工具装配仍保持凭证、模型映射与取消时同步释放上游流。"""

from __future__ import annotations

from contextlib import aclosing

from src.provider.base import Event, EventKind, Model
from src.provider.miora import MioraProvider
from src.provider.miora.client import MioraHTTPError
from src.tasks.pacer import Pacer


async def test_media_auto_hook_receives_real_stream_factory_and_closes_before_pacer_release():
    class Client:
        def __init__(self):
            self.stream_closed = False
            self.closed = False

        async def stream_chat(self, credential, payload, model):
            assert credential.uid == "u" and model == "text-model"
            assert payload["messages"][0]["content"] == "code only"
            try:
                yield Event(kind=EventKind.CONTENT, content="wired")
                yield Event(kind=EventKind.FINISH, finish_reason="stop")
            finally:
                self.stream_closed = True

        async def aclose(self):
            self.closed = True

    class Auto:
        def __init__(self):
            self.called = False
            self.closed = False

        async def stream_chat(self, credential, payload, model, factory):
            self.called = True
            try:
                async with aclosing(factory(credential, payload, model)) as inner:
                    async for event in inner:
                        yield event
            finally:
                assert client.stream_closed
                self.closed = True

    client, auto = Client(), Auto()
    provider = MioraProvider(
        client=client, media_auto=auto, pacer=Pacer(0, 0, allow_concurrent=True, max_concurrency=1)
    )
    stream = provider.stream_chat(
        {"authToken": "test", "refreshToken": "r", "uid": "u"},
        {"messages": [{"role": "user", "content": "code only"}]},
        "text-model",
    )
    assert (await anext(stream)).content == "wired"
    assert auto.called and provider.pacer._inflight == {"u": 1}
    await stream.aclose()
    assert client.stream_closed and auto.closed and provider.pacer._inflight == {}
    await provider.aclose()
    assert client.closed


async def test_media_source_hook_reports_partial_directory_and_recovers(monkeypatch):
    class Client:
        async def fetch_models(self, credential):
            return [Model(id="text")]

    provider = MioraProvider(client=Client())
    assert provider.model_list_source() == (False, "")

    async def failed(credential):
        raise MioraHTTPError(503, b"temporary directory failure")

    monkeypatch.setattr(provider.media_client, "catalog", failed)
    assert [model.id for model in await provider.list_models({"authToken": "test"})] == ["text"]
    degraded, reason = provider.model_list_source()
    assert degraded and "保留文本" in reason

    async def recovered(credential):
        return [Model(id="image", gateway_endpoints=("/v1/images/generations",))]

    monkeypatch.setattr(provider.media_client, "catalog", recovered)
    assert len(await provider.list_models({"authToken": "test"})) == 2
    assert provider.model_list_source() == (False, "")
