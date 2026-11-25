import asyncio
import base64
import json

import pytest
from anyio import CancelScope
from starlette.requests import ClientDisconnect

from src.api import streaming as mod
from src.api.streaming import (
    JSON_KEEPALIVE_BYTES,
    _encoded_frames,
    _json_frames,
    respond_with_encoded_json,
    respond_with_keepalive,
    with_keepalive,
)
from src.media.streaming import MediaJSONStream, encode_media_json

SCOPE = {"type": "http", "asgi": {"spec_version": "2.4"}}


async def receive():
    await asyncio.Event().wait()


def simple_encoder(payload):
    async def frames():
        yield json.dumps(payload).encode()

    return frames()


async def render(response):
    messages = []

    async def send(message):
        messages.append(message)

    await response(SCOPE, receive, send)
    return b"".join(message.get("body", b"") for message in messages)


@pytest.mark.parametrize("error", [RuntimeError("header failed"), OSError("disconnected")])
async def test_header_failure_releases_once_before_encoder_starts(error):
    released, started = [], []

    def encode(payload):
        started.append(1)
        return simple_encoder(payload)

    response = respond_with_encoded_json({}, encode=encode, release=lambda: released.append(1))

    async def send(message):
        assert message["type"] == "http.response.start"
        raise error

    expected = ClientDisconnect if isinstance(error, OSError) else RuntimeError
    with pytest.raises(expected):
        await response(SCOPE, receive, send)
    assert started == []
    assert released == [1]
    response._release_once()
    assert released == [1]


async def test_asgi_disconnect_during_header_wait_never_starts_encoder():
    released, started = [], []
    header_started = asyncio.Event()

    def encode(payload):
        started.append(1)
        return simple_encoder(payload)

    async def send(message):
        assert message["type"] == "http.response.start"
        header_started.set()
        await asyncio.Event().wait()

    async def disconnect():
        await header_started.wait()
        return {"type": "http.disconnect"}

    response = respond_with_encoded_json({}, encode=encode, release=lambda: released.append(1))
    await response({"type": "http", "asgi": {"spec_version": "2.3"}}, disconnect, send)
    assert started == []
    assert released == [1]


@pytest.mark.parametrize("with_release", [False, True])
@pytest.mark.parametrize("with_convert", [False, True])
async def test_fast_encoded_response_converts_and_releases_at_body_end(with_release, with_convert):
    released = []

    async def work():
        return {"ok": 1}

    response = await respond_with_keepalive(
        work,
        grace=1,
        encode=simple_encoder,
        convert=(lambda payload: {"wrapped": payload}) if with_convert else None,
        release=(lambda: released.append(1)) if with_release else None,
    )
    assert released == []
    assert json.loads(await render(response)) == (
        {"wrapped": {"ok": 1}} if with_convert else {"ok": 1}
    )
    assert released == ([1] if with_release else [])


@pytest.mark.parametrize("with_release", [False, True])
@pytest.mark.parametrize("conversion_failure", [False, True])
async def test_fast_work_and_convert_errors_release_without_starting_body(
    with_release, conversion_failure
):
    released = []

    async def work():
        if not conversion_failure:
            raise ValueError("failed")
        return {}

    def convert(payload):
        raise ValueError("conversion failed")

    with pytest.raises(ValueError, match="failed"):
        await respond_with_keepalive(
            work,
            grace=1,
            encode=simple_encoder,
            convert=convert if conversion_failure else None,
            release=(lambda: released.append(1)) if with_release else None,
        )
    assert released == ([1] if with_release else [])


@pytest.mark.parametrize("with_release", [False, True])
async def test_synchronous_encoder_failure_still_releases(with_release):
    released = []

    def broken_encoder(payload):
        raise RuntimeError("encoder failed before iterator exists")

    response = respond_with_encoded_json(
        {}, encode=broken_encoder, release=(lambda: released.append(1)) if with_release else None
    )
    with pytest.raises(RuntimeError, match="before iterator"):
        await render(response)
    assert released == ([1] if with_release else [])


async def test_encoded_iterator_without_release_closes_inner_on_failure():
    closed = []

    def encoder(payload):
        async def frames():
            try:
                yield b"first"
                raise ValueError("late encoder failure")
            finally:
                closed.append(1)

        return frames()

    frames = _encoded_frames({}, encoder, None)
    assert await anext(frames) == b"first"
    with pytest.raises(ValueError, match="late encoder"):
        await anext(frames)
    assert closed == [1]


