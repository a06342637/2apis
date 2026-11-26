import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from src import main
from src.api.deps import ApiKeyPrincipal, Services
from src.api.miora_preflight import preflight_miora
from src.compat.openai.request import ChatRequest, InvalidRequest
from src.provider.base import Event, EventKind, Model
from src.provider.miora import MioraProvider
from src.tasks.pacer import Pacer
from tests.api.test_miora_integration import LocalMioraClient, token
from tests.helpers.auth import isolated_settings
from tests.helpers.startup import wait_for_startup

MODEL = Model("glm-5.3", name="GLM 5.3", supports_tool_call=True)
TABLE = {"glm-5.3": MODEL}


def chat(model="glm-5.3", **raw):
    messages = raw.pop("messages", [{"role": "user", "content": "hello"}])
    return ChatRequest(model=model, messages=messages, stream=False, raw=raw)


def service(rows=None, cache=None):
    return SimpleNamespace(
        settings=SimpleNamespace(default_model="glm-5.3", model_aliases=""),
        credentials=SimpleNamespace(
            candidates=lambda **_kwargs: rows if rows is not None else [
                SimpleNamespace(credential_id="a", provider="miora")
            ]
        ),
        model_list_cache={"miora": TABLE} if cache is None else cache,
        model_aliases={},
    )


@pytest.mark.parametrize(
    "model,binding,tables,rows,override",
    [
        ("glm-5.3@miora", "codearts", None, None, None),
        ("glm-5.3@codearts", "miora", None, None, None),
        ("glm-5.3", "", None, None, None),
        ("glm-5.3", "", {}, [], None),
        ("glm-5.3", "", {"a": TABLE, "b": TABLE}, [
            SimpleNamespace(credential_id="a", provider="miora"),
            SimpleNamespace(credential_id="b", provider="trae"),
        ], None),
        ("glm-5.3@miora", "miora", None, None, "trae"),
    ],
)
async def test_other_or_ambiguous_routes_keep_their_own_validation(
    model, binding, tables, rows, override
):
    await preflight_miora(
        service(rows), chat(model, temperature=0.5),
        ApiKeyPrincipal("root", provider_binding=binding),
        account_tables=tables, provider_binding=override,
    )


