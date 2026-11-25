import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

from src.media import storage as mod
from src.media.storage import MediaStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    secured = []
    # 私有权限的 Windows/POSIX 实现另有完整测试；这里只验证存储层调用保护入口。
    monkeypatch.setattr(mod, "secure_dir", lambda path: secured.append(("dir", path)))
    monkeypatch.setattr(mod, "secure_file", lambda path: secured.append(("file", path)))
    clock = [100]
    value = MediaStore(tmp_path / "media", "test-secret", ttl=60, clock=lambda: clock[0])
    return value, clock, secured


def test_create_load_and_atomic_replacement_include_timestamps_and_private_file_calls(store):
    value, clock, secured = store
    job = value.create(key_id="key-a", credential_id="account-a")
    assert job["status"] == "submitting" and job["files"] == []
    assert job["created_at"] == job["updated_at"] == 100
    assert value.load(job["id"]) == job
    clock[0] = 110
    updated = value.save({**job, "status": "completed", "files": [{"file_key": "file/a"}]})
    assert value.load(job["id"]) == updated
    assert updated["updated_at"] == 110 and updated["created_at"] == 100
    assert [kind for kind, _ in secured] == ["dir", "file", "file"]
    assert all(path.suffix == ".tmp" for kind, path in secured if kind == "file")
    assert len(list(value.directory.iterdir())) == 1


@pytest.mark.parametrize("job_id", [None, "", "../outside", "media_a", "media_" + "A" * 32])
def test_job_paths_reject_malformed_or_traversal_ids(store, job_id):
    with pytest.raises(HTTPException) as error:
        store[0].path(job_id)
    assert error.value.status_code == 404


def test_nonexistent_and_symlink_jobs_are_not_read(store, monkeypatch):
    value = store[0]
    missing = "media_" + "a" * 32
    with pytest.raises(HTTPException):
        value.load(missing)
    job = value.create(key_id="key-a")
    monkeypatch.setattr(type(value.path(job["id"])), "is_symlink", lambda self: True)
    with pytest.raises(HTTPException):
        value.load(job["id"])


def test_failed_serialization_cannot_replace_committed_job(store):
    value = store[0]
    job = value.create(key_id="key-a")
    with pytest.raises(ValueError):
        value.save({**job, "progress": float("nan")})
    assert value.load(job["id"]) == job
    assert list(value.directory.iterdir()) == [value.path(job["id"])]


def test_invalid_json_is_not_misreported_as_an_empty_job(store):
    value = store[0]
    job = value.create(key_id="key-a")
    value.path(job["id"]).write_text("{invalid", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        value.load(job["id"])


def test_idempotency_intent_ids_bind_key_and_nonce_and_can_be_used_on_creation(store):
    value = store[0]
    job_id = value.intent_id("key-a", "request-1")
    assert value.intent_id("key-a", "request-1") == job_id
    assert value.intent_id("key-b", "request-1") != job_id
    assert value.intent_id("key-a", "request-2") != job_id
    assert value.create(job_id=job_id, key_id="key-a")["id"] == job_id
    assert value.path(job_id).is_file()


@pytest.mark.parametrize("nonce", [None, 1, "", "a" * 129, "非ASCII"])
def test_idempotency_key_validation(store, nonce):
    with pytest.raises(HTTPException) as error:
        store[0].intent_id("key-a", nonce)
    assert error.value.status_code == 400


@pytest.mark.parametrize("index", [True, "0", -1, 16])
def test_asset_index_cannot_escape_job_namespace(store, index):
    value = store[0]
    job = value.create(key_id="key-a")
    with pytest.raises(HTTPException) as error:
        value.asset(job["id"], index)
    assert error.value.status_code == 404


def test_signed_url_is_ttl_bound_and_verifies_against_exact_key_job_and_index(store):
    value, clock, _ = store
    job = value.create(key_id="key-a")
    assert value.asset(job["id"], 15).name.endswith("-15.bin")
    url = value.signed_url(job, "https://gateway.example/")
    assert urlsplit(url).path == f"/v1/media/{job['id']}/content"
    query = parse_qs(urlsplit(url).query)
    signature = query["signature"][0]
    assert query["expires"] == ["160"]
    value.verify_signature(job, 0, 160, signature)
    for modified, index in [({**job, "key_id": "other"}, 0), (job, 1)]:
        with pytest.raises(HTTPException):
            value.verify_signature(modified, index, 160, signature)
    clock[0] = 160
    with pytest.raises(HTTPException):
        value.verify_signature(job, 0, 160, signature)


@pytest.mark.parametrize(
    "expires, signature",
    [(100, "a" * 64), (161, "a" * 64), (160, None), (160, "bad"), (160, "A" * 64), (160, "0" * 64)],
)
def test_invalid_or_overlong_expiry_and_signature_are_rejected(store, expires, signature):
    value = store[0]
    job = value.create(key_id="key-a")
    with pytest.raises(HTTPException) as error:
        value.verify_signature(job, 0, expires, signature)
    assert error.value.status_code == 403
