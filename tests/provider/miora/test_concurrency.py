"""本地容量快照、有界排队与取消不能耗掉健康账号的名额。"""

import asyncio

import pytest

from src.provider.base import ErrKind
from src.provider.miora import MioraProvider
from src.provider.miora import client as client_module
from src.provider.miora.client import MioraClient, MioraQueueTimeout
from src.provider.miora.credential import MioraCredential
from src.tasks.pacer import Pacer
from tests.provider.miora.test_provider import Client


def test_capacity_snapshot_does_not_reserve_or_advance_rate_window():
    clock = [0.0]
    pacer = Pacer(5, 5, allow_concurrent=True, max_concurrency=1,
                  window_seconds=60, max_requests_per_window=3, now=lambda: clock[0])
    assert pacer.minimum_wait("a") == 0
    assert pacer._inflight == pacer._starts == pacer._bucket_started == {}
    pacer._bucket_started["a"] = 0
    assert pacer.minimum_wait("a") == 5
    pacer._inflight["a"] = 1
    assert pacer.minimum_wait("a") is None
    assert pacer.minimum_wait("b") == 0
    pacer._max_concurrency = lambda: 2
    assert pacer.minimum_wait("a") == 0
    pacer._starts["a"] = [0, 1, 2]
    assert pacer.minimum_wait("a") == 60
    clock[0] = 60
    assert pacer.minimum_wait("a") == 0
    assert pacer._starts["a"] == [0, 1, 2]


def test_capacity_snapshot_handles_strict_mode_and_default_bucket():
    clock = [0.0]
    pacer = Pacer(5, 5, now=lambda: clock[0])
    assert pacer.minimum_wait() == 0
    pacer._last_started = 0
    assert pacer.minimum_wait() == 5
    clock[0] = 10
    assert pacer.minimum_wait() == 0
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1)
    pacer._inflight[""] = 1
    assert pacer.minimum_wait() is None


async def test_provider_capacity_accounts_for_shared_runtime_lock_and_pacer():
    client = MioraClient()
    provider = MioraProvider(client=client)
    raw = {"access_token": "t", "uid": "same"}
    account = client._account(MioraCredential.from_dict(raw))
    assert provider.queue_delay(raw) == 0
    await account.lock.acquire()
    try:
        assert provider.queue_delay({**raw, "project_id": "alias"}) is None
        assert provider.queue_delay({**raw, "uid": "other"}) == 0
    finally:
        account.lock.release()
    provider.pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1)
    await provider.pacer.wait_turn("same")
    assert provider.queue_delay(raw) is None
    provider.pacer.release("same")
    assert provider.queue_delay(raw) == 0
    assert MioraProvider(client=Client()).queue_delay(raw) == 0


async def test_pacer_queue_timeout_does_not_penalize_or_release_another_request(monkeypatch):
    import src.provider.miora as module

    monkeypatch.setattr(module, "CHAT_QUEUE_TIMEOUT_SECONDS", .01)
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1)
    await pacer.wait_turn("u")
    client = Client()
    provider = MioraProvider(client=client, pacer=pacer)
    with pytest.raises(MioraQueueTimeout, match="排队超时") as caught:
        [event async for event in provider.stream_chat({"access_token": "t", "uid": "u"},
                                                       {}, "model")]
    assert caught.value.kind() == ErrKind.UPSTREAM_PROTOCOL
    assert caught.value.error_code == "local_queue_timeout"
    assert pacer._inflight == {"u": 1}
    assert pacer._waiters == {}
    assert client.seen == []
    pacer.release("u")
    assert len([event async for event in provider.stream_chat(
        {"access_token": "t", "uid": "u"}, {}, "model")]) == 2
    assert pacer._inflight == {}


async def test_account_queue_timeout_and_cancellation_leave_owner_lock_intact(monkeypatch):
    monkeypatch.setattr(client_module, "CHAT_QUEUE_TIMEOUT_SECONDS", .01)
    client = MioraClient()
    credential = MioraCredential("t", uid="u")
    account = client._account(credential)
    await account.lock.acquire()
    with pytest.raises(MioraQueueTimeout, match="账号排队超时"):
        await client.fetch_models(credential)
    assert account.lock.locked() and account.users == 0
    monkeypatch.setattr(client_module, "CHAT_QUEUE_TIMEOUT_SECONDS", 60)
    task = asyncio.create_task(client.fetch_models(credential))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert account.lock.locked() and account.users == 0
    account.lock.release()
    with pytest.raises(ValueError, match="owner failure"):
        async with client._account_turn(account):
            raise ValueError("owner failure")
    assert not account.lock.locked()
