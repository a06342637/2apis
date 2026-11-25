"""Miora 控制面、账号身份合并和每账号热更节流接线。"""

import pytest

from src.config import validate_miora_endpoint_allowed
from src.engine.model_resolver import KNOWN_PROVIDERS, resolve
from src.main import _miora_endpoint, build_app
from src.provider.login_identity import (
    MANUAL_MERGE_PROVIDERS,
    login_identities_for,
    login_identity_for,
)
from src.provider.tos import tos_for
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.helpers.auth import isolated_settings, settings_with
from tests.helpers.stores import MemoryStore


def test_endpoint_defaults_whitelist_and_normalization():
    settings = settings_with()
    assert settings.miora_allowed == ("https://miora.design",)
    assert _miora_endpoint(settings) == "https://miora.design"
    assert validate_miora_endpoint_allowed("https://miora.design/", settings)
    assert not validate_miora_endpoint_allowed("https://evil.example", settings)
    assert settings_with(miora_allowed_endpoints=" https://one.example/ ,, ").miora_allowed == (
        "https://one.example",)
    with pytest.raises(ValueError, match="MIORA_API_ENDPOINT"):
        _miora_endpoint(settings_with(miora_api_endpoint="https://evil.example"))
    with pytest.raises(ValueError, match="MIORA_API_ENDPOINT"):
        _miora_endpoint(settings_with(miora_api_endpoint=""))


def test_miora_registered_and_account_identity_is_stable():
    assert "miora" in KNOWN_PROVIDERS
    assert "miora" in MANUAL_MERGE_PROVIDERS
    assert tos_for("miora").level == "gray"
    target = resolve("glm-5.3@miora", "default")
    assert target.model == "glm-5.3" and target.providers == ("miora",) and target.forced
    assert login_identity_for("miora", {"uid": " 42 "}) == "42"
    assert login_identities_for("miora", {"user_id": "42"}) == {"42"}
    assert login_identity_for("miora", {}) == ""
    assert login_identities_for("miora", {}) == frozenset()


def test_miora_throttle_settings_are_hot():
    runtime = RuntimeSettings(settings_with(), MemoryStore())
    assert "miora_chat_min_interval" in HOT_BY_KEY
    assert "miora_max_concurrency" in HOT_BY_KEY
    assert runtime.miora_chat_min_interval == 5
    assert runtime.miora_max_concurrency == 1
    assert runtime.miora_max_requests_per_window == 3
    assert runtime.miora_request_window_seconds == 60
    assert "miora_max_requests_per_window" in HOT_BY_KEY
    runtime.set("miora_max_requests_per_window", 0)
    assert runtime.miora_max_requests_per_window == 0
    runtime.set("miora_chat_min_interval", 2)
    runtime.set("miora_max_concurrency", 3)
    assert runtime.miora_chat_min_interval == 2
    assert runtime.miora_max_concurrency == 3


async def test_real_wiring_keeps_miora_account_buckets_and_hot_limits(tmp_path):
    app = build_app(isolated_settings(tmp_path))
    services = app.state.services
    provider = services.registry["miora"]
    assert provider.client.host == "https://miora.design"
    assert provider.pacer.allow_concurrent
    assert provider.pacer.min_seconds == provider.pacer.max_seconds == 5
    assert provider.pacer.max_concurrency == 1
    assert provider.pacer.max_requests_per_window == 3
    assert provider.pacer.window_seconds == 60
    services.settings.set("miora_chat_min_interval", 0.5)
    services.settings.set("miora_max_concurrency", 2)
    assert provider.pacer.min_seconds == provider.pacer.max_seconds == 0.5
    assert provider.pacer.max_concurrency == 2
    for item in services.registry.values():
        await item.aclose()
