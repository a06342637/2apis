"""模型并发信号按窗口回收并有界，容量拥挤不能清掉仍活跃的整组检测。"""

from src.engine.scheduler import (
    CONCURRENCY_BURST_WINDOW_SECONDS,
    CONCURRENCY_COOLDOWN_MAX_SECONDS,
    CONCURRENCY_COOLDOWN_SECONDS,
    Candidate,
    Scheduler,
)
from src.provider.base import ErrKind


def burst(scheduler, model, now, cid="first"):
    outcome = scheduler.note_error(Candidate(credential_id=cid, provider="miora"),
        ErrKind.CONCURRENCY, now, model=model)
    return outcome.model_cooldowns[model].cooling_until - now


def test_expired_burst_keys_are_reclaimed_and_live_sequence_preserved():
    scheduler = Scheduler()
    for index in range(20):
        assert burst(scheduler, f"old-{index}", 100) == CONCURRENCY_COOLDOWN_SECONDS
    assert burst(scheduler, "live", 150) == CONCURRENCY_COOLDOWN_SECONDS
    # 窗口边界仍属于原探测轮；超出边界后旧键应全部回收，保留较新的轮次。
    assert burst(scheduler, "old-0", 100 + CONCURRENCY_BURST_WINDOW_SECONDS,
                 "second") == CONCURRENCY_COOLDOWN_MAX_SECONDS
    assert burst(scheduler, "live", 191, "second") == CONCURRENCY_COOLDOWN_MAX_SECONDS
    assert list(scheduler._concurrency_bursts) == [("miora", "live")]
    assert burst(scheduler, "live", 241, "third") == CONCURRENCY_COOLDOWN_SECONDS


def test_capacity_does_not_evict_active_probe_sequence(monkeypatch):
    monkeypatch.setattr("src.engine.scheduler.MAX_CONCURRENCY_BURST_RECORDS", 4)
    scheduler = Scheduler()
    for index in range(100):
        assert burst(scheduler, f"model-{index}", 100) == CONCURRENCY_COOLDOWN_SECONDS
    assert len(scheduler._concurrency_bursts) == 4
    assert burst(scheduler, "model-0", 101, "second") == CONCURRENCY_COOLDOWN_MAX_SECONDS
    assert burst(scheduler, "model-0", 102, "second") == CONCURRENCY_COOLDOWN_SECONDS
    assert burst(scheduler, "new-after-window", 191) == CONCURRENCY_COOLDOWN_SECONDS
    assert list(scheduler._concurrency_bursts) == [("miora", "new-after-window")]


def test_clock_change_replaces_expired_record_behind_recent_head():
    scheduler = Scheduler()
    burst(scheduler, "future-first", 500)
    burst(scheduler, "older-second", 100)
    assert burst(scheduler, "older-second", 200, "second") == CONCURRENCY_COOLDOWN_SECONDS
    assert scheduler._concurrency_bursts[("miora", "older-second")] == (200, {"second"})
