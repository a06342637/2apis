import httpx
import pytest
from fastapi import FastAPI

from src.api.media import create_router
from src.provider.miora import media as protocol
from src.provider.miora.client import MioraClient
from src.provider.miora.credential import MioraCredential
from src.provider.miora.media import FILM_API, MioraMediaClient
from src.webapp.handlers import register_exception_handlers
from tests.api.test_media import Guard
from tests.provider.miora.test_media import Api, completed_slot, row, task

pytestmark = pytest.mark.real_upstream("miora_media")


@pytest.fixture
async def gateway(media_env, monkeypatch):
    env = media_env
    api = Api()
    api.catalogue = {
        "data": [
            row(modelId="raw-image", label="Pretty Picture"),
            row("video", modelId="raw-video", label="Pretty Video"),
        ]
    }
    api.signed = {"signedUrls": ["https://sign.miorausercontent.com/0"]}
    env.settings.nonstream_keepalive_seconds = 0
    env.settings.trust_proxy = False
    env.services.media = env.manager
    env.services.api_keys.authenticate = lambda _key: env.state.key
    env.services.key_quota = Guard()
    env.services.user_quota = Guard()
    env.manager.provider.credential_from = lambda data: MioraCredential("synthetic", uid=data["id"])

    async def public(_host):
        pass

    monkeypatch.setattr(protocol, "ensure_public_host", public)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as network:
        upstream = MioraClient(short_client=network)
        env.manager.client = MioraMediaClient(upstream)
        app = FastAPI()
        app.state.services = env.services
        app.include_router(create_router(env.services))
        register_exception_handlers(app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "validation-once"},
        ) as client:
            yield env, api, client
        await upstream.aclose()


def generation_posts(api):
    return [
        call for call in api.calls
        if call[0] == "POST" and (
            call[1] == f"{FILM_API}/sessions" or call[1].endswith("/tasks")
        )
    ]


@pytest.mark.parametrize(
    "kind,invalid",
    [
        ("video", {"seconds": 9}),
        ("video", {"aspect_ratio": "4:5"}),
        ("video", {"resolution": "480P"}),
        ("image", {"aspect_ratio": "4:5"}),
        ("image", {"resolution": "512"}),
    ],
)
async def test_real_directory_parameter_rejection_is_400_before_any_generation_post(
    gateway, kind, invalid
):
    env, api, client = gateway
    endpoint = "/v1/videos" if kind == "video" else "/v1/images/generations"
    body = {"model": "pretty-video" if kind == "video" else "pretty-picture", "prompt": "cup"}
    for _ in range(2):
        response = await client.post(endpoint, json={**body, **invalid})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_request"
        assert not generation_posts(api)
        assert not list(env.manager.store.directory.glob("*.json"))
        assert not env.state.records
        assert not env.manager.locks
        assert env.services.inflight.count("a") == 0
        assert all(
            lease.released == 1
            for guard in (env.services.key_quota, env.services.user_quota)
            for lease in guard.leases
        )
    api.create_response = {"session": {"id": "s1"}, "task": task(kind)}
    api.progress = [task(kind, status="completed", slots=[completed_slot(kind)])]
    corrected = await client.post(endpoint, json=body)
    assert corrected.status_code == 200, corrected.text
    assert len(generation_posts(api)) == 1
    assert len(env.state.records) == 1 and env.state.records[0]["ok"]


async def test_post_network_failure_remains_durable_and_is_never_replayed(gateway):
    env, api, client = gateway
    api.failures[f"{FILM_API}/sessions"] = httpx.ReadError("response lost after POST")
    # 会话 GET 须成功，只有真正生成 POST 模拟失去响应。
    real_call = api.__call__

    def dispatch(request):
        if request.method == "GET":
            failure = api.failures.pop(f"{FILM_API}/sessions")
            try:
                return real_call(request)
            finally:
                api.failures[f"{FILM_API}/sessions"] = failure
        return real_call(request)

    env.manager.client.client._short._transport = httpx.MockTransport(dispatch)
    body = {"model": "pretty-video", "prompt": "cup"}
    first = await client.post("/v1/videos", json=body)
    assert first.status_code == 200 and first.json()["status"] == "failed"
    second = await client.post("/v1/videos", json=body)
    assert second.json()["id"] == first.json()["id"]
    assert len(generation_posts(api)) == 1
    assert len(list(env.manager.store.directory.glob("*.json"))) == 1
    assert len(env.state.records) == 1 and not env.state.records[0]["ok"]
