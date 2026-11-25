"""当前密码复核后才返回所属 Key，整个链使用真实 SQLite/加密/鉴权。"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from src.api.admin_keys import create_router
from src.audit.actions import ACTION_API_KEY_REVEAL
from src.auth.session import SESSION_COOKIE, create_session_token
from src.auth.throttle import LoginThrottle
from src.auth.users import DbUserStore, create_password_hash
from src.config import Settings
from src.db.crypto import CredentialCipher
from src.db.repo import ApiKeyRepository, AuditRepository, UserRepository
from src.webapp.handlers import register_exception_handlers
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET

PASSWORD = "reveal-test-password"


@pytest.fixture
def reveal_app(db):
    users = UserRepository(db)
    users.create("alice", create_password_hash(PASSWORD), role="admin")
    users.create("bob", create_password_hash(PASSWORD), role="viewer")
    cipher = CredentialCipher(SECRET)
    keys = ApiKeyRepository(db, cipher)
    alice = keys.create("alice", "recoverable")
    bob = keys.create("bob", "other owner")
    old = ApiKeyRepository(db).create("alice", "legacy")
    services = SimpleNamespace(settings=Settings(_env_file=None, APP_SECRET=SECRET),
        api_keys=keys, users=DbUserStore(users), user_repo=users,
        login_throttle=LoginThrottle(), audit=AuditRepository(db))
    app = FastAPI()
    app.state.services = services
    app.include_router(create_router(services))
    register_exception_handlers(app)
    return SimpleNamespace(app=app, services=services, keys=keys, alice=alice, bob=bob, old=old,
                           db=db, cipher=cipher)


def headers(username="alice", **extra):
    return {"Cookie": f"{SESSION_COOKIE}=" + create_session_token(username, SECRET),
            "X-Requested-With": "XMLHttpRequest", **extra}


async def request(app, key_id, payload=None, *, request_headers=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app,
        client=("198.51.100.1", 1234)), base_url="http://testserver") as client:
        return await client.post(f"/api/api-keys/{key_id}/reveal",
            headers=headers() if request_headers is None else request_headers,
            json={"password": PASSWORD} if payload is None else payload)


async def test_correct_password_reveals_only_key_and_records_redacted_audit(reveal_app):
    app = reveal_app
    response = await request(app, app.alice["id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"api_key": app.alice["api_key"]}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    events = app.services.audit.query(action=ACTION_API_KEY_REVEAL)
    assert len(events) == 1 and events[0]["ok"]
    assert events[0]["target"] == app.alice["id"] and events[0]["actor"] == "alice"
    assert PASSWORD not in str(events) and app.alice["api_key"] not in str(events)


async def test_wrong_password_keeps_key_and_session_and_never_echoes_secrets(reveal_app):
    app = reveal_app
    wrong = "wrong-do-not-echo-password"
    response = await request(app, app.alice["id"], {"password": wrong})
    assert response.status_code == 401 and response.json()["error"]["code"] == "invalid_password"
    assert wrong not in response.text and app.alice["api_key"] not in response.text
    assert app.keys.authenticate(app.alice["api_key"])["username"] == "alice"
    assert (await request(app, app.alice["id"])).status_code == 200
    events = app.services.audit.query(action=ACTION_API_KEY_REVEAL)
    assert len(events) == 2 and {event["ok"] for event in events} == {False, True}


@pytest.mark.parametrize("password", [None, "", 42, False, [], "x" * 8193])
async def test_invalid_password_input_rejected_without_hash_work(reveal_app, password, monkeypatch):
    monkeypatch.setattr(reveal_app.services.users, "verify",
                        lambda *_: pytest.fail("must not hash"))
    response = await request(reveal_app, reveal_app.alice["id"], {"password": password})
    assert response.status_code == 400


@pytest.mark.parametrize("kind", ["missing", "other-owner", "deleted"])
async def test_admin_cannot_reveal_another_owner_or_deleted_key(reveal_app, kind, monkeypatch):
    app = reveal_app
    key_id = app.bob["id"] if kind == "other-owner" else app.alice["id"]
    if kind == "missing":
        key_id = "key-missing"
    elif kind == "deleted":
        app.keys.delete(key_id, "alice")
    monkeypatch.setattr(app.services.users, "verify", lambda *_: pytest.fail("must not hash"))
    response = await request(app, key_id)
    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert app.bob["api_key"] not in response.text


async def test_viewer_can_reveal_own_key_with_current_password(reveal_app):
    response = await request(reveal_app, reveal_app.bob["id"], request_headers=headers("bob"))
    assert response.status_code == 200
    assert response.json()["api_key"] == reveal_app.bob["api_key"]


async def test_old_key_is_explicitly_unrecoverable_without_changing_digest(reveal_app):
    app = reveal_app
    response = await request(app, app.old["id"])
    assert response.status_code == 409 and response.json()["error"]["code"] == "key_not_recoverable"
    assert app.keys.authenticate(app.old["api_key"])["username"] == "alice"


@pytest.mark.parametrize("kind", ["ciphertext", "unicode", "digest"])
async def test_corrupted_ciphertext_never_reaches_response(reveal_app, kind):
    app = reveal_app
    value = {"ciphertext": b"bad", "unicode": app.cipher.encrypt(b"\xff"),
             "digest": app.cipher.encrypt(b"secret-that-does-not-match")}[kind]
    app.db.connect().execute("UPDATE api_keys SET key_enc=? WHERE id=?", (value, app.alice["id"]))
    app.db.connect().commit()
    response = await request(app, app.alice["id"])
    assert response.status_code == 409 and response.json()["error"]["code"] == "key_not_recoverable"
    assert app.alice["api_key"] not in response.text


async def test_throttle_stops_password_bruteforce_and_returns_retry_after(reveal_app):
    app = reveal_app
    for _ in range(5):
        assert (await request(app, app.alice["id"], {"password": "wrong"})).status_code == 401
    limited = await request(app, app.alice["id"], {"password": "wrong"})
    assert limited.status_code == 429 and limited.json()["error"]["code"] == "rate_limited"
    assert limited.headers["Retry-After"] == "60"
    # 用户名窗口不是哈希前硬拦截，正确登录密码仍可解除别人抬高的用户名计数。
    assert (await request(app, app.alice["id"])).status_code == 200


async def test_transport_throttle_runs_before_cpu_hashing(reveal_app, monkeypatch):
    app = reveal_app
    for index in range(8):
        app.services.login_throttle.record_failure(ip="198.51.100.1", username=f"other-{index}")
    monkeypatch.setattr(app.services.users, "verify", lambda *_: pytest.fail("must not hash"))
    response = await request(app, app.alice["id"])
    assert response.status_code == 429


@pytest.mark.parametrize("kind", ["missing-session", "expired-session", "disabled-user", "csrf"])
async def test_session_and_csrf_gate_key_reveal(reveal_app, kind):
    app = reveal_app
    request_headers = headers()
    if kind == "missing-session":
        request_headers = {"X-Requested-With": "XMLHttpRequest"}
    elif kind == "expired-session":
        request_headers["Cookie"] = (f"{SESSION_COOKIE}="
                                     + create_session_token("alice", SECRET, issued_at=1))
    elif kind == "disabled-user":
        app.db.connect().execute("UPDATE users SET enabled=0 WHERE username='alice'")
        app.db.connect().commit()
    else:
        request_headers["Origin"] = "https://attacker.invalid"
    response = await request(app, app.alice["id"], request_headers=request_headers)
    assert response.status_code == (403 if kind == "csrf" else 401)
    assert app.alice["api_key"] not in response.text


async def test_session_epoch_is_rechecked_after_async_password_verification(
    reveal_app, monkeypatch,
):
    app = reveal_app
    original = app.services.users.verify
    def verify_then_logout(username, password):
        result = original(username, password)
        app.services.users.bump_session_epoch(username)
        return result
    monkeypatch.setattr(app.services.users, "verify", verify_then_logout)
    response = await request(app, app.alice["id"])
    assert response.status_code == 401
    assert app.alice["api_key"] not in response.text
    assert app.services.audit.query(action=ACTION_API_KEY_REVEAL) == []
