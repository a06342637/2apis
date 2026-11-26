from types import MethodType

import pytest

from src import main
from src.api.deps import ServicePaused
from src.compat.openai.request import InvalidRequest
from src.provider.base import Event, EventKind
from src.provider.miora import MioraProvider
from src.provider.miora.client import MioraClient
from src.request_context import credential_scope
from tests.api.test_miora_integration import token
from tests.api.test_miora_preflight import gateway as _gateway_fixture
from tests.helpers.auth import isolated_settings

gateway = _gateway_fixture


def test_waiting_chat_reads_latest_persisted_token_before_inference(gateway):
    app, client, boundary, provider, headers, key = gateway
    account = key["credential_ids"][0]
    fresh = token("preflight-account", "rotated")
    observed = []

    async def wait_turn(_identity):
        data = app.state.credentials.credential_data(account)
        app.state.credentials.save_credential_data(account, {**data, "access_token": fresh})

    async def stream_chat(self, credential, payload, model):
        observed.append(credential.access_token)
        yield Event(kind=EventKind.CONTENT, content="OK")
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    provider.pacer.wait_turn = wait_turn
    boundary.stream_chat = MethodType(stream_chat, boundary)
    response = client.post("/v1/chat/completions", headers=headers,
        json={"model": "glm-5.3@miora", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200, response.text
    assert observed == [fresh]


@pytest.mark.parametrize("mode", ["gateway", "account"])
def test_waiting_chat_stops_when_operator_pauses_before_inference(gateway, mode):
    app, client, boundary, provider, headers, key = gateway
    account = key["credential_ids"][0]

    async def wait_turn(_identity):
        if mode == "gateway":
            app.state.services.settings.set("gateway_paused", True)
        else:
            app.state.credentials.set_enabled(account, False)

    provider.pacer.wait_turn = wait_turn
    response = client.post("/v1/chat/completions", headers=headers,
        json={"model": "glm-5.3@miora", "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code != 200, response.text
    assert not boundary.posts
    assert app.state.services.key_quota.inflight(key["id"]) == 0


@pytest.mark.parametrize("mode", ["gateway", "account"])
def test_stream_pause_is_controlled_and_does_not_penalize_account(gateway, mode):
    app, client, boundary, provider, headers, key = gateway
    account = key["credential_ids"][0]
    before = next(row for row in app.state.credentials.candidates() if row.credential_id == account)

    async def wait_turn(_identity):
        if mode == "gateway":
            app.state.services.settings.set("gateway_paused", True)
        else:
            app.state.credentials.set_enabled(account, False)

    provider.pacer.wait_turn = wait_turn
    response = client.post("/v1/chat/completions", headers=headers,
        json={"model": "glm-5.3@miora", "stream": True,
              "messages": [{"role": "user", "content": "hi"}]})
    after = next(row for row in app.state.credentials.candidates() if row.credential_id == account)
    assert not boundary.posts
    assert after.health == before.health and after.cooling_until == before.cooling_until
    assert app.state.services.key_quota.inflight(key["id"]) == 0
    assert "internal_error" not in response.text, response.text


def test_main_wires_actual_client_and_guards_no_context_selected_missing_and_paused_accounts(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(main, "require_supported_platform", lambda: None)
    monkeypatch.setattr("src.db.conn.secure_dir", lambda *_args, **_kwargs: True)
    provider = MioraProvider(client=MioraClient())
    app = main.build_app(isolated_settings(tmp_path), providers={"miora": provider})
    services = app.state.services
    assert provider.client.reload_credential is provider.reload_credential
    assert provider.client.before_inference is provider.before_inference
    with credential_scope(None):
        assert provider.reload_credential() is None
        assert provider.before_inference() is None
    for uid in ("other", "selected"):
        selected = services.credentials.add(provider="miora", credential_data={
            "access_token": "synthetic-" + uid, "uid": uid})
    with credential_scope(selected):
        assert provider.reload_credential()["uid"] == "selected"
        assert provider.before_inference() is None
    with credential_scope("missing"), pytest.raises(InvalidRequest, match="paused or removed"):
        provider.before_inference()
    services.credentials.set_enabled(selected, False)
    with credential_scope(selected), pytest.raises(InvalidRequest, match="paused or removed"):
        provider.before_inference()
    services.settings.set("gateway_paused", True)
    with credential_scope(None), pytest.raises(ServicePaused):
        provider.before_inference()
    services.stats_query._db.close()
