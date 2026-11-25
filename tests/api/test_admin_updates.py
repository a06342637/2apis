from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.admin_updates import create_router
from src.api.deps import principal_from_request
from src.auth.rbac import Principal
from src.webapp.handlers import register_exception_handlers


def install(app, monkeypatch, failure=False):
    calls = []
    class Client:
        def __init__(self, *_args):
            return
        async def call(self, action, payload):
            calls.append((action, payload))
            if failure:
                raise ValueError("CI 未通过")
            return {"enabled": True}
    monkeypatch.setattr("src.api.admin_updates.UpdateClient", Client)
    services = SimpleNamespace(settings=SimpleNamespace(update_socket_path="/run/socket",
                                update_secret_file="/run/secret"),
                               audit=SimpleNamespace(record=lambda **_: None))
    app.router.routes[:] = [route for route in app.router.routes
                            if not getattr(route, "path", "").startswith("/api/system-updates")]
    app.include_router(create_router(services))
    return calls


@pytest.fixture
def update_client():
    # 此路由只桥接主机，不需要完整 provider 启动或平台锁；鉴权身份单独注入，
    # 真正 require_admin/CSRF/错误映射仍由生产实现裁决。
    app = FastAPI()
    current = {"admin": True}
    app.dependency_overrides[principal_from_request] = lambda: Principal("root", current["admin"])
    register_exception_handlers(app)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", "session")
        yield app, client, current


def test_update_admin_actions_and_error(update_client, monkeypatch):
    app, client, _ = update_client
    calls = install(app, monkeypatch)
    assert client.get("/api/system-updates").json() == {"enabled": True}
    assert client.post("/api/system-updates/check").status_code == 200
    assert client.put("/api/system-updates/source", json={"source": "a/b"}).status_code == 200
    assert client.post("/api/system-updates/start", json={"commit": "a" * 40}).status_code == 200
    assert calls == [("status", None), ("check", None), ("source", {"source": "a/b"}),
                     ("start", {"commit": "a" * 40})]


def test_update_error_becomes_controlled_bad_request(update_client, monkeypatch):
    app, client, _ = update_client
    install(app, monkeypatch, failure=True)
    response = client.get("/api/system-updates")
    assert response.status_code == 400 and "CI" in response.json()["error"]["message"]


def test_updates_require_admin_and_csrf(update_client, monkeypatch):
    app, client, current = update_client
    calls = install(app, monkeypatch)
    current["admin"] = False
    for method, path in (("get", ""), ("post", "/check"), ("put", "/source"),
                         ("post", "/start")):
        assert client.request(method, f"/api/system-updates{path}", json={}).status_code == 403
    assert calls == []
    current["admin"] = True
    assert client.post("/api/system-updates/check", headers={"Origin": "https://evil.example"}
                       ).status_code == 403
