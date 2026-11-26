"""选中账号导出：真实权限 / CSRF、可重导入与全有或全无的容量边界。"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import credential_export
from src.api.admin_credentials import create_router
from src.api.credential_export import (
    BATCH_EXPORT_MAX_BYTES,
    BATCH_EXPORT_MAX_IDS,
    encode_credential_export,
    validate_export_ids,
)
from src.api.deps import principal_from_request
from src.audit.actions import ACTION_CREDENTIAL_EXPORT
from src.auth.rbac import Principal, UnauthorizedError
from src.compat.openai.request import InvalidRequest
from src.db.crypto import CredentialCipher
from src.db.repo import AuditRepository, CredentialRepository
from src.webapp.handlers import register_exception_handlers
from tests.db.test_api_key_reveal import db as db
from tests.helpers.constants import SECRET


class _ImportProvider:
    """只由批量导入调用，不实现任何刷新 / 探测接口，防止导出误出网。"""

    def import_credential(self, raw):
        return dict(raw)


@pytest.fixture
def export_app(db):
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    audit = AuditRepository(db)
    probes: list[str] = []
    services = SimpleNamespace(credentials=credentials, audit=audit,
        registry={"codebuddy": _ImportProvider()}, schedule_probe=probes.append)
    app = FastAPI()
    app.include_router(create_router(services))
    register_exception_handlers(app)
    app.dependency_overrides[principal_from_request] = lambda: Principal("root", True)
    with TestClient(app) as client:
        yield app, client, credentials, audit, probes


def _post(client, ids):
    return client.post("/api/credentials/export", json={"ids": ids})


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_selected_subset_order_duplicates_stripping_and_sanitized_audit(
        export_app, db, caplog, role):
    app, client, repository, audit, probes = export_app
    app.dependency_overrides[principal_from_request] = lambda: Principal("exporter", role=role)
    client.cookies.set("coding2api_session", "session")
    entries = []
    for provider in ("miora", "qoder", "codearts"):
        entries.append(repository.add(provider=provider, nickname=f"{provider} 私密昵称",
            tags=["所选", provider], credential_data={"authToken": f"secret-{provider}",
            "refreshToken": f"refresh-{provider}", "password": "private-password",
            "web_cookie": "private-web-cookie", "phone": "13900000000"}))
    unselected = repository.add(provider="miora", credential_data={"authToken": "never-export"})
    before = db.connect().execute("SELECT id,data_enc FROM credentials ORDER BY id").fetchall()
    with caplog.at_level("INFO"):
        response = client.post("/api/credentials/export",
            json={"ids": [entries[2], entries[0], entries[2], entries[1]]},
            headers={"Origin": "http://testserver", "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/json"
    assert response.headers["Content-Disposition"] == (
        'attachment; filename="coding2api-credentials.json"')
    assert response.headers["Cache-Control"] == "no-store"
    exported = response.json()
    assert exported == [repository.export_entry(cid) for cid in (
        entries[2], entries[0], entries[1])]
    assert all(set(item) == {"provider", "nickname", "tags", "credential"}
               for item in exported)
    assert all("password" not in item["credential"] for item in exported)
    assert "web_cookie" not in exported[0]["credential"]
    assert exported[1]["credential"]["web_cookie"] == "private-web-cookie"
    assert "web_cookie" not in exported[2]["credential"]
    assert "never-export" not in response.text
    assert probes == []
    assert [tuple(row) for row in before] == [tuple(row) for row in
        db.connect().execute("SELECT id,data_enc FROM credentials ORDER BY id").fetchall()]
    events = audit.query(action=ACTION_CREDENTIAL_EXPORT)
    assert len(events) == 1
    assert events[0]["actor"] == "exporter" and events[0]["target"] is None
    assert json.loads(events[0]["detail"]) == {
        "count": 3, "ids": [entries[2], entries[0], entries[1]]}
    private_audit = json.dumps(events, ensure_ascii=False) + caplog.text
    for secret in ("secret-", "refresh-", "private-password", "private-web-cookie",
                   "私密昵称", "13900000000", unselected):
        assert secret not in private_audit


@pytest.mark.parametrize("principal,status", [
    (Principal("viewer", role="viewer"), 403), (None, 401)])
def test_export_rejects_viewer_and_unauthenticated_before_decryption(
        export_app, monkeypatch, principal, status):
    app, client, repository, audit, _ = export_app

    def authenticate():
        if principal is None:
            raise UnauthorizedError()
        return principal

    app.dependency_overrides[principal_from_request] = authenticate
    monkeypatch.setattr(repository, "export_entry", lambda _cid: pytest.fail("must not decrypt"))
    response = _post(client, ["selected"])
    assert response.status_code == status
    assert audit.query() == []


@pytest.mark.parametrize("headers", [
    {"Origin": "https://attacker.example"},
    {"Referer": "https://attacker.example/form"},
    {"Origin": "https://attacker.example", "X-Requested-With": "XMLHttpRequest"}])
def test_export_enforces_real_session_csrf(export_app, monkeypatch, headers):
    _app, client, repository, audit, _ = export_app
    client.cookies.set("coding2api_session", "session")
    monkeypatch.setattr(repository, "export_entry", lambda _cid: pytest.fail("must not decrypt"))
    response = client.post("/api/credentials/export", json={"ids": ["selected"]},
                           headers=headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "forbidden"
    assert response.json()["error"]["message"] == "cross-origin write rejected"
    assert audit.query() == []


@pytest.mark.parametrize("payload,message", [
    ({}, "请选择至少一个凭证账号后重试"),
    ({"ids": None}, "请选择至少一个凭证账号后重试"),
    ({"ids": []}, "请选择至少一个凭证账号后重试"),
    ({"ids": "selected"}, "请选择至少一个凭证账号后重试"),
    ({"ids": {}}, "请选择至少一个凭证账号后重试"),
    ({"ids": ["selected", None]}, "所选凭证无效，请刷新列表后重新选择"),
    ({"ids": ["selected", 0]}, "所选凭证无效，请刷新列表后重新选择"),
    ({"ids": ["selected", True]}, "所选凭证无效，请刷新列表后重新选择"),
    ({"ids": ["selected", {}]}, "所选凭证无效，请刷新列表后重新选择"),
    ({"ids": ["selected", []]}, "所选凭证无效，请刷新列表后重新选择"),
    ({"ids": ["selected", ""]}, "所选凭证无效，请刷新列表后重新选择"),
    ({"ids": ["selected", " \t\n"]}, "所选凭证无效，请刷新列表后重新选择"),
    ({"ids": ["selected"] * (BATCH_EXPORT_MAX_IDS + 1)},
     "最多导出 5000 个凭证账号，请减少选择后重试")])
def test_export_strictly_rejects_invalid_ids_before_decrypting(
        export_app, monkeypatch, payload, message):
    _app, client, repository, audit, _ = export_app
    monkeypatch.setattr(repository, "export_entry", lambda _cid: pytest.fail("must not decrypt"))
    response = client.post("/api/credentials/export", json=payload)
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["message"] == message
    assert audit.query() == []


def test_exact_id_limit_and_duplicate_selection_reads_only_once(export_app, monkeypatch):
    _app, client, repository, audit, _ = export_app
    cid = repository.add(provider="miora", credential_data={"authToken": "selected"})
    original = repository.export_entry
    calls = []

    def read(selected):
        calls.append(selected)
        return original(selected)

    monkeypatch.setattr(repository, "export_entry", read)
    response = _post(client, [cid] * BATCH_EXPORT_MAX_IDS)
    assert response.status_code == 200 and len(response.json()) == 1
    assert calls == [cid]
    assert json.loads(audit.query()[0]["detail"]) == {"count": 1, "ids": [cid]}


def test_full_unique_id_limit_and_exact_unmodified_identifiers():
    ids = [f"id-{index}" for index in range(BATCH_EXPORT_MAX_IDS)]
    calls = []

    def read(cid):
        calls.append(cid)
        return {"provider": "miora", "nickname": cid, "tags": [], "credential": {"t": cid}}

    response = encode_credential_export(SimpleNamespace(export_entry=read),
                                        validate_export_ids({"ids": ids}))
    assert calls == ids
    assert [entry["nickname"] for entry in json.loads(response)] == ids
    assert validate_export_ids({"ids": [" id ", "id", " id "]}) == [" id ", "id"]


@pytest.mark.parametrize("missing", ["missing-token-secret", " nonexistent "])
def test_missing_later_item_aborts_without_partial_file_or_secret_echo(
        export_app, caplog, missing):
    _app, client, repository, audit, probes = export_app
    cid = repository.add(provider="miora", credential_data={"authToken": "must-stay-private"})
    response = _post(client, [cid, missing])
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "所选凭证不存在，请刷新列表后重新选择并重试"
    assert "Content-Disposition" not in response.headers
    assert "must-stay-private" not in response.text + caplog.text
    assert missing not in response.text + caplog.text
    assert audit.query() == [] and probes == []


@pytest.mark.parametrize("corruption", [
    "ciphertext", "json", "unicode", "list", "integer", "nonfinite", "surrogate", "recursion"])
def test_later_decryption_or_storage_corruption_aborts_and_sanitizes(
        export_app, db, caplog, corruption):
    _app, client, repository, audit, probes = export_app
    valid = repository.add(provider="miora", credential_data={"authToken": "first-private"})
    broken = repository.add(provider="miora", credential_data={"authToken": "second-private"})
    plaintext = {"json": b"secret-invalid-json", "unicode": b"\xff",
                 "list": b"[]", "integer": b"42", "nonfinite": b'{"token":NaN}',
                 "surrogate": b'{"token":"\\ud800"}',
                 "recursion": b'{"token":' + b"[" * (2 * sys.getrecursionlimit())
                    + b'"private-recursion"' + b"]" * (2 * sys.getrecursionlimit()) + b"}"}
    encrypted = (b"broken-secret-ciphertext" if corruption == "ciphertext"
                 else CredentialCipher(SECRET).encrypt(plaintext[corruption]))
    db.connect().execute("UPDATE credentials SET data_enc=? WHERE id=?", (encrypted, broken))
    db.connect().commit()
    response = _post(client, [valid, broken])
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "无法导出所选凭证，请检查凭证状态后重试"
    assert "Content-Disposition" not in response.headers
    assert "private" not in response.text + caplog.text
    assert "secret" not in response.text + caplog.text
    assert audit.query() == [] and probes == []


def test_deep_encoding_recursion_is_controlled_without_partial_export(
        export_app, monkeypatch, caplog):
    _app, client, repository, audit, probes = export_app
    nested = {"token": "private-recursion"}
    for _ in range(2 * sys.getrecursionlimit()):
        nested = {"child": nested}

    def read(cid):
        return {"credential": {"token": "private-first"}} if cid == "first" else nested

    monkeypatch.setattr(repository, "export_entry", read)
    response = _post(client, ["first", "deep"])
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "无法导出所选凭证，请检查凭证状态后重试"
    assert "Content-Disposition" not in response.headers
    assert "private" not in response.text + caplog.text
    assert audit.query() == [] and probes == []


def test_export_roundtrips_through_existing_batch_import(export_app):
    _app, client, repository, audit, probes = export_app
    cid = repository.add(provider="codebuddy", nickname="可重导入", tags=["one", "two"],
        credential_data={"bearer_token": "roundtrip-private", "password": "never-import"})
    exported = _post(client, [cid]).json()
    assert probes == []
    assert repository.delete(cid)
    imported = client.post("/api/credentials/batch-import", json={"items": exported})
    assert imported.status_code == 200
    result = imported.json()
    assert result["skipped"] == [] and result["failed"] == [] and len(result["created"]) == 1
    new_id = result["created"][0]["id"]
    assert repository.export_entry(new_id) == exported[0]
    assert repository.credential_data(new_id) == {"bearer_token": "roundtrip-private"}
    assert probes == [new_id]
    assert len(audit.query(action=ACTION_CREDENTIAL_EXPORT)) == 1


@pytest.mark.parametrize("extra", [0, 1])
def test_real_16_mib_encoded_boundary(extra):
    entry = {"provider": "miora", "nickname": "", "tags": [], "credential": {"authToken": ""}}
    overhead = len(json.dumps([entry], ensure_ascii=False, separators=(",", ":")).encode())
    entry["credential"]["authToken"] = "x" * (BATCH_EXPORT_MAX_BYTES - overhead + extra)
    repository = SimpleNamespace(export_entry=lambda _cid: entry)
    if extra:
        with pytest.raises(InvalidRequest, match="超过 16 MiB，请减少选择后重试"):
            encode_credential_export(repository, ["selected"])
    else:
        encoded = encode_credential_export(repository, ["selected"])
        assert len(encoded) == BATCH_EXPORT_MAX_BYTES and json.loads(encoded) == [entry]


@pytest.mark.parametrize("text", ["中文🙂" * 4000, "a" * 25000, "\\\n\t\"" * 4000])
def test_utf8_and_json_escape_limits_use_encoded_bytes(export_app, monkeypatch, text):
    _app, client, repository, audit, _ = export_app
    entry = {"provider": "miora", "nickname": "", "tags": [], "credential": {"authToken": text}}
    expected = json.dumps([entry], ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
    monkeypatch.setattr(repository, "export_entry", lambda _cid: entry)
    monkeypatch.setattr(credential_export, "BATCH_EXPORT_MAX_BYTES", len(expected))
    success = _post(client, ["selected"])
    assert success.status_code == 200 and success.content == expected
    assert len(audit.query()) == 1
    monkeypatch.setattr(credential_export, "BATCH_EXPORT_MAX_BYTES", len(expected) - 10)
    failure = _post(client, ["selected"])
    assert failure.status_code == 400
    assert failure.json()["error"]["message"] == "导出文件超过 16 MiB，请减少选择后重试"
    assert "Content-Disposition" not in failure.headers
    assert len(audit.query()) == 1


def test_oversized_encoding_chunk_is_rejected_before_allocating_utf8_bytes(monkeypatch):
    monkeypatch.setattr(credential_export, "BATCH_EXPORT_MAX_BYTES", 100)
    repository = SimpleNamespace(export_entry=lambda _cid: {"token": "x" * 101})
    with pytest.raises(InvalidRequest, match="超过 16 MiB，请减少选择后重试"):
        encode_credential_export(repository, ["selected"])


def test_non_ascii_encoding_chunk_cannot_bypass_byte_limit(monkeypatch):
    monkeypatch.setattr(credential_export, "BATCH_EXPORT_MAX_BYTES", 100)
    repository = SimpleNamespace(export_entry=lambda _cid: {"token": "中" * 40})
    with pytest.raises(InvalidRequest, match="超过 16 MiB，请减少选择后重试"):
        encode_credential_export(repository, ["selected"])