@pytest.mark.parametrize("route", ["binding", "suffix", "tables", "override"])
@pytest.mark.parametrize(
    "invalid",
    [
        {"temperature": 0.5},
        {"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
        {"messages": ["bad"]},
        {"tools": [{"type": "function", "function": {
            "name": "tool", "parameters": {"type": "invalid-schema-type"}}}]},
        {"tool_choice": "required"},
        {"max_tokens": 0},
    ],
)
async def test_clearly_miora_requests_validate_parameters_messages_and_tools(route, invalid):
    principal = ApiKeyPrincipal("root", provider_binding="miora" if route == "binding" else "")
    with pytest.raises(InvalidRequest):
        await preflight_miora(
            service(), chat("glm-5.3@miora" if route == "suffix" else "glm-5.3", **invalid),
            principal, account_tables={"a": TABLE} if route == "tables" else None,
            provider_binding="miora" if route == "override" else None,
        )


@pytest.mark.parametrize("model", [None, "", " auto ", "AUTO"])
async def test_automatic_model_selection_does_not_reject_an_unrelated_default(model):
    services = service()
    services.settings.default_model = "missing-default"
    await preflight_miora(services, chat(model), ApiKeyPrincipal("root", provider_binding="miora"))


@pytest.mark.parametrize("table", [None, {}, [], {"bad": {}}, {1: MODEL}])
async def test_missing_empty_or_malformed_global_directory_is_not_authority(table):
    await preflight_miora(
        service(cache={"miora": table}), chat("new-model@miora"), ApiKeyPrincipal("root")
    )


async def test_loaded_global_directory_rejects_unknown_model():
    with pytest.raises(InvalidRequest, match="does not offer"):
        await preflight_miora(service(), chat("unknown@miora"), ApiKeyPrincipal("root"))


@pytest.mark.parametrize("scope", ["only_selected", "supplied_tables"])
async def test_model_directory_scope_does_not_include_other_accounts(scope):
    rows = [SimpleNamespace(credential_id="a", provider="miora"),
            SimpleNamespace(credential_id="b", provider="trae")]
    principal = ApiKeyPrincipal("root", credential_ids=("a",) if scope == "only_selected" else ())
    await preflight_miora(service(rows), chat(), principal, account_tables={"a": TABLE})


@pytest.mark.parametrize("tables", [None, {}, {"a": TABLE}, {"a": TABLE, "b": {}},
                                     {"a": TABLE, "b": []}])
async def test_incomplete_selected_account_tables_cannot_reject_unknown(tables):
    rows = [SimpleNamespace(credential_id=account, provider="miora") for account in ("a", "b")]
    await preflight_miora(
        service(rows), chat("new-model@miora"),
        ApiKeyPrincipal("root", credential_ids=("a", "b")), account_tables=tables,
    )


async def test_selected_known_tables_accept_any_matching_account_and_reject_unknown():
    rows = [SimpleNamespace(credential_id=account, provider="miora") for account in ("a", "b")]
    tables = {"a": {"other": Model("other")}, "b": TABLE}
    principal = ApiKeyPrincipal("root", credential_ids=("a", "b"))
    await preflight_miora(service(rows), chat(), principal, account_tables=tables)
    with pytest.raises(InvalidRequest, match="does not offer"):
        await preflight_miora(service(rows), chat("unknown"), principal, account_tables=tables)
    # 存在绑定范围但没有活跃账号时，保留执行器原来的无可用账号错误。
    await preflight_miora(service(rows=[]), chat("unknown@miora"), principal, account_tables=tables)


@pytest.mark.parametrize("alias", ["canonical", "custom", "existing"])
async def test_valid_canonical_and_custom_aliases_are_not_rejected(alias):
    services = service()
    if alias == "canonical":
        services.model_list_cache = {"miora": {"raw": Model("raw", name="Friendly Model")}}
        model = "friendly-model"
    elif alias == "custom":
        services.settings.model_aliases = "fast=glm-5.3@miora"
        model = "fast"
    else:
        services.model_aliases = {"miora": {"legacy": "glm-5.3"}}
        model = "legacy"
    await preflight_miora(services, chat(model), ApiKeyPrincipal("root", provider_binding="miora"))


class HTTPBoundary(LocalMioraClient):
    def __init__(self):
        super().__init__()
        self.posts = []
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        self.posts.append(request.url.path)
        return httpx.Response(200, json={"text": "OK"})

    async def stream_chat(self, credential, payload, model):
        result = await self.http.post("https://miora.design/mock-inference", json=payload)
        yield Event(kind=EventKind.CONTENT, content=result.json()["text"])
        yield Event(kind=EventKind.FINISH, finish_reason="stop")

    async def aclose(self):
        await self.http.aclose()
        await super().aclose()


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "require_supported_platform", lambda: None)
    monkeypatch.setattr("src.db.conn.secure_dir", lambda *_args, **_kwargs: True)
    boundary = HTTPBoundary()
    provider = MioraProvider(client=boundary, pacer=Pacer(0, 0, allow_concurrent=True,
                                                       max_concurrency=1))

    async def catalog(_credential, *, force=False, include_unavailable=False):
        return []

    provider.media_client = SimpleNamespace(catalog=catalog)
    app = main.build_app(isolated_settings(tmp_path, NONSTREAM_KEEPALIVE_SECONDS=15,
        DEFAULT_MODEL="glm-5.3",
        MODEL_ALIASES="friendly=glm-5.3@miora",
        MEDIA_ENABLED=False, MIORA_AUTO_MEDIA_ENABLED=False), providers={"miora": provider})
    account = app.state.credentials.add(provider="miora", credential_data={
        "access_token": token("preflight-account", "initial"), "refresh_token": "synthetic"})
    key = app.state.api_keys.create("root", "preflight", provider_binding="miora",
        credential_ids=[account], quota='{"max_concurrency":1}')
    headers = {"Authorization": "Bearer " + key["api_key"]}
    with TestClient(app) as client:
        wait_for_startup(app, client)
        assert isinstance(app.state.services, Services)
        yield app, client, boundary, provider, headers, key