async def test_encoded_iterator_without_release_handles_synchronous_encoder_failure():
    def encoder(payload):
        raise ValueError("early failure")

    with pytest.raises(ValueError, match="early"):
        await anext(_encoded_frames({}, encoder, None))


@pytest.mark.parametrize("with_release", [False, True])
@pytest.mark.parametrize("with_convert", [False, True])
async def test_slow_encoded_response_keeps_whitespace_and_closes_encoder(
    with_release, with_convert
):
    gate = asyncio.Event()
    released, closed = [], []

    async def work():
        await gate.wait()
        return {"ok": 1}

    def encode(payload):
        async def frames():
            try:
                yield json.dumps(payload).encode()
            finally:
                closed.append(1)

        return frames()

    response = await respond_with_keepalive(
        work,
        grace=0.001,
        encode=encode,
        convert=(lambda payload: {"wrapped": payload}) if with_convert else None,
        release=(lambda: released.append(1)) if with_release else None,
    )
    chunks = []

    async def send(message):
        if message["type"] == "http.response.body" and message["body"]:
            assert released == []
            chunks.append(message["body"])
            if message["body"] == JSON_KEEPALIVE_BYTES:
                gate.set()

    await response(SCOPE, receive, send)
    assert chunks[0] == JSON_KEEPALIVE_BYTES
    data = b"".join(chunks)
    assert json.loads(data) == ({"wrapped": {"ok": 1}} if with_convert else {"ok": 1})
    assert closed == [1]
    assert released == ([1] if with_release else [])


async def test_raw_keepalive_frames_without_release_callback_finish_cleanly():
    async def work():
        return {"ok": 1}

    task = asyncio.create_task(work())
    await task
    assert [chunk async for chunk in _json_frames(task, 0.001, None, None)] == [b'{"ok":1}']


async def test_default_sse_interval_reads_current_runtime_setting(monkeypatch):
    monkeypatch.setattr(mod, "KEEPALIVE_SECONDS", 0.001)

    async def waiting():
        await asyncio.Event().wait()
        yield b"unreachable"  # 由 with_keepalive 调用；本用例在等待期关闭流。

    frames = with_keepalive(waiting())
    assert await anext(frames) == mod.SSE_COMMENT
    await frames.aclose()


async def test_explicit_close_under_cancelled_scope_releases_image_io_and_quota(tmp_path):
    path = tmp_path / "image"
    path.write_bytes(b"image-content")
    slots, released = asyncio.Semaphore(1), []
    payload = MediaJSONStream(
        created=1, paths=[path], maximum=100, slots=slots, keepalive=0
    )
    response = respond_with_encoded_json(
        payload, encode=encode_media_json, release=lambda: released.append(1)
    )
    with CancelScope() as scope:
        assert await anext(response.body_iterator) == b'{"created":1,"data":['
        assert await anext(response.body_iterator) == b'{"b64_json":"'
        assert base64.b64decode(await anext(response.body_iterator)) == b"image-conten"
        assert slots.locked()
        scope.cancel()
        await response.body_iterator.aclose()
    assert not slots.locked()
    assert released == [1]
    response._release_once()
    assert released == [1]


async def test_body_send_failure_closes_active_image_io_and_quota(tmp_path):
    path = tmp_path / "image"
    path.write_bytes(b"image-content")
    slots, released = asyncio.Semaphore(1), []
    payload = MediaJSONStream(created=1, paths=[path], maximum=100, slots=slots, keepalive=0)
    response = respond_with_encoded_json(
        payload, encode=encode_media_json, release=lambda: released.append(1)
    )
    chunks = 0

    async def send(message):
        nonlocal chunks
        if message["type"] == "http.response.body":
            chunks += 1
            if chunks == 3:
                assert slots.locked()
                raise OSError("client gone")

    with pytest.raises(ClientDisconnect):
        await response(SCOPE, receive, send)
    assert released == [1]
    assert not slots.locked()


@pytest.mark.parametrize("encode", [None, simple_encoder])
async def test_slow_header_failure_cancels_work_and_releases_before_body_starts(encode):
    started = asyncio.Event()
    cancelled, released = [], []

    async def work():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cancelled.append(1)

    response = await respond_with_keepalive(
        work, grace=0.001, encode=encode, release=lambda: released.append(1)
    )
    await started.wait()

    async def send(message):
        assert message["type"] == "http.response.start"
        raise OSError("header disconnected")

    with pytest.raises(ClientDisconnect):
        await response(SCOPE, receive, send)
    assert released == [1]
    assert cancelled == [1]
