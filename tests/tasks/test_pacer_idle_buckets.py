"""Pacer按时间清旧桶，保留窗口/等待者/在途，不用LRU丢配额。"""

from __future__ import annotations

import asyncio

import pytest

from src.tasks.pacer import Pacer, paced


async def test_thousand_released_old_buckets_are_collected_and_disabled_call_keeps_no_buckets():
    clock = {"now": 0.0}
    caps = {"concurrency": 1, "requests": 1}
    pacer = Pacer(0, 0, allow_concurrent=True,
        max_concurrency=lambda: caps["concurrency"],
        max_requests_per_window=lambda: caps["requests"], window_seconds=60,
        now=lambda: clock["now"])
    for index in range(1000):
        async with paced(pacer, f"old-{index}"):
            pass
    assert len(pacer._bucket_started) == len(pacer._starts) == 1000
    assert pacer._inflight == pacer._waiters == {}
    clock["now"] = 121
    async with paced(pacer, "hot"):
        assert set(pacer._bucket_started) == set(pacer._starts) == {"hot"}
    caps.update(concurrency=0, requests=0)
    clock["now"] = 182
    await pacer.wait_turn("unused-while-disabled")
    assert pacer._starts == pacer._bucket_started == pacer._inflight == pacer._waiters == {}


@pytest.mark.parametrize("disabled", ["length", "requests"])
async def test_temporary_window_disable_cannot_clean_still_valid_rate_history(disabled):
    clock = {"now": 0.0}
    policy = {"window": 240, "requests": 1}
    slept = []
    sleeping = asyncio.Event()

    async def sleep(seconds):
        slept.append(seconds)
        sleeping.set()
        await asyncio.Future()

    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1,
        max_requests_per_window=lambda: policy["requests"],
        window_seconds=lambda: policy["window"], sleep=sleep, now=lambda: clock["now"])
    await pacer.wait_turn("account")
    pacer.release("account")
    clock["now"] = 70
    policy["window" if disabled == "length" else "requests"] = 0
    async with paced(pacer, "unrelated"):
        pass
    assert pacer._starts["account"] == [0]
    policy.update(window=240, requests=1)
    clock["now"] = 80
    pending = asyncio.create_task(pacer.wait_turn("account"))
    await sleeping.wait()
    assert not pending.done() and slept == [160]
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert pacer._waiters == {} and pacer._starts["account"] == [0]


async def test_longer_live_window_is_observed_before_sweep_and_old_credit_still_blocks():
    clock = {"now": 0.0}
    window = {"seconds": 60}
    sleeping = asyncio.Event()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        sleeping.set()
        await asyncio.Future()

    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1,
        window_seconds=lambda: window["seconds"], sleep=sleep, now=lambda: clock["now"])
    async with paced(pacer, "old"):
        pass
    clock["now"] = 120
    window["seconds"] = 240
    async with paced(pacer, "fresh"):
        pass
    assert pacer._starts["old"] == [0]
    pending = asyncio.create_task(pacer.wait_turn("old"))
    await sleeping.wait()
    assert slept == [120] and not pending.done()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def test_expired_bucket_with_waiters_is_preserved_until_all_waiters_cancel():
    clock = {"now": 0.0}
    sleeping = asyncio.Event()

    async def sleep(_seconds):
        sleeping.set()
        await asyncio.Future()

    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1,
        window_seconds=300, sleep=sleep, now=lambda: clock["now"])
    async with paced(pacer, "waiting"):
        pass
    clock["now"] = 1
    pending = [asyncio.create_task(pacer.wait_turn("waiting")) for _ in range(2)]
    await sleeping.wait()
    await asyncio.sleep(0)
    assert pacer._waiters == {"waiting": 2} and pacer._inflight == {}
    pending[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending[0]
    assert pacer._waiters == {"waiting": 1}
    clock["now"] = 400
    async with paced(pacer, "fresh"):
        pass
    assert pacer._starts["waiting"] == [0] and "waiting" in pacer._bucket_started
    pending[1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending[1]
    assert pacer._waiters == {}
    clock["now"] = 461
    async with paced(pacer, "another"):
        pass
    assert "waiting" not in pacer._starts and "waiting" not in pacer._bucket_started


async def test_expired_long_inflight_remains_capped_and_cancellation_does_not_take_a_slot():
    clock = {"now": 0.0}
    pacer = Pacer(0, 0, allow_concurrent=True, max_concurrency=1,
        window_seconds=60, now=lambda: clock["now"])
    await pacer.wait_turn("active")
    clock["now"] = 121
    async with paced(pacer, "fresh"):
        pass
    assert "active" in pacer._bucket_started
    pending = asyncio.create_task(pacer.wait_turn("active"))
    await asyncio.sleep(0)
    assert not pending.done() and pacer._inflight == {"active": 1}
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    pacer.release("active")
    clock["now"] = 182
    async with paced(pacer, "another"):
        pass
    assert "active" not in pacer._bucket_started
    assert pacer._waiters == pacer._inflight == {}


async def test_idle_retention_covers_long_minimum_interval_and_planned_start_cancel():
    clock = {"now": 0.0}
    entered_sleep = asyncio.Event()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        entered_sleep.set()
        await asyncio.Future()

    pacer = Pacer(100, 100, allow_concurrent=True, max_concurrency=1,
        sleep=sleep, now=lambda: clock["now"])
    pacer.next_interval = lambda: 100
    async with paced(pacer, "account"):
        pass
    clock["now"] = 70
    async with paced(pacer, "other"):
        pass
    pending = asyncio.create_task(pacer.wait_turn("account"))
    await entered_sleep.wait()
    assert slept == [30] and pacer._bucket_started["account"] == 100
    clock["now"] = 400
    async with paced(pacer, "fresh"):
        pass
    assert pacer._inflight["account"] == 1 and pacer._waiters["account"] == 1
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert pacer._inflight == pacer._waiters == {}
    clock["now"] = 461
    async with paced(pacer, "another"):
        pass
    assert "account" not in pacer._bucket_started


async def test_clock_rollback_cannot_clear_future_start_and_bypass_minimum_spacing():
    clock = {"now": 100.0}
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds

    pacer = Pacer(5, 5, allow_concurrent=True, max_concurrency=1,
                  sleep=sleep, now=lambda: clock["now"])
    pacer.next_interval = lambda: 5
    async with paced(pacer, "account"):
        pass
    clock["now"] = 0
    async with paced(pacer, "account"):
        pass
    assert slept == [105] and clock["now"] == 105
