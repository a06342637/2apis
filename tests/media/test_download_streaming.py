import os

import httpx
import pytest
from anyio import CancelScope, sleep

from src.media import download as mod
from src.media.download import MediaDownloader
from src.provider.base import UpstreamProtocolViolation
from tests.media.test_download import HOST, URL
from tests.media.test_download import private_files as _private_files

private_files = _private_files


@pytest.mark.parametrize("encoding", ["gzip", "br", "identity,gzip", " GZip "])
async def test_compressed_responses_are_rejected_before_body_read(
    tmp_path, private_files, encoding
):
    class Unreadable(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("compressed body must not be read or decompressed")
            yield b""  # 异步迭代器接口由 HTTPX 调用；本用例应在读取前拒绝。

    target = tmp_path / "asset"
    target.write_bytes(b"old")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png", "content-encoding": encoding},
                stream=Unreadable(),
            )
        )
    ) as client:
        with pytest.raises(UpstreamProtocolViolation, match="identity encoding"):
            await MediaDownloader((HOST,), client=client).download(URL, target, "image")
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]
    assert not private_files


async def test_identity_header_accepts_case_and_outer_whitespace(tmp_path, private_files):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "image/png", "content-encoding": " IDENTITY "},
                content=b"image",
            )
        )
    ) as client:
        target = tmp_path / "asset"
        result = await MediaDownloader((HOST,), client=client).download(URL, target, "image")
    assert result["bytes"] == 5
    assert target.read_bytes() == b"image"


async def test_asgi_scope_cancellation_closes_file_client_and_removes_partial_download(
    tmp_path, monkeypatch, private_files
):
    opened = []
    real_fdopen = os.fdopen

    def track_fdopen(*args, **kwargs):
        handle = real_fdopen(*args, **kwargs)
        opened.append(handle)
        return handle

    class CancelledStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * 65536
            scope.cancel()
            await sleep(0)
            yield b"unreachable"  # 由 HTTPX 调用；本用例在上一行取消后不读取。

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "image/png"}, stream=CancelledStream()
            )
        )
    )
    monkeypatch.setattr(mod.os, "fdopen", track_fdopen)
    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **_kwargs: client)
    target = tmp_path / "asset"
    target.write_bytes(b"old")
    with CancelScope() as scope:
        await MediaDownloader((HOST,)).download(URL, target, "image")
    assert scope.cancelled_caught
    assert opened[0].closed
    assert client.is_closed
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]
