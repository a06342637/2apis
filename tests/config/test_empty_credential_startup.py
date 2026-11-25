"""默认启动保持空账号池；无鉴权渠道由用户明确添加，删除后重启不补回。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.main import build_app
from src.provider.base import Event, EventKind, Usage
from tests.helpers.auth import isolated_settings
from tests.helpers.constants import SECRET
from tests.helpers.startup import wait_for_startup


def test_new_database_startup_has_no_implicit_channel_accounts(tmp_path):
    app = build_app(isolated_settings(tmp_path))
    with TestClient(app) as client:
        wait_for_startup(app, client)
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        assert client.get("/api/credentials").json()["credentials"] == []
        key = app.state.api_keys.create("root")["api_key"]
        response = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"})
        assert response.status_code == 200 and response.json()["data"] == []
        assert {"zen", "kilo"} <= set(app.state.services.registry)


def test_manual_channels_can_be_used_and_deleted_accounts_stay_deleted(tmp_path, monkeypatch):
    settings = isolated_settings(tmp_path)
    first = build_app(settings)
    with TestClient(first) as client:
        wait_for_startup(first, client)
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = {}
        for provider in ("zen", "kilo"):
            response = client.post("/api/credentials", json={
                "provider": provider, "credential": {}, "nickname": f"My {provider}"})
            assert response.status_code == 200, response.text
            created[provider] = response.json()["id"]

        rows = first.state.credentials.list_all()
        assert len(rows) == 2 and all(row["added_by"] == "root" for row in rows)
        key = first.state.api_keys.create("root")["api_key"]
        headers = {"Authorization": f"Bearer {key}"}
        catalog = client.get("/v1/models", headers=headers).json()["data"]
        assert {provider for item in catalog for provider in item["providers"]} == {"zen", "kilo"}

        async def answer(payload, model, api_key=""):
            yield Event(kind=EventKind.CONTENT, content="manual channel works")
            yield Event(kind=EventKind.USAGE, usage=Usage(input_tokens=1, output_tokens=2))
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

        for provider in ("zen", "kilo"):
            monkeypatch.setattr(first.state.services.registry[provider].client,
                                "stream_chat", answer)
            model = next(item["by_provider"][provider]["raw_id"]
                         for item in catalog if provider in item["providers"])
            response = client.post("/v1/chat/completions", headers=headers, json={
                "model": f"{model}@{provider}", "messages": [{"role": "user", "content": "hello"}]})
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == "manual channel works"

        assert client.delete(f"/api/credentials/{created['kilo']}").status_code == 200

    second = build_app(settings)
    with TestClient(second) as client:
        wait_for_startup(second, client)
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        rows = second.state.credentials.list_all()
        assert [row["id"] for row in rows] == [created["zen"]]
        assert rows[0]["added_by"] == "root"
        assert second.state.credentials.candidates(["kilo"]) == []
        assert client.delete(f"/api/credentials/{created['zen']}").status_code == 200

    third = build_app(settings)
    with TestClient(third) as client:
        wait_for_startup(third, client)
        assert third.state.credentials.list_all() == []
