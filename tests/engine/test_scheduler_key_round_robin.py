"""同账号集合的多Key独立公平轮换，游标有界而健康/负载/冷却仍共享。"""

from src.engine.scheduler import MAX_ROUND_ROBIN_RECORDS, Scheduler
from src.request_context import set_api_key_id
from tests.engine.test_scheduler_account_binding import account


def test_per_key_round_robin_uses_trusted_context_and_shared_load_or_cooldown():
    scheduler = Scheduler()
    pool = [account("a", health=100), account("b", health=50), account("c", health=100)]
    try:
        set_api_key_id("key-one")
        assert scheduler.select(pool, set(), 100, round_robin=True) == "a"
        set_api_key_id("key-two")
        assert scheduler.select(pool, set(), 100, round_robin=True) == "a"
        set_api_key_id("key-one")
        assert scheduler.select(pool, set(), 100, round_robin=True) == "b"
        set_api_key_id("key-two")
        assert scheduler.select(pool, set(), 100, round_robin=True) == "b"
        loaded = [account("a", health=100, inflight=1), account("b", health=50),
                  account("c", health=100, cooling_until=200)]
        assert scheduler.select(loaded, set(), 100, round_robin=True) == "b"
        assert scheduler.select(pool, {"a", "b", "c"}, 100, round_robin=True) is None
    finally:
        set_api_key_id(None)


def test_round_robin_cursor_memory_is_bounded_without_changing_eligible_pool():
    scheduler = Scheduler()
    pool = [account("only", health=100)]
    for index in range(MAX_ROUND_ROBIN_RECORDS + 1):
        assert scheduler.select(pool, set(), 0, round_robin=True,
                                round_robin_key=f"key-{index}") == "only"
    assert len(scheduler._round_robin_last) == MAX_ROUND_ROBIN_RECORDS
    assert ("key-0", "only") not in scheduler._round_robin_last
    assert scheduler.select(pool, set(), 0, round_robin=True, round_robin_key="key-0") == "only"
    assert len(scheduler._round_robin_last) == MAX_ROUND_ROBIN_RECORDS
