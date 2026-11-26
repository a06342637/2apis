"""批量 Miora 导入独立账号、北京时间默认昵称与重导保护；仅假票据。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api import admin_credentials
from src.api.credential_login import save_login_credential
from src.auth.users import DbUserStore, create_password_hash
from src.config import Settings
from src.db.crypto import CredentialCipher
from src.db.repo import AuditRepository, CredentialRepository, UserRepository
from src.provider.miora import MioraProvider
from src.webapp.handlers import register_exception_handlers
from tests.api.test_api_key_reveal import headers
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


@pytest.fixture
def batch_app(db):
    users = UserRepository(db)
    users.create("alice", create_password_hash("synthetic-password"), role="admin")
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    probes = []
    services = SimpleNamespace(settings=Settings(_env_file=None, APP_SECRET=SECRET),
        users=DbUserStore(users), credentials=credentials,
        registry={"miora": MioraProvider(),
                  "codebuddy": SimpleNamespace(import_credential=lambda raw: raw)},
        schedule_probe=lambda cid: probes.append(cid), audit=AuditRepository(db))
    app = FastAPI()
    app.state.services = services
    app.include_router(admin_credentials.create_router(services))
    register_exception_handlers(app)
    return SimpleNamespace(app=app, services=services, probes=probes)


async def import_rows(app, items, *, batch=True):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
        base_url="http://testserver") as client:
        response = await client.post("/api/credentials/batch-import" if batch
            else "/api/credentials", headers=headers(), json={"items": items} if batch else items)
    assert response.status_code == 200, response.text
    return response.json()


def item(token, nickname="", **credential):
    return {"provider": "miora", "nickname": nickname,
            "credential": {"authToken": token, **credential}}


def names(app):
    return {row["id"]: row["nickname"] for row in app.services.credentials.list_all()}


def freeze_clock(monkeypatch):
    timezones = []

    class Clock:
        @staticmethod
        def now(tz):
            timezones.append(tz.key)
            return datetime(2026, 11, 25, 18, 15, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(admin_credentials, "datetime", Clock)
    return timezones


async def test_default_batch_names_use_beijing_time_and_only_unnamed_new_sequence(
        batch_app, monkeypatch):
    zones = freeze_clock(monkeypatch)
    response = await import_rows(batch_app, [
        item("synthetic-first", "  "),
        item("synthetic-email", " mail@example.com "),
        item("synthetic-first"),
        {"provider": "codebuddy", "credential": {"token": "synthetic-other"}},
        {"provider": "unknown", "credential": {}},
        item("synthetic-last"),
    ])
    assert zones == ["Asia/Shanghai"]
    assert [row["index"] for row in response["created"]] == [0, 1, 3, 5]
    assert response["skipped"] == [{"index": 2, "reason": "duplicate"}]
    assert response["failed"][0]["index"] == 4
    assert list(names(batch_app).values()) == [
        "20261126-021530-001", "mail@example.com", "", "20261126-021530-002"]
    assert len(batch_app.probes) == 4


async def test_reimport_keeps_custom_names_and_explicit_email_can_rename(batch_app, monkeypatch):
    freeze_clock(monkeypatch)
    first = await import_rows(batch_app, [item("synthetic-old", "my custom name", uid="user-1")])
    cid = first["created"][0]["id"]
    response = await import_rows(batch_app, [
        item("synthetic-renewed", uid="user-1"), item("synthetic-new-account"),
    ])
    assert response["created"][0]["id"] == cid
    assert response["created"][0]["merged"] is True
    assert names(batch_app)[cid] == "my custom name"
    assert list(names(batch_app).values())[1] == "20261126-021530-001"
    response = await import_rows(batch_app, [
        item("synthetic-renewed", "new@example.com", uid="user-1")])
    assert response["created"][0]["id"] == cid and response["created"][0]["merged"] is True
    assert names(batch_app)[cid] == "new@example.com"
    skipped = await import_rows(batch_app, [item("synthetic-renewed", uid="user-1")])
    assert skipped["created"] == [] and skipped["skipped"][0]["index"] == 0
    assert names(batch_app)[cid] == "new@example.com"


async def test_opaque_exact_reimport_renames_one_row_but_email_does_not_guess_identity(batch_app):
    first = await import_rows(batch_app, [item("synthetic-opaque", "old-name", refreshToken="r1")])
    cid = first["created"][0]["id"]
    response = await import_rows(batch_app, [
        item("synthetic-opaque", "mail@example.com", refreshToken="r1"),
        item("synthetic-opaque", "duplicate@example.com", refreshToken="r1"),
        item("synthetic-different", "mail@example.com", refreshToken="r2"),
    ])
    assert response["created"][0]["id"] == cid and response["created"][0]["merged"] is True
    assert response["skipped"] == [{"index": 1, "reason": "duplicate"}]
    assert names(batch_app)[cid] == "mail@example.com"
    assert response["created"][1]["id"] != cid
    assert len(names(batch_app)) == 2
    assert batch_app.services.credentials.credential_data(cid)["refresh_token"] == "r1"


async def test_single_import_and_non_miora_batch_keep_old_nickname_contract(batch_app, monkeypatch):
    def unexpected_default():
        raise AssertionError("single import must not create a batch name factory")

    with monkeypatch.context() as context:
        context.setattr(admin_credentials, "_miora_batch_nickname_factory", unexpected_default)
        result = await import_rows(batch_app, item("synthetic-single"), batch=False)
    assert names(batch_app)[result["id"]] == ""
    response = await import_rows(batch_app, [
        {"provider": "codebuddy", "credential": {"token": "same"}, "nickname": "first"},
    ])
    cid = response["created"][0]["id"]
    response = await import_rows(batch_app, [
        {"provider": "codebuddy", "credential": {"token": "same"}, "nickname": "new name"},
    ])
    assert response["created"] == [] and response["skipped"][0]["reason"] == "duplicate"
    assert names(batch_app)[cid] == "first"


async def test_login_default_callback_only_runs_for_new_unnamed_rows(batch_app, monkeypatch):
    services = batch_app.services
    called = []

    def default_name():
        called.append(1)
        return f"default-{len(called)}"

    fresh, merged = await save_login_credential(services, provider_id="miora",
        credential_data={"access_token": "opaque-one"}, nickname="", added_by="alice",
        new_nickname=default_name)
    assert merged is False and names(batch_app)[fresh] == "default-1"
    explicit, merged = await save_login_credential(services, provider_id="miora",
        credential_data={"uid": "user-one", "access_token": "old"}, nickname="custom",
        added_by="alice", new_nickname=default_name)
    assert merged is False and called == [1]
    updated, merged = await save_login_credential(services, provider_id="miora",
        credential_data={"uid": "user-one", "access_token": "new"}, nickname="",
        added_by="alice", new_nickname=default_name, merge_identical=True)
    assert updated == explicit and merged is True
    assert names(batch_app)[updated] == "custom" and called == [1]
    with monkeypatch.context() as context:
        context.setattr(services.credentials, "find_login_credential", lambda *_: "vanished")
        replacement, merged = await save_login_credential(services, provider_id="miora",
            credential_data={"uid": "user-two"}, nickname="", added_by="alice",
            new_nickname=default_name)
    assert replacement != "vanished" and merged is False
    assert names(batch_app)[replacement] == "default-2"


async def test_opt_in_exact_merge_is_canonical_and_default_does_not_rename(batch_app):
    services = batch_app.services
    old = services.credentials.add(provider="miora",
        credential_data={"refresh_token": "refresh", "access_token": "opaque"}, nickname="custom")

    def must_not_name():
        raise AssertionError("merge must not generate default nickname")

    merged_id, merged = await save_login_credential(services, provider_id="miora",
        credential_data={"access_token": "opaque", "refresh_token": "refresh"},
        nickname="", added_by="alice", new_nickname=must_not_name, merge_identical=True)
    assert merged_id == old and merged is True
    assert names(batch_app)[old] == "custom"
    # 只要认证内容不同就保留独立账号；相同邮箱不作为合并依据。
    new_id, merged = await save_login_credential(services, provider_id="miora",
        credential_data={"access_token": "opaque", "refresh_token": "different"},
        nickname="custom", added_by="alice", merge_identical=True)
    assert new_id != old and merged is False
