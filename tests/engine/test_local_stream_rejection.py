import asyncio

import pytest

from src.api import streaming
from src.api.deps import ServicePaused as ExportedServicePaused
from src.api.streaming import respond_with_stream_start
from src.compat.openai.errors import ServicePaused
from src.compat.openai.request import ChatRequest, InvalidRequest
from src.engine.executor import Executor


def source(error, *, emitted=False, gate=None):
    closed = []

    class Engine(Executor):
        def __init__(self):
            pass

        async def stream(self, *_args, **_kwargs):
            try:
                if emitted:
                    yield b"data: first\n\n"
                if gate is not None:
                    await gate.wait()
                raise error
            finally:
                closed.append(1)

    return Engine(), closed


def request():
    return ChatRequest(model="text", messages=[], stream=True, raw={})


@pytest.mark.parametrize("kind", ["pause", "invalid"])
@pytest.mark.parametrize("emitted", [False, True])
@pytest.mark.parametrize("callback", [False, True])
async def test_local_rejection_uses_controlled_error_and_reports_only_before_first_frame(
    kind, emitted, callback
):
    error = (ServicePaused("private operator reason")
             if kind == "pause" else InvalidRequest("invalid"))
    engine, closed = source(error, emitted=emitted)
    seen = []
    frames = [frame async for frame in engine.stream_guarded(request(),
        on_terminal=(lambda code, message: seen.append((code, message))) if callback else None)]
    code = "service_paused" if kind == "pause" else "invalid_request"
    assert code.encode() in frames[-1]
    assert b"internal_error" not in frames[-1]
    assert b"private operator reason" not in frames[-1]
    assert seen == ([(code, str(error))] if callback and not emitted else [])
    assert closed == [1]
    if emitted:
        assert frames[0] == b"data: first\n\n"


@pytest.mark.parametrize("kind,status", [("pause", 503), ("invalid", 400)])
async def test_local_rejection_before_headers_is_plain_json_with_correct_status(kind, status):
    error = ServicePaused() if kind == "pause" else InvalidRequest("invalid")
    engine, closed = source(error)
    terminal = {}
    frames = engine.stream_guarded(request(), on_terminal=lambda code, message: terminal.update(
        code=code, message=message))
    response = await respond_with_stream_start(frames, terminal)
    assert response.status_code == status
    assert str(error).encode() in response.body
    assert closed == [1]


@pytest.mark.parametrize("kind", ["pause", "invalid"])
async def test_local_rejection_after_header_grace_stays_standard_error_frame(monkeypatch, kind):
    monkeypatch.setattr(streaming, "STREAM_START_GRACE_SECONDS", 0.001)
    gate = asyncio.Event()
    error = ServicePaused() if kind == "pause" else InvalidRequest("invalid")
    engine, closed = source(error, gate=gate)
    terminal = {}
    response = await respond_with_stream_start(engine.stream_guarded(request(),
        on_terminal=lambda code, message: terminal.update(code=code, message=message)), terminal)
    assert response.status_code == 200
    gate.set()
    content = b"".join([chunk async for chunk in response.body_iterator])
    code = "service_paused" if kind == "pause" else "invalid_request"
    assert code.encode() in content and content.endswith(b"data: [DONE]\n\n")
    assert b"internal_error" not in content
    assert closed == [1]


def test_legacy_service_paused_import_is_the_shared_class():
    assert ExportedServicePaused is ServicePaused
