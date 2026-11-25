"""可恢复 Key 只存加密文本，旧摘要库升级不伪造明文。"""

import pytest

from src.auth.api_key import digest_api_key
from src.db.conn import Database
from src.db.crypto import CredentialCipher, CredentialDecryptError
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import ApiKeyRepository
from tests.helpers.constants import SECRET


@pytest.fixture
def db(tmp_path, monkeypatch):
    # 权限收口由专用全平台测试覆盖，本例隔离数据库内容与加密行为。
    monkeypatch.setattr("src.db.conn.secure_dir", lambda *_a, **_k: True)
    monkeypatch.setattr("src.db.conn.secure_file", lambda *_a, **_k: True)
    database = Database(tmp_path / "reveal.sqlite3", strict_permissions=False)
    apply_schema(database.connect())
    yield database
    database.close()


def test_encrypted_key_owner_digest_and_public_metadata(db):
    cipher = CredentialCipher(SECRET)
    keys = ApiKeyRepository(db, cipher)
    key = keys.create("alice", "recoverable")
    stored = db.connect().execute("SELECT key_enc FROM api_keys").fetchone()[0]
    assert key["api_key"].encode() not in stored
    assert keys.reveal(key["id"], "alice") == key["api_key"]
    assert keys.reveal(key["id"], "bob") is None
    assert keys.reveal_info(key["id"], "bob") is None
    assert keys.reveal_info(key["id"], "alice") == {"id": key["id"], "can_reveal": True}
    for limit in (None, 1):
        public = keys.list_for("alice", limit=limit)[0]
        assert public["can_reveal"] is True
        assert "key_enc" not in public and "key_digest" not in public and "api_key" not in public
    assert keys.authenticate(key["api_key"])["username"] == "alice"


def test_old_unencrypted_keys_and_unconfigured_cipher(db):
    old = ApiKeyRepository(db)
    key = old.create("alice")
    assert old.reveal(key["id"], "alice") is None
    assert old.reveal_info(key["id"], "alice")["can_reveal"] is False
    assert old.list_for("alice")[0]["can_reveal"] is False
    encrypted = ApiKeyRepository(db, CredentialCipher(SECRET))
    assert encrypted.reveal(key["id"], "alice") is None
    assert encrypted.list_for("alice")[0]["can_reveal"] is False
    second = encrypted.create("alice")
    assert old.reveal(second["id"], "alice") is None
    assert old.reveal_info(second["id"], "alice")["can_reveal"] is False
    assert old.list_for("alice")[1]["can_reveal"] is False


@pytest.mark.parametrize("corruption", ["ciphertext", "digest", "non-key", "unicode"])
def test_ciphertext_and_digest_tampering_does_not_reveal_a_key(db, corruption):
    cipher = CredentialCipher(SECRET)
    keys = ApiKeyRepository(db, cipher)
    key = keys.create("alice")
    if corruption == "digest":
        db.connect().execute("UPDATE api_keys SET key_digest=?", (digest_api_key("wrong"),))
    else:
        value = {"ciphertext": b"broken", "non-key": cipher.encrypt(b"a different secret"),
                 "unicode": cipher.encrypt(b"\xff")}[corruption]
        db.connect().execute("UPDATE api_keys SET key_enc=?", (value,))
    db.connect().commit()
    with pytest.raises((CredentialDecryptError, UnicodeDecodeError)):
        keys.reveal(key["id"], "alice")


def test_schema_41_migration_preserves_old_digest_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "legacy-keys.sqlite3", strict_permissions=False)
    conn = db.connect()
    apply_schema(conn)
    conn.execute("ALTER TABLE api_keys DROP COLUMN key_enc")
    conn.execute("PRAGMA user_version=40")
    conn.execute("INSERT INTO api_keys(id,username,key_digest,preview,created_at,credential_ids) "
                 "VALUES ('old','alice','old-digest','preview',12,'[\"selected\"]')")
    conn.commit()
    apply_schema(conn)
    apply_schema(conn)
    assert tuple(conn.execute("SELECT key_digest,key_enc,credential_ids FROM api_keys "
                              "WHERE id='old'").fetchone()) == ("old-digest", None, '["selected"]')
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert ApiKeyRepository(db, CredentialCipher(SECRET)).reveal("old", "alice") is None
    assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 1
    db.close()
