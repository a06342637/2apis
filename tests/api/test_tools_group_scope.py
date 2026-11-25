"""内置search/fetch同样只能使用动态组成员，不借共享池重试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.provider.codebuddy.tools import ToolRejected
from tests.api.test_tools import _app, _ToolProvider


@pytest.mark.parametrize("path,body", [
    ("/v1/tools/search", {"query": "hello"}),
    ("/v1/fetch", {"url": "https://public.example"}),
])
@pytest.mark.parametrize("reject", [False, True])
def test_aux_tools_obey_selected_group_and_never_retry_unselected_account(
        tmp_path, monkeypatch, path, body, reject):
    monkeypatch.setattr("src.api.tools.blocked_webhook_host", lambda _url: False)
    provider = _ToolProvider(reject_403=reject)

    async def fetch(data, **_kwargs):
        provider.calls.append(data["bearer_token"])
        if reject:
            raise ToolRejected(403, "credential rejected")
        return {"content": "allowed account"}

    provider.fetch_url = fetch
    app = _app(tmp_path, provider)
    selected = app.state.credentials.add(provider="codebuddy",
                                         credential_data={"bearer_token": "allowed"})
    group = app.state.services.credential_groups.create("codebuddy", "Tools", [selected])
    with TestClient(app) as client:
        key = app.state.api_keys.create("root", group_ids=[group["id"]])
        headers = {"Authorization": f"Bearer {key['api_key']}"}
        response = client.post(path, headers=headers, json=body)
        assert response.status_code == (403 if reject else 200), response.text
        assert provider.calls == ["allowed"]
        app.state.services.credential_groups.update(group["id"], credential_ids=[])
        assert client.post(path, headers=headers, json=body).status_code == 403
        assert provider.calls == ["allowed"]


def test_aux_provider_binding_filters_other_channels_before_selecting_accounts(tmp_path):
    provider = _ToolProvider()
    app = _app(tmp_path, provider)
    with TestClient(app) as client:
        key = app.state.api_keys.create("root", provider_binding="miora")
        response = client.post("/v1/tools/search", json={"query": "hello"},
            headers={"Authorization": f"Bearer {key['api_key']}"})
    assert response.status_code == 503 and provider.calls == []
