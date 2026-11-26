"""限额和编辑版本的大数字在真实HTTP/SQLite边界拒绝，不能留下半次编辑。"""

import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api.admin_credentials import create_router
from src.auth.users import DbUserStore, create_password_hash
from src.config import Settings
from src.db.crypto import CredentialCipher
from src.db.limits import LimitsRepository
from src.db.repo import AuditRepository, CredentialRepository, UserRepository
from src.webapp.handlers import register_exception_handlers
from tests.api.test_api_key_reveal import headers
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


@pytest.fixture
def numeric_app(db):
    users = UserRepository(db)
    users.create("alice", create_password_hash("synthetic-password"), role="admin")
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    cid = credentials.add(provider="miora", credential_data={"access_token": "synthetic"})
    credentials.apply_credential_edit(cid, allowed_models="original")
    limits = LimitsRepository(db)
    limits.set(cid, daily_tokens=100, updated_by="alice", now=123)
    invalidated = []
    services = SimpleNamespace(settings=Settings(_env_file=None, APP_SECRET=SECRET),
        users=DbUserStore(users), credentials=credentials, registry={},
        schedule_probe=lambda *_: None,
        limits=limits, limit_guard=SimpleNamespace(invalidate=lambda: invalidated.append(1)),
        audit=AuditRepository(db))
    app = FastAPI()
    app.state.services = services
    app.include_router(create_router(services))
    register_exception_handlers(app)
    return SimpleNamespace(app=app, services=services, cid=cid, invalidated=invalidated)


async def put(app, path, payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app,
        raise_app_exceptions=False), base_url="http://testserver") as client:
        return await client.put(f"/api/credentials/{app.cid}/{path}",
            headers=headers(**{"Content-Type": "application/json"}), content=json.dumps(payload))


@pytest.mark.parametrize("payload", [
    {"keep_credits": 10**400}, {"daily_credits": 10**400}, {"daily_tokens": 10**400},
    {"daily_tokens": 2**63}, {"daily_tokens": float(2**63 - 1)},
    {"per_model_tokens": {"model": 10**400}}, {"per_model_tokens": {"model": 2**63}},
    {"expected_updated_at": 2**63}, {"expected_updated_at": 10**400},
    {"keep_credits": float("-inf")}, {"daily_tokens": float("inf")},
    {"daily_credits": float("nan")}, {"per_model_tokens": {"model": float("inf")}},
    {"expected_updated_at": float("inf")}, {"expected_updated_at": float("nan")},
])
async def test_large_limits_or_version_return_400_before_any_write(numeric_app, payload):
    app = numeric_app
    before = app.services.credentials.edit_snapshot(app.cid)
    limits = app.services.limits.get(app.cid).as_dict()
    response = await put(app, "limits", {"allowed_models": "changed", **payload})
    assert app.services.credentials.edit_snapshot(app.cid) == before
    assert app.services.limits.get(app.cid).as_dict() == limits
    assert response.status_code == 400, response.text
    assert app.invalidated == [] and app.services.audit.query() == []


@pytest.mark.parametrize("value", [10**400, -(10**400), float("inf"), float("nan")])
async def test_weight_is_already_strict_and_large_values_are_controlled(numeric_app, value):
    before = numeric_app.services.credentials.edit_snapshot(numeric_app.cid)
    response = await put(numeric_app, "weight", {"weight": value})
    assert response.status_code == 400
    assert numeric_app.services.credentials.edit_snapshot(numeric_app.cid) == before


async def test_valid_max_integer_and_large_finite_credits_remain_supported(numeric_app):
    response = await put(numeric_app, "limits", {"daily_tokens": 2**63 - 1,
        "keep_credits": 10**308, "daily_credits": 1e300,
        "per_model_tokens": {"model": 2**63 - 1, "fraction": 3.9, "off": 0.5}})
    assert response.status_code == 200, response.text
    limits = response.json()["limits"]
    assert limits["daily_tokens"] == 2**63 - 1
    assert limits["keep_credits"] == 1e308 and limits["daily_credits"] == 1e300
    assert limits["per_model_tokens"] == {"model": 2**63 - 1, "fraction": 3}


async def test_negative_large_values_keep_clear_limit_contract(numeric_app):
    value = -(10**400)
    response = await put(numeric_app, "limits", {"keep_credits": value, "daily_credits": value,
        "daily_tokens": value, "per_model_tokens": {"model": value}})
    assert response.status_code == 200 and response.json()["limits"] is None
    assert numeric_app.services.credentials.edit_snapshot(numeric_app.cid)["allowed_models"] == \
        "original"


async def test_largest_storable_version_returns_conflict_without_binding_overflow(numeric_app):
    response = await put(numeric_app, "limits", {"expected_updated_at": 2**63 - 1})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "credential_conflict"


async def test_storage_failure_rolls_back_entire_limits_edit(numeric_app, monkeypatch):
    app = numeric_app
    before = app.services.credentials.edit_snapshot(app.cid)
    limits = app.services.limits.get(app.cid).as_dict()
    original = app.services.limits.set

    def interrupted_write(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("synthetic storage failure")

    monkeypatch.setattr(app.services.limits, "set", interrupted_write)
    response = await put(app, "limits", {"allowed_models": "changed", "daily_tokens": 200})
    assert response.status_code == 500
    assert app.services.credentials.edit_snapshot(app.cid) == before
    assert app.services.limits.get(app.cid).as_dict() == limits
    assert app.invalidated == [] and app.services.audit.query() == []


@pytest.mark.parametrize("value", [False, 0, [], ["model"], {"model": True}])
async def test_non_text_model_policy_cannot_error_or_silently_clear_restriction(numeric_app, value):
    app = numeric_app
    before = app.services.credentials.edit_snapshot(app.cid)
    limits = app.services.limits.get(app.cid).as_dict()
    response = await put(app, "limits", {"allowed_models": value})
    assert app.services.credentials.edit_snapshot(app.cid) == before
    assert app.services.limits.get(app.cid).as_dict() == limits
    assert response.status_code == 400


async def test_explicit_null_model_policy_keeps_existing_clear_contract(numeric_app):
    response = await put(numeric_app, "limits", {"allowed_models": None, "daily_tokens": 100})
    assert response.status_code == 200
    assert response.json()["allowed_models"] == ""
    assert response.json()["limits"]["daily_tokens"] == 100
