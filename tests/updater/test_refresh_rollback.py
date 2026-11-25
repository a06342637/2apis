"""已消费一次性RT的新票据在旧schema回滚时保留，其余状态来自不变备份。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from src.db.crypto import CredentialCipher
from src.updater.deployment import Deployment, manifest, verify_backup
from tests.helpers.constants import SECRET


@pytest.fixture
def rollback_env(host, tmp_path):
    cipher = CredentialCipher(SECRET)
    host.env_file.write_text(f"APP_SECRET={SECRET}\n")
    original = cipher.encrypt(json.dumps({"access_token": "synthetic-old-access",
        "refresh_token": "synthetic-once-old", "expires_at": 500}).encode())
    rotated = cipher.encrypt(json.dumps({"access_token": "synthetic-new-access",
        "refresh_token": "synthetic-once-new", "expires_at": 900, "password": "saved"}).encode())
    primary = host.data_dir / "coding2api.sqlite3"
    with closing(sqlite3.connect(primary)) as conn, conn:
        conn.execute("CREATE TABLE credentials (id TEXT PRIMARY KEY, provider TEXT, data_enc BLOB, "
            "token_issued_at INTEGER,token_expires_at INTEGER,login_secret INTEGER,nickname TEXT)")
        conn.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?)",
                     ("account", "miora", original, 100, 500, None, "original nickname"))
        conn.execute("CREATE TABLE api_keys (id TEXT PRIMARY KEY,key_enc BLOB)")
        conn.execute("INSERT INTO api_keys VALUES ('original-key',?)", (original,))
        conn.execute("CREATE TABLE credential_groups (id TEXT PRIMARY KEY,name TEXT)")
        conn.execute("INSERT INTO credential_groups VALUES ('original-group','Old Group')")
        conn.execute("PRAGMA user_version=41")
    deployment = Deployment(host)
    backup = tmp_path / "rollback-backup"
    deployment.backup(backup)
    # 健康检查前新服务启动、轮转RT且完成落库；停止/排空后helper才做restore。
    with closing(sqlite3.connect(primary)) as conn, conn:
        conn.execute("UPDATE credentials SET data_enc=?,token_issued_at=200,"
            "token_expires_at=900,login_secret=1,nickname='failed new nickname'", (rotated,))
        conn.execute("ALTER TABLE credentials ADD COLUMN newer_schema_only TEXT")
        conn.execute("DELETE FROM api_keys")
        conn.execute("DELETE FROM credential_groups")
        conn.execute("PRAGMA user_version=42")
    return SimpleNamespace(host=host, primary=primary, backup=backup, deployment=deployment,
                           cipher=cipher, original=original, rotated=rotated)


def row(env):
    with closing(sqlite3.connect(env.primary)) as conn:
        return conn.execute("SELECT * FROM credentials WHERE id='account'").fetchone()


def test_rollback_keeps_new_rt_and_old_schema_keys_groups_backup(rollback_env, caplog):
    env = rollback_env
    before = manifest(env.backup)
    env.deployment.restore(env.backup)
    restored = row(env)
    assert restored == ("account", "miora", env.rotated, 200, 900, 1, "original nickname")
    payload = json.loads(env.cipher.decrypt(restored[2]))
    assert payload["refresh_token"] == "synthetic-once-new"
    assert env.host.env_file.read_text() == f"APP_SECRET={SECRET}\n"
    with closing(sqlite3.connect(env.primary)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 41
        columns = {item[1] for item in conn.execute("PRAGMA table_info(credentials)")}
        assert "newer_schema_only" not in columns
        assert conn.execute("SELECT id,key_enc FROM api_keys").fetchone() == (
            "original-key", env.original)
        assert conn.execute("SELECT * FROM credential_groups").fetchone() == (
            "original-group", "Old Group")
    assert manifest(env.backup) == before
    verify_backup(env.backup)
    assert "保留 1 个账号" in caplog.text
    assert "synthetic-once" not in caplog.text and str(env.rotated) not in caplog.text


@pytest.mark.parametrize("issued,expires", [(100, 900), (200, 500), (200, 900)])
def test_newness_accepts_one_increased_bound(rollback_env, issued, expires):
    env = rollback_env
    with closing(sqlite3.connect(env.primary)) as conn, conn:
        conn.execute("UPDATE credentials SET token_issued_at=?,token_expires_at=?",
                     (issued, expires))
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.rotated, issued, expires, 1)


@pytest.mark.parametrize("issued,expires", [
    (100, 500), (99, 900), (200, 499), (None, 900), (200, None),
    (-1, 900), (200, -1), ("invalid", 900), (200, "invalid"), (200, 200), (0, 900),
])
def test_unproven_timestamp_keeps_original_login(rollback_env, issued, expires):
    env = rollback_env
    with closing(sqlite3.connect(env.primary)) as conn, conn:
        conn.execute("UPDATE credentials SET token_issued_at=?,token_expires_at=?",
                     (issued, expires))
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.original, 100, 500, None)


@pytest.mark.parametrize("change", ["provider", "id", "same_cipher", "empty_cipher", "new_account"])
def test_only_matching_existing_account_with_changed_cipher_is_preserved(rollback_env, change):
    env = rollback_env
    with closing(sqlite3.connect(env.primary)) as conn, conn:
        if change == "provider":
            conn.execute("UPDATE credentials SET provider='codearts'")
        elif change == "id":
            conn.execute("UPDATE credentials SET id='foreign-account'")
        elif change == "same_cipher":
            conn.execute("UPDATE credentials SET data_enc=?", (env.original,))
        elif change == "empty_cipher":
            conn.execute("UPDATE credentials SET data_enc=X''")
        else:
            conn.execute("DELETE FROM credentials")
            conn.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?)", (
                "added-after-backup", "miora", env.rotated, 200, 900, 1, "new", "new column"))
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.original, 100, 500, None)
    with closing(sqlite3.connect(env.primary)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["missing", "corrupt", "missing_table", "missing_column"])
def test_unreadable_current_database_keeps_old_backup(rollback_env, mode, caplog):
    env = rollback_env
    if mode == "missing":
        env.primary.unlink()
    elif mode == "corrupt":
        env.primary.write_bytes(b"corrupt current database")
    else:
        with closing(sqlite3.connect(env.primary)) as conn, conn:
            if mode == "missing_table":
                conn.execute("DROP TABLE credentials")
            else:
                conn.execute("ALTER TABLE credentials DROP COLUMN login_secret")
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.original, 100, 500, None)
    verify_backup(env.backup)
    assert "回滚" in caplog.text and ("无法保留" in caplog.text or "需要重新登录" in caplog.text)


def test_incompatible_old_schema_is_restored_without_adding_new_login_columns(rollback_env, caplog):
    env = rollback_env
    old_primary = env.backup / "data" / "coding2api.sqlite3"
    with closing(sqlite3.connect(old_primary)) as conn, conn:
        conn.execute("ALTER TABLE credentials DROP COLUMN login_secret")
    signed = manifest(env.backup)
    signed.pop("manifest.json")
    (env.backup / "manifest.json").write_text(json.dumps(signed))
    env.deployment.restore(env.backup)
    with closing(sqlite3.connect(env.primary)) as conn:
        columns = {item[1] for item in conn.execute("PRAGMA table_info(credentials)")}
        assert "login_secret" not in columns
        assert conn.execute("SELECT data_enc FROM credentials").fetchone()[0] == env.original
    assert "字段不兼容" in caplog.text


def test_current_integrity_check_failure_falls_back_to_backup(rollback_env, monkeypatch, caplog):
    env = rollback_env
    connect = sqlite3.connect

    def wrapped(path, **kwargs):
        conn = connect(path, **kwargs)
        if str(path).startswith(env.primary.as_uri()):
            def execute(sql, *args):
                return (SimpleNamespace(fetchone=lambda: ("bad integrity",))
                        if sql == "PRAGMA quick_check" else conn.execute(sql, *args))

            return SimpleNamespace(execute=execute, close=conn.close)
        return conn

    monkeypatch.setattr("src.updater.deployment.sqlite3.connect", wrapped)
    assert env.deployment._rotated_logins(env.backup / "data" / "coding2api.sqlite3") == []
    assert "无法安全读取" in caplog.text


def test_preservation_update_error_rolls_back_all_login_updates_and_warns(rollback_env, caplog):
    env = rollback_env
    # 同表约束可拒绝新版本保存的值；保留失败不能损坏已恢复旧schema数据库。
    old_primary = env.backup / "data" / "coding2api.sqlite3"
    with closing(sqlite3.connect(old_primary)) as conn, conn:
        conn.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?)",
                     ("second", "miora", env.original, 100, 500, None, "second old"))
        conn.execute("CREATE TRIGGER no_fresh_update BEFORE UPDATE ON credentials "
                     "WHEN OLD.id='second' "
                     "BEGIN SELECT RAISE(ABORT,'controlled rejection'); END")
    with closing(sqlite3.connect(env.primary)) as conn, conn:
        conn.execute("INSERT INTO credentials VALUES (?,?,?,?,?,?,?,?)",
                     ("second", "miora", env.rotated, 200, 900, 1, "second new", "new"))
    signed = manifest(env.backup)
    signed.pop("manifest.json")
    (env.backup / "manifest.json").write_text(json.dumps(signed))
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.original, 100, 500, None)
    with closing(sqlite3.connect(env.primary)) as conn:
        assert conn.execute("SELECT data_enc FROM credentials WHERE id='second'").fetchone()[0] == (
            env.original)
    assert "保留新续期票据失败" in caplog.text
    verify_backup(env.backup)


@pytest.mark.parametrize("value,expected", [
    ("APP_SECRET=plain-value\n", "plain-value"),
    ("# comment\nOTHER=value\nexport APP_SECRET='quoted-value'\n", "quoted-value"),
    ('APP_SECRET="double-quoted"\n', "double-quoted"),
    ("APP_SECRET=plain # comment\n", "plain"),
    ("APP_SECRET=plain#literal-hash\n", "plain#literal-hash"),
    ("APP_SECRET=${EXTERNAL_KEY}\n", None), ("APP_SECRET=unsafe\\ntext\n", None),
    ("OTHER=value\n", None), ("APP_SECRET=\n", None),
    ("APP_SECRET=a\nAPP_SECRET=b\n", None),
    ('APP_SECRET="\n', None), ('APP_SECRET="unclosed\n', None),
    ('APP_SECRET="unsafe\\nquote"\n', None),
])
def test_app_secret_comparison_only_accepts_unambiguous_literals(tmp_path, value, expected):
    path = tmp_path / "environment"
    path.write_text(value)
    assert Deployment._app_secret(path) == expected


def test_unreadable_env_cannot_qualify_ciphertext_for_preservation(tmp_path):
    path = tmp_path / "missing"
    assert Deployment._app_secret(path) is None
    path.write_bytes(b"APP_SECRET=\xff")
    assert Deployment._app_secret(path) is None


@pytest.mark.parametrize("mode", ["different", "missing", "missing_key", "empty_key"])
def test_changed_or_missing_app_secret_restores_old_env_and_does_not_inherit_new_cipher(
        rollback_env, mode, caplog):
    env = rollback_env
    if mode == "different":
        env.host.env_file.write_text("APP_SECRET=another-strong-synthetic-value\n")
    elif mode == "missing":
        env.host.env_file.unlink()
    elif mode == "missing_key":
        env.host.env_file.write_text("OTHER_KEY=synthetic\n")
    else:
        env.host.env_file.write_text("APP_SECRET=\n")
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.original, 100, 500, None)
    assert env.host.env_file.read_text() == f"APP_SECRET={SECRET}\n"
    assert "密钥缺失或不一致" in caplog.text
    assert SECRET not in caplog.text and "another-strong-synthetic" not in caplog.text


def test_quoted_current_secret_matches_literal_backup_key_and_preserves_new_ticket(rollback_env):
    env = rollback_env
    env.host.env_file.write_text(f"APP_SECRET='{SECRET}'\n")
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.rotated, 200, 900, 1)


def test_missing_backup_app_secret_prevents_inheriting_new_cipher(rollback_env, caplog):
    env = rollback_env
    (env.backup / "env").write_text("OTHER=old-config\n")
    signed = manifest(env.backup)
    signed.pop("manifest.json")
    (env.backup / "manifest.json").write_text(json.dumps(signed))
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.original, 100, 500, None)
    assert "密钥缺失或不一致" in caplog.text


@pytest.mark.parametrize("issued,expires", [(0, 500), (100, 0), (None, 500), (100, None)])
def test_unknown_backup_token_bounds_cannot_prove_newer_ticket(rollback_env, issued, expires):
    env = rollback_env
    old_primary = env.backup / "data" / "coding2api.sqlite3"
    with closing(sqlite3.connect(old_primary)) as conn, conn:
        conn.execute("UPDATE credentials SET token_issued_at=?,token_expires_at=?",
                     (issued, expires))
    signed = manifest(env.backup)
    signed.pop("manifest.json")
    (env.backup / "manifest.json").write_text(json.dumps(signed))
    env.deployment.restore(env.backup)
    assert row(env)[2:6] == (env.original, issued, expires, None)
