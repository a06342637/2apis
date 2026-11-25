"""v41老库升级分组，原Key/可恢复密文/账号绑定保留且幂等。"""

from src.db.groups import CredentialGroupRepository
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import ApiKeyRepository


def test_v41_groups_upgrade_is_idempotent_and_keeps_existing_key_policy(repo):
    credentials, database = repo
    account = credentials.add(provider="miora", credential_data={"uid": "synthetic"})
    keys = ApiKeyRepository(database, credentials._cipher)
    key = keys.create("root", "legacy", credential_ids=[account], allowed_models="glm-*")
    conn = database.connect()
    original_encrypted = conn.execute("SELECT key_enc FROM api_keys WHERE id=?",
                                      (key["id"],)).fetchone()[0]
    conn.execute("DROP TABLE credential_group_members")
    conn.execute("DROP TABLE credential_groups")
    conn.execute("ALTER TABLE api_keys DROP COLUMN group_ids")
    conn.execute("PRAGMA user_version=41")
    conn.commit()
    apply_schema(conn)
    apply_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert keys.authenticate(key["api_key"])["group_ids"] == []
    assert keys.authenticate(key["api_key"])["credential_ids"] == [account]
    assert keys.authenticate(key["api_key"])["allowed_models"] == "glm-*"
    assert keys.reveal(key["id"], "root") == key["api_key"]
    assert conn.execute("SELECT key_enc FROM api_keys WHERE id=?",
                        (key["id"],)).fetchone()[0] == original_encrypted
    assert CredentialGroupRepository(database).list_all() == []
    created = CredentialGroupRepository(database).create("miora", "Imported", [account])
    assert CredentialGroupRepository(database).resolve([created["id"]]) == (account,)
