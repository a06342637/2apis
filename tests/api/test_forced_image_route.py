"""Responses 显式 image_generation 选择只进入有权限的 Miora，先拒绝无能力配置。"""

from __future__ import annotations

import pytest

from src.api.chat_media_gate import force_image_request
from src.api.deps import ApiKeyPrincipal
from src.compat.openai.request import parse_chat_request
from tests.api.test_miora_integration import import_account
from tests.api.test_miora_integration import miora_app as _miora_fixture

miora_app = _miora_fixture


def body(model="glm-5.3"):
    return {
        "model": model,
        "input": "generate an image",
        "tools": [{"type": "image_generation"}],
        "tool_choice": {"type": "image_generation"},
    }


@pytest.mark.parametrize(
    "conflict",
    ["key", "suffix", "registry", "service", "media_off", "auto_off", "unselected", "empty"],
)
def test_forced_generation_capability_conflicts_are_rejected_before_quota(
    miora_app, monkeypatch, conflict
):
    app, client, _network = miora_app
    services = app.state.services
    one = import_account(client)["id"] if conflict != "empty" else None
    policy = {"provider_binding": "codearts"} if conflict == "key" else {}
    if conflict == "unselected":
        import_account(client, "other")
        services.credentials.set_enabled(one, False)
        policy["credential_ids"] = [one]
    key = services.api_keys.create("root", **policy)

    def no_quota(*args, **kwargs):
        raise AssertionError("forced capability errors must precede quota admission")

    with monkeypatch.context() as changes:
        changes.setattr("src.api.endpoints._admit_quota", no_quota)
        if conflict == "registry":
            changes.setitem(services.registry, "miora", None)
        if conflict == "service":
            changes.setattr(services, "media", None)
        if conflict == "media_off":
            changes.setattr(services.settings, "_base", services.settings._base.model_copy(
                update={"media_enabled": False}))
        if conflict == "auto_off":
            services.settings.set("miora_auto_media_enabled", False)
        request = body("glm-5.3@codearts" if conflict == "suffix" else "glm-5.3")
        response = client.post(
            "/v1/responses", headers={"Authorization": f"Bearer {key['api_key']}"}, json=request
        )
        assert response.status_code == 400, response.text
        assert "image_generation" in response.text


def test_forced_generation_routes_miora_without_expanding_selected_accounts(miora_app):
    app, client, network = miora_app
    one = import_account(client)["id"]
    import_account(client, "other")
    key = app.state.api_keys.create("root", credential_ids=[one], allowed_models="glm-5.3")
    observed = []

    class Auto:
        async def stream_chat(self, credential, payload, model, factory):
            observed.append((credential.uid, model, payload["miora_image_generation_forced"]))
            async for event in factory(credential, payload, model):
                yield event

    app.state.services.registry["miora"].media_auto = Auto()
    result = client.post(
        "/v1/responses", headers={"Authorization": f"Bearer {key['api_key']}"}, json=body()
    )
    assert result.status_code == 200, result.text
    assert observed == [("account-1", "glm-5.3", True)]
    assert network.calls[0][0] == "account-1"


def test_forced_default_provider_override_still_checks_key_whitelist(miora_app, monkeypatch):
    app, client, _network = miora_app
    import_account(client)
    app.state.services.settings.set("default_model", "glm-5.3@codearts")
    key = app.state.api_keys.create("root", allowed_models="*@codearts")

    def no_quota(*args, **kwargs):
        raise AssertionError("forced route whitelist must precede quota")

    monkeypatch.setattr("src.api.endpoints._admit_quota", no_quota)
    request = body()
    request.pop("model")
    result = client.post(
        "/v1/responses", headers={"Authorization": f"Bearer {key['api_key']}"}, json=request
    )
    assert result.status_code == 400 and "not allowed" in result.text


def test_plain_marker_declaration_keeps_existing_provider_binding(miora_app):
    app, _client, _network = miora_app
    request = parse_chat_request(
        {
            "model": "text@codearts",
            "messages": [{"role": "user", "content": "code"}],
            "miora_image_generation": {},
        }
    )
    principal = ApiKeyPrincipal(username="root", provider_binding="codearts")
    returned, binding = force_image_request(app.state.services, request, principal)
    assert returned is request and binding == "codearts"
