"""四套聊天出口提前拒绝媒体模型，且只能使用当前 Key 的账号目录判断。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.api.chat_media_gate import chat_account_tables, ensure_chat_endpoint
from src.api.deps import ApiKeyPrincipal
from src.compat.openai.request import InvalidRequest, parse_chat_request
from src.provider.base import Model
from tests.api.test_miora_integration import import_account
from tests.api.test_miora_integration import miora_app as _miora_fixture

miora_app = _miora_fixture
IMAGE = Model(id="image", name="Image model", gateway_endpoints=("/v1/images/generations",))
TEXT = Model(id="text", name="Text model", gateway_endpoints=("/v1/chat/completions",))


def request(model):
    return parse_chat_request({"model": model, "messages": [{"role": "user", "content": "hello"}]})


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("bound", [False, True])
def test_all_protocols_reject_media_before_quota_or_inference(
    miora_app, monkeypatch, protocol, stream, bound
):
    app, client, network = miora_app
    account = import_account(client)["id"]
    services = app.state.services

    async def catalog(credential):
        return [IMAGE, TEXT]

    network.fetch_models = catalog
    services.model_list_cache["miora"] = {"image": IMAGE, "text": TEXT}
    key = app.state.api_keys.create("root", credential_ids=[account] if bound else [])
    headers = {"Authorization": f"Bearer {key['api_key']}"}

    def no_quota(*args, **kwargs):
        raise AssertionError("endpoint capability must be checked before quota admission")

    monkeypatch.setattr("src.api.endpoints._admit_quota", no_quota)
    body = {"model": "image@miora", "stream": stream, "max_tokens": 32}
    if protocol == "responses":
        path, body["input"] = "/v1/responses", "hello"
    elif protocol == "gemini":
        action = "streamGenerateContent" if stream else "generateContent"
        path = f"/v1beta/models/image@miora:{action}"
        body = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}
    else:
        path = "/v1/messages" if protocol == "anthropic" else "/v1/chat/completions"
        body["messages"] = [{"role": "user", "content": "hello"}]
    response = client.post(path, headers=headers, json=body)
    assert response.status_code == 400, response.text
    assert "/v1/images/generations" in response.text and "media endpoint" in response.text
    assert network.calls == []


def test_bound_gate_uses_selected_account_and_respects_suffix_and_binding(miora_app):
    app, client, _network = miora_app
    one = import_account(client)["id"]
    two = import_account(client, "two")["id"]
    services = app.state.services
    services.model_list_cache["miora"] = {"image": IMAGE}
    selected = ApiKeyPrincipal(username="root", credential_ids=(one,))
    tables = {one: {"image": Model(id="image")}, two: {"image": IMAGE}}
    ensure_chat_endpoint(services, request("image@miora"), selected, tables)
    # 未选账号的媒体声明不能拒绝选中账号的旧/未知文本声明。
    ensure_chat_endpoint(services, request("image"), selected, {two: {"image": IMAGE}})
    selected = replace(selected, provider_binding="miora")
    ensure_chat_endpoint(services, request("image@codearts"), selected, {one: {"image": IMAGE}})
    selected = replace(selected, provider_binding="codearts")
    ensure_chat_endpoint(services, request("image"), selected, {one: {"image": IMAGE}})
    selected = replace(selected, provider_binding="miora")
    with pytest.raises(InvalidRequest, match="images/generations"):
        ensure_chat_endpoint(services, request("image"), selected, {one: {"image": IMAGE}})
    assert chat_account_tables(
        {one: {"image": IMAGE, "text": TEXT, "legacy": Model(id="legacy")}}
    ) == {one: {"text": TEXT, "legacy": Model(id="legacy")}}


def test_media_alias_mapping_and_unknown_catalog_keep_legacy_behavior(miora_app):
    app, client, _network = miora_app
    one = import_account(client)["id"]
    services = app.state.services
    selected = ApiKeyPrincipal(username="root", credential_ids=(one,))
    tables = {one: {"image": IMAGE}}
    with pytest.raises(InvalidRequest, match="media endpoint"):
        ensure_chat_endpoint(services, request("image-model@miora"), selected, tables)
    services.settings.set("model_aliases", "draw=image@miora")
    with pytest.raises(InvalidRequest, match="media endpoint"):
        ensure_chat_endpoint(services, request("draw@miora"), selected, tables)
    services.model_aliases["miora"] = {"old-image-name": "image"}
    with pytest.raises(InvalidRequest, match="media endpoint"):
        ensure_chat_endpoint(services, request("old-image-name@miora"), selected, tables)
    ensure_chat_endpoint(services, request("missing@miora"), selected, tables)
    ensure_chat_endpoint(services, request("image@miora"), selected, {})
    ensure_chat_endpoint(services, request("image@miora"), ApiKeyPrincipal(username="root"))
    services.model_list_cache["miora"] = {"image": IMAGE}
    ensure_chat_endpoint(services, request("missing@miora"), ApiKeyPrincipal(username="root"))


def test_unbound_gate_narrows_registered_aliases_and_handles_empty_endpoints(miora_app):
    app, client, _network = miora_app
    import_account(client)
    services = app.state.services
    services.model_list_cache["miora"] = {"image": IMAGE}
    services.model_aliases["miora"] = {"image-model": "image"}
    with pytest.raises(InvalidRequest, match="media endpoint"):
        ensure_chat_endpoint(services, request("image-model"), ApiKeyPrincipal(username="root"))
    services.model_list_cache["miora"]["image"] = Model(id="image", gateway_endpoints=())
    with pytest.raises(InvalidRequest, match="supported media endpoint"):
        ensure_chat_endpoint(services, request("image@miora"), ApiKeyPrincipal(username="root"))
    services.model_list_cache["miora"]["image"] = TEXT
    ensure_chat_endpoint(services, request("image@miora"), ApiKeyPrincipal(username="root"))
    services.model_aliases.clear()
    ensure_chat_endpoint(services, request("text"), ApiKeyPrincipal(username="root"))


def test_gemini_catalog_scopes_accounts_provider_and_filters_media_only(miora_app):
    app, client, network = miora_app
    one = import_account(client)["id"]
    import_account(client, "unselected")

    async def catalog(credential):
        return [Model(id="allowed" if credential.uid == "account-1" else "foreign"), IMAGE, TEXT]

    network.fetch_models = catalog
    key = app.state.api_keys.create("root", credential_ids=[one], provider_binding="miora")
    headers = {"Authorization": f"Bearer {key['api_key']}"}
    listed = client.get("/v1beta/models", headers=headers)
    assert listed.status_code == 200, listed.text
    assert {entry["name"] for entry in listed.json()["models"]} == {
        "models/allowed",
        "models/text-model",
    }
    standard = client.get("/v1/models", headers=headers).json()["data"]
    assert any(entry.get("gateway_endpoints") == ["/v1/images/generations"] for entry in standard)
    foreign = app.state.api_keys.create("root", provider_binding="trae")
    assert (
        client.get(
            "/v1beta/models", headers={"Authorization": f"Bearer {foreign['api_key']}"}
        ).json()["models"]
        == []
    )
    app.state.credentials.set_enabled(one, False)
    assert client.get("/v1beta/models", headers=headers).json()["models"] == []
