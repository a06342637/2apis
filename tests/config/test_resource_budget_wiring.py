"""真实配置、热更与重启装配共用预算，并保持管理接口可访问。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src import main
from src.runtime_settings import HOT_BY_KEY, InvalidSetting, RuntimeSettings
from src.webapp.limits import BodySizeLimitMiddleware
from src.webapp.request_budget import BudgetRejected, RequestBudgetMiddleware
from tests.helpers.auth import isolated_settings, settings_with
from tests.helpers.stores import MemoryStore


@pytest.mark.parametrize(("key", "maximum"), [
    ("resource_max_active_requests", 64),
    ("resource_max_queued_requests", 256),
    ("resource_queue_timeout_seconds", 300),
    ("resource_input_budget_mb", 512),
])
def test_resource_limits_have_identical_startup_and_hot_update_ranges(key, maximum):
    runtime = RuntimeSettings(settings_with(), MemoryStore())
    assert HOT_BY_KEY[key].group == "resources"
    for value in (0, maximum):
        startup = settings_with(**{key: value})
        assert getattr(startup, key) == value
        runtime.set(key, value)
        assert getattr(runtime, key) == value
    for value in (-1, maximum + 1):
        with pytest.raises(ValidationError):
            settings_with(**{key: value})
        with pytest.raises(InvalidSetting):
            runtime.set(key, value)
    runtime.reset(key)
    assert getattr(runtime, key) == getattr(settings_with(), key)


def test_default_resource_profile_and_runtime_getters():
    runtime = RuntimeSettings(settings_with(), MemoryStore())
    assert runtime.resource_max_active_requests == 0
    assert runtime.resource_max_queued_requests == 32
    assert runtime.resource_queue_timeout_seconds == 60
    assert runtime.resource_input_budget_mb == 0


async def test_build_app_uses_hot_runtime_and_restart_keeps_limits_with_fresh_budget(
    tmp_path, monkeypatch
):
    # 平台门禁由独立测试覆盖；此处只验证配置与中间件装配，兼容 Windows 测试。
    monkeypatch.setattr(main, "require_supported_platform", lambda: None)
    monkeypatch.setattr("src.db.conn.secure_dir", lambda *_args, **_kwargs: True)
    config = isolated_settings(tmp_path, resource_max_active_requests=4,
                               resource_input_budget_mb=16, upstream_status_enabled=False)
    previous_budget = None
    for attempt in range(2):
        app = main.build_app(config, providers={})
        runtime = app.state.services.settings
        middleware = next(item for item in app.user_middleware
                          if item.cls is RequestBudgetMiddleware)
        assert middleware.kwargs["settings"] is runtime
        classes = [item.cls for item in app.user_middleware]
        assert classes.index(BodySizeLimitMiddleware) < classes.index(RequestBudgetMiddleware)
        stack = app.build_middleware_stack()
        while not isinstance(stack, RequestBudgetMiddleware):
            stack = stack.app
        budget = stack.budget
        assert budget is not previous_budget
        if previous_budget is not None:
            assert budget._condition is not previous_budget._condition
        async with app.router.lifespan_context(app):
            if attempt == 0:
                assert runtime.resource_max_active_requests == 4
                runtime.set("resource_max_active_requests", 1)
                runtime.set("resource_max_queued_requests", 0)
                runtime.set("resource_input_budget_mb", 2)
            assert runtime.resource_max_active_requests == 1
            assert runtime.resource_input_budget_mb == 2
            active = await budget.acquire(1)
            with pytest.raises(BudgetRejected) as rejected:
                await budget.acquire(1)
            assert rejected.value.code == "resource_queue_full"
            runtime.set("resource_max_active_requests", 2)
            second = await budget.acquire(1)
            assert budget.active_requests == budget.active_input_mb == 2
            await active.release()
            await second.release()
            assert budget.active_requests == budget.active_input_mb == budget.queued_requests == 0
            # 每次模拟重启前保存相同的限制，下次装配从 DB 恢复。
            runtime.set("resource_max_active_requests", 1)
        previous_budget = budget
