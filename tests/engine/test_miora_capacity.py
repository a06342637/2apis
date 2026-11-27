"""忙碌窗口不拖住空闲账号；固定会话、绑定和手工 pin 保留约束。"""

from dataclasses import replace

import pytest

from src.engine.half_open import HalfOpenGuard
from src.engine.model_resolver import ModelTarget
from tests.engine.test_miora_balancing import catalog, engine, request, run


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("delays, selected", [
    ([60, 0, None], 1), ([20, 10, None], 1), ([None, None, None], 0),
])
async def test_miora_uses_account_with_shortest_local_wait(repo, stream, bound, delays, selected):
    executor, _credentials, provider, ids, uids = engine(repo)
    provider.queue_delay = lambda data: delays[uids.index(data["uid"])]
    scope = {"credential_ids": tuple(ids), "credential_models": catalog(ids)} if bound else {}
    await run(executor, stream, **scope)
    assert provider.calls[0][0] == uids[selected]
    assert executor._deps.inflight.total() == 0


@pytest.mark.parametrize("stream", [False, True])
async def test_capacity_does_not_move_valid_session_or_manual_pin(repo, stream):
    executor, credentials, provider, ids, uids = engine(repo)
    provider.queue_delay = lambda data: None if data["uid"] == uids[0] else 0
    credentials.set_pinned(ids[0])
    await run(executor, stream, request(session="session"))
    assert provider.calls[-1][0] == uids[0]
    credentials.set_pinned(None)
    await run(executor, stream, request(session="session"))
    assert provider.calls[-1][0] == uids[0]
    await run(executor, stream, credential_ids=(ids[2],), credential_models=catalog([ids[2]]))
    assert provider.calls[-1][0] == uids[2]


def test_capacity_ignores_tried_and_deleted_credentials(repo, monkeypatch):
    executor, credentials, provider, ids, uids = engine(repo)
    provider.queue_delay = lambda data: 0
    candidates = credentials.candidates()
    assert executor._miora_ready(candidates, set(ids), None) == candidates
    read_data = credentials.credential_data
    monkeypatch.setattr(credentials, "credential_data",
                        lambda cid: None if cid == ids[0] else read_data(cid))
    ready = executor._miora_ready(candidates, {ids[2]}, None)
    assert [candidate.credential_id for candidate in ready] == [ids[1]]


def test_busy_half_open_probe_still_tries_slower_healthy_account(repo):
    executor, credentials, provider, ids, uids = engine(repo)
    guard = HalfOpenGuard(now=lambda: 100)
    executor._deps.half_open = guard
    provider.queue_delay = lambda data: 0 if data["uid"] == uids[0] else 5
    rows = {candidate.credential_id: candidate for candidate in credentials.candidates()}
    recovering = replace(rows[ids[0]], cooling_until=1)
    target = ModelTarget("chat", ("miora",), forced=True)
    key = executor._half_open_key(recovering, target, 100)
    assert key == (ids[0], "") and guard.acquire(key)
    selected = executor._choose([recovering, rows[ids[1]]], target, set(), None, 100)
    assert selected == ids[1]
    assert executor._pending_probe == {}
    guard.release(key)