@pytest.mark.parametrize("invalid", [
    {"temperature": 0.5}, {"model": "unknown@miora"},
    {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "https://example.com/image.png"}}]}]},
    {"tools": [{"type": "function", "function": {
        "name": "tool", "parameters": {"type": "invalid"}}}]},
])
def test_real_http_with_15_second_keepalive_and_full_pacer_rejects_before_post(gateway, invalid):
    app, client, boundary, provider, headers, key = gateway
    identity = provider.credential_from(
        {"access_token": token("preflight-account", "initial")}).identity()
    client.portal.call(provider.pacer.wait_turn, identity)
    started = time.monotonic()
    try:
        response = client.post("/v1/chat/completions", headers=headers,
            json={"model": "glm-5.3@miora", "messages": [{"role": "user", "content": "hi"}],
                  **invalid})
        assert response.status_code == 400, response.text
        assert time.monotonic() - started < 5
        assert "upstream request failed after" not in response.text
        assert not boundary.posts
        assert app.state.services.key_quota.inflight(key["id"]) == 0
        assert app.state.services.user_quota.inflight("root") == 0
    finally:
        client.portal.call(provider.pacer.release, identity)


@pytest.mark.parametrize("model", ["glm-5.3@miora", "friendly", "auto"])
def test_real_http_valid_explicit_and_auto_models_still_reach_upstream(gateway, model):
    app, client, boundary, _provider, headers, key = gateway
    response = client.post("/v1/chat/completions", headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "OK"
    assert boundary.posts == ["/mock-inference"]
    assert app.state.services.key_quota.inflight(key["id"]) == 0


@pytest.mark.parametrize("binding,model", [
    ("", "glm-5.3"), ("miora", "glm-5.3"), ("", "glm-5.3@miora"),
])
async def test_unscoped_requests_do_not_scan_account_rows(binding, model):
    services = service()

    def forbidden_scan(**_kwargs):
        pytest.fail("unscoped validation must not scan the account pool")

    services.credentials.candidates = forbidden_scan
    await preflight_miora(services, chat(model), ApiKeyPrincipal("root", provider_binding=binding))


@pytest.mark.parametrize("model", ["fast", "fast@miora", "missing-member"])
async def test_declared_fallback_groups_and_members_keep_executor_semantics(model):
    services = service()
    services.settings.model_fallback_groups = "fast=glm-5.3,missing-member"
    principal = ApiKeyPrincipal("root", provider_binding="miora")
    await preflight_miora(services, chat(model), principal)
    with pytest.raises(InvalidRequest):
        await preflight_miora(services, chat(model, temperature=0.5), principal)
    with pytest.raises(InvalidRequest, match="does not offer"):
        await preflight_miora(services, chat("undeclared-model"), principal)


@pytest.mark.parametrize("marker", [
    {"type": "image_generation"},
    {"type": "image_generation", "quality": "auto", "size": "1024x1024"},
])
async def test_valid_builtin_media_options_are_not_rejected(marker):
    await preflight_miora(
        service(), chat(miora_image_generation=marker),
        ApiKeyPrincipal("root", provider_binding="miora")
    )


@pytest.mark.parametrize("options", [{"quality": "high"}, {"size": "999x999"}])
def test_responses_builtin_media_options_fail_before_full_pacer_and_keepalive(gateway, options):
    app, client, boundary, provider, headers, key = gateway
    identity = provider.credential_from(
        {"access_token": token("preflight-account", "initial")}).identity()
    client.portal.call(provider.pacer.wait_turn, identity)
    started = time.monotonic()
    try:
        response = client.post("/v1/responses", headers=headers, json={
            "model": "glm-5.3@miora", "input": "Draw a cup",
            "tools": [{"type": "image_generation", **options}],
        })
        assert response.status_code == 400, response.text
        assert time.monotonic() - started < 5
        assert "upstream request failed after" not in response.text
        assert not boundary.posts
        assert app.state.services.key_quota.inflight(key["id"]) == 0
        assert app.state.services.user_quota.inflight("root") == 0
    finally:
        client.portal.call(provider.pacer.release, identity)


def test_responses_valid_builtin_media_options_preserve_normal_text_route(gateway):
    app, client, boundary, _provider, headers, key = gateway
    response = client.post("/v1/responses", headers=headers, json={
        "model": "glm-5.3@miora", "input": "Draw a cup",
        "tools": [{"type": "image_generation", "quality": "auto", "size": "1024x1024"}],
    })
    assert response.status_code == 200, response.text
    assert boundary.posts == ["/mock-inference"]
    assert app.state.services.key_quota.inflight(key["id"]) == 0
