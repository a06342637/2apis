import asyncio

import pytest

from src.provider.miora import media as mod
from src.provider.miora.credential import MioraCredential
from src.provider.miora.media import MioraMediaClient
from tests.provider.miora.test_media import row

pytestmark = pytest.mark.real_upstream("miora_media")


async def test_cache_pressure_never_detaches_an_account_with_a_woken_lock_waiter(monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACCOUNTS", 1)
    gate = asyncio.Event()
    calls, active, maximum = [], 0, 0

    class Client:
        async def _request(self, credential, *_args, **_kwargs):
            nonlocal active, maximum
            calls.append(credential.uid)
            active += 1
            maximum = max(maximum, active)
            await gate.wait()
            active -= 1
            return {"data": [row()]}

    media = MioraMediaClient(Client())
    credential = MioraCredential("synthetic", uid="a")
    original = media._account(credential)
    await original.lock.acquire()
    waiting = asyncio.create_task(media.catalog(credential))
    await asyncio.sleep(0)
    original.lock.release()
    # release 已唤醒等待者，但 acquire 尚未恢复，locked() 此刻是 False。
    media._account(MioraCredential("other", uid="b"))
    second = asyncio.create_task(media.catalog(credential))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(waiting, second)
    assert maximum == 1
    assert calls == ["a"]
    assert media._account(credential) is original


async def test_cancelled_media_waiter_releases_reference_and_allows_idle_eviction(monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACCOUNTS", 1)
    media = MioraMediaClient(None)
    credential = MioraCredential("synthetic", uid="a")
    original = media._account(credential)
    await original.lock.acquire()
    waiting = asyncio.create_task(media.catalog(credential))
    await asyncio.sleep(0)
    assert original.users == 1
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert original.users == 0
    original.lock.release()
    media._account(MioraCredential("other", uid="b"))
    assert "a" not in media._accounts


async def test_media_cache_keeps_all_active_references_then_trims_when_each_finishes(monkeypatch):
    monkeypatch.setattr(mod, "MAX_ACCOUNTS", 1)
    media = MioraMediaClient(None)
    one, two = MioraCredential("one", uid="a"), MioraCredential("two", uid="b")
    async with media._using_account(one) as a:
        async with media._using_account(two) as b:
            assert a.users == b.users == 1
            assert len(media._accounts) == 2
        assert b.users == 0 and list(media._accounts) == ["a"]
    assert a.users == 0
