"""非流式同样需要逐事件截止，不能等整体 600s 才释放停滞账号。"""

import asyncio

import pytest

from src.engine.inflight import InflightGuard
from src.provider.base import ErrKind, Event, EventKind
from tests.engine.test_executor_stream_timeout import (
    STALL,
    _executor,
    _StallProvider,
    _stream_request,
)
from tests.helpers.events import GOOD


@pytest.mark.parametrize("first", [[], [Event(kind=EventKind.CONTENT)]])
async def test_nonstream_first_event_deadline_rotates_and_releases(repo, first):
    credentials, db = repo
    credentials.add(provider="trae", credential_data={"accessToken": "a"})
    credentials.add(provider="trae", credential_data={"accessToken": "b"})
    provider = _StallProvider(first=first, later=GOOD)
    guard = InflightGuard(lambda: 1)
    executor = _executor(credentials, provider, first_event_timeout_seconds=STALL,
                         short_cooldown_wait_seconds=0, inflight=guard)
    result = await executor.complete(_stream_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 2 and guard.total() == 0
    cooled = db.connect().execute(
        "SELECT err_count FROM credentials WHERE cooling_until IS NOT NULL").fetchall()
    assert len(cooled) == 1 and cooled[0]["err_count"] == 0


@pytest.mark.parametrize("kind", [ErrKind.SOFT, ErrKind.NETWORK])
async def test_nonstream_error_rotates_without_waiting_for_stalled_upstream_eof(repo, kind):
    credentials, _db = repo
    for token in ("a", "b"):
        credentials.add(provider="trae", credential_data={"accessToken": token})
    provider = _StallProvider(first=[Event(kind=EventKind.ERROR, error_kind=kind,
                                         error_message="failed")], later=GOOD)
    guard = InflightGuard(lambda: 1)
    executor = _executor(credentials, provider, inflight=guard)
    # 未启用停滞超时：真实错误事件必须立即处理，不依赖后续EOF或超时来换号。
    result = await asyncio.wait_for(executor.complete(_stream_request()), timeout=1)
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 2 and guard.total() == 0


@pytest.mark.parametrize("hide_reasoning", [False, True])
async def test_nonstream_idle_deadline_discards_partial_output_before_safe_retry(
    repo, hide_reasoning,
):
    credentials, _db = repo
    for token in ("a", "b"):
        credentials.add(provider="trae", credential_data={"accessToken": token})
    provider = _StallProvider(first=[Event(kind=EventKind.REASONING, content="thinking"),
                                    Event(kind=EventKind.CONTENT, content="incomplete")],
                              later=GOOD)
    guard = InflightGuard(lambda: 1)
    executor = _executor(credentials, provider, stream_idle_timeout_seconds=STALL,
                         force_disable_thinking=hide_reasoning, inflight=guard)
    result = await executor.complete(_stream_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 2 and guard.total() == 0
