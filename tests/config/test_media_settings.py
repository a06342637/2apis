"""媒体资源上限在启动前验证，避免无间隔轮询与无限制内存响应。"""

import pytest
from pydantic import ValidationError

from tests.helpers.auth import settings_with


@pytest.mark.parametrize(("name", "value"), [
    ("MEDIA_MAX_BYTES", 0), ("MEDIA_MAX_BYTES", 64 * 1024 * 1024 + 1),
    ("MEDIA_TIMEOUT_SECONDS", 0), ("MEDIA_TIMEOUT_SECONDS", 1801),
    ("MEDIA_SIGNED_URL_SECONDS", 0), ("MEDIA_SIGNED_URL_SECONDS", 86401),
    ("MEDIA_POLL_SECONDS", 0), ("MEDIA_POLL_SECONDS", 61),
])
def test_media_resource_limits(name, value):
    with pytest.raises(ValidationError):
        settings_with(**{name: value})


def test_media_defaults_and_runtime_switch():
    from src.runtime_settings import RuntimeSettings
    from tests.helpers.stores import MemoryStore

    settings = settings_with()
    assert settings.media_enabled and settings.miora_auto_media_enabled
    assert settings.media_poll_seconds == 2
    runtime = RuntimeSettings(settings, MemoryStore())
    runtime.set("miora_auto_media_enabled", False)
    assert runtime.miora_auto_media_enabled is False
