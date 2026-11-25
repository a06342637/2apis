"""真实旧Key批次清理；释放/政策暂关/时钟变化不能丢掉活跃窗口或并发登记。"""

from __future__ import annotations

import pytest

from src.key_quota import KeyQuota, KeyQuotaGuard, QuotaExceeded
from tests.test_key_quota import _Clock


@pytest.mark.parametrize("trigger", ["usage", "disabled_admit"])
def test_thousand_released_old_keys_are_collected_by_unrelated_call(trigger):
    clock = _Clock(0)
    guard = KeyQuotaGuard(now_fn=clock)
    for index in range(1000):
        guard.admit(f"old-{index}", KeyQuota(rpm=1, tpm=10), tokens=10).release()
    assert len(guard._events) == 1000 and guard._inflight == {}
    clock.now = 121
    if trigger == "usage":
        assert guard.window_usage("fresh") == (0, 0)
    else:
        assert not guard.admit("fresh", KeyQuota()).counted
    assert guard._events == {} and guard._inflight == {}


@pytest.mark.parametrize("quota,tokens,message", [
    (KeyQuota(rpm=1), 1, "requests per minute"),
    (KeyQuota(tpm=10), 10, "tokens per minute"),
])
def test_cleanup_keeps_hot_key_window_after_release_and_temporary_policy_disable(
        quota, tokens, message):
    clock = _Clock(0)
    guard = KeyQuotaGuard(now_fn=clock)
    guard.admit("old", quota, tokens=tokens).release()
    clock.now = 100
    guard.admit("hot", quota, tokens=tokens).release()
    clock.now = 121
    # 清理旧桶，同时窗口内的hot不能随release或临时关闭配额而被淘汰。
    assert guard.admit("hot", KeyQuota()).counted is False
    assert set(guard._events) == {"hot"}
    with pytest.raises(QuotaExceeded, match=message):
        guard.admit("hot", quota, tokens=1)
    assert guard.window_usage("hot") == (1, tokens)


def test_cleanup_preserves_long_inflight_and_release_still_returns_concurrency_slot():
    clock = _Clock(0)
    guard = KeyQuotaGuard(now_fn=clock)
    quota = KeyQuota(max_concurrency=1)
    active = guard.admit("active", quota)
    guard.admit("old", quota).release()
    clock.now = 121
    guard.window_usage("unrelated")
    assert "active" in guard._events and "old" not in guard._events
    with pytest.raises(QuotaExceeded, match="concurrent"):
        guard.admit("active", quota)
    assert guard.inflight("active") == 1
    active.release()
    active.release()
    next_lease = guard.admit("active", quota)
    assert guard.inflight("active") == 1
    next_lease.release()
    clock.now = 182
    guard.window_usage("unrelated")
    assert guard._events == {} and guard._inflight == {}


def test_cleanup_uses_configured_window_and_clock_rollback_keeps_future_rate_history():
    clock = _Clock(1000)
    guard = KeyQuotaGuard(window_seconds=240, now_fn=clock)
    quota = KeyQuota(rpm=1)
    guard.admit("hot", quota).release()
    clock.now = 1121
    assert guard.window_usage("unrelated") == (0, 0)
    assert "hot" in guard._events
    clock.now = 900
    with pytest.raises(QuotaExceeded) as caught:
        guard.admit("hot", quota)
    assert caught.value.retry_after == 340
    clock.now = 1240
    guard.window_usage("unrelated")
    assert guard._events == {}
