"""真实装配共用媒体服务、目录缓存和独立限流设置。"""

from __future__ import annotations

from src.api.models import _refresh_provider
from src.main import build_app
from src.provider.base import Model
from src.provider.miora import MioraProvider
from src.provider.miora.client import MioraHTTPError
from src.tasks.pacer import paced
from tests.helpers.auth import isolated_settings


async def test_miora_media_service_auto_and_model_probe_share_live_objects(tmp_path):
    settings = isolated_settings(tmp_path, upstream_status_enabled=False)
    app = build_app(settings)
    services = app.state.services
    provider = services.registry["miora"]
    assert isinstance(provider, MioraProvider)
    assert services.media.provider is provider
    assert services.media.stats is app.state.stats_collector
    assert provider.media_auto.service is services.media
    assert provider.media_client is services.media.client
    assert provider.media_client.client is provider.client
    paths = app.openapi()["paths"]
    assert "/v1/images/generations" in paths and "/v1/videos" in paths
    async with app.router.lifespan_context(app):
        assert app.state.model_probe_task._model_list_cache is services.model_list_cache


async def test_custom_non_miora_provider_keeps_media_unmounted(tmp_path):
    class Dummy:
        async def aclose(self):
            pass

    app = build_app(isolated_settings(tmp_path), providers={"miora": Dummy()})
    assert app.state.services.media is None
    assert "/v1/images/generations" not in app.openapi()["paths"]
    async with app.router.lifespan_context(app):
        pass


async def test_real_miora_window_counts_are_independent_of_concurrency_without_real_sleep(
    tmp_path, monkeypatch
):
    settings = isolated_settings(tmp_path, upstream_status_enabled=False)
    app = build_app(settings)
    services = app.state.services
    pacer = services.registry["miora"].pacer
    assert pacer.max_concurrency == 1 and pacer.max_requests_per_window == 3
    services.settings.set("miora_max_concurrency", 2)
    assert pacer.max_requests_per_window == 3
    clock = [0.0]

    async def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(pacer, "_now", lambda: clock[0])
    monkeypatch.setattr(pacer, "_sleep", sleep)
    monkeypatch.setattr(pacer, "next_interval", lambda: 5.0)
    starts = []
    for _ in range(5):
        async with paced(pacer, "one"):
            starts.append(clock[0])
    assert starts == [0, 5, 10, 60, 65]
    async with paced(pacer, "two"):
        assert clock[0] == 65
    services.settings.set("miora_max_requests_per_window", 0)
    assert pacer.max_concurrency == 2 and pacer.max_requests_per_window == 0
    async with app.router.lifespan_context(app):
        pass


async def test_media_catalog_failure_is_visible_as_shared_directory_degradation(
    tmp_path, monkeypatch
):
    app = build_app(isolated_settings(tmp_path))
    services = app.state.services
    provider = services.registry["miora"]
    services.credentials.add(
        provider="miora", credential_data={"access_token": "test", "refresh_token": "r", "uid": "u"}
    )

    async def text_models(credential):
        assert credential.uid == "u"
        return [Model(id="text")]

    async def failure(credential, *, include_unavailable=False):
        raise MioraHTTPError(503, b"directory temporarily unavailable")

    monkeypatch.setattr(provider.client, "fetch_models", text_models)
    monkeypatch.setattr(provider.media_client, "catalog", failure)
    try:
        await _refresh_provider(services, "miora", provider)
        assert services.model_list_degraded["miora"] is True
        assert "保留文本" in services.model_list_failure_reason["miora"]
        assert set(services.model_list_cache["miora"]) == {"text"}

        async def recovered(credential, *, include_unavailable=False):
            return [Model(id="image", gateway_endpoints=("/v1/images/generations",))]

        monkeypatch.setattr(provider.media_client, "catalog", recovered)
        await _refresh_provider(services, "miora", provider)
        assert services.model_list_degraded["miora"] is False
        assert "miora" not in services.model_list_failure_reason
        assert set(services.model_list_cache["miora"]) == {"text", "image"}
    finally:
        for registered in services.registry.values():
            await registered.aclose()
        services.log_repo.close()
        app.state.stats_collector.flush()
        services.credentials._db.close()
