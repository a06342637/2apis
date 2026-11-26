"""订阅展示补充信息持久化与旧库升级，不改变额度和权限。"""

from __future__ import annotations

import json

import pytest

from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import CredentialRepository, _quota_detail_value
from src.provider.base import Quota
from tests.helpers.constants import SECRET


def repository(tmp_path):
    db = Database(tmp_path / "quota-detail.sqlite3")
    apply_schema(db.connect())
    return db, CredentialRepository(db, CredentialCipher(SECRET))


def test_quota_detail_survives_legacy_balance_only_updates_and_replaces_explicit_unknown(tmp_path):
    db, repo = repository(tmp_path)
    cid = repo.add(provider="miora", credential_data={"access_token": "synthetic", "uid": "one"})
    detail = {
        "miora_subscription": {
            "tier": "pro",
            "status": "active",
            "vip": True,
            "expires_at": 2_000_000_000,
            "probed_at": 1,
        }
    }
    repo.save_quota(cid, Quota(remaining=7, total=10, probed_at=1, detail=detail))
    repo.save_quota(cid, Quota(remaining=6, total=10, probed_at=2))
    assert repo.quota_detail(cid) == detail
    assert repo.list_all()[0]["quota_detail"] == detail
    unknown = {
        "miora_subscription": {
            "tier": None,
            "status": "unknown",
            "vip": None,
            "expires_at": None,
            "probed_at": 3,
        }
    }
    repo.save_quota(cid, Quota(remaining=5, total=10, probed_at=3, detail=unknown))
    assert repo.quota_detail(cid) == unknown and repo.list_all()[0]["quota_remaining"] == 5
    repo.save_quota(cid, Quota(remaining=5, total=10, probed_at=4, detail={}))
    assert repo.quota_detail(cid) == {}
    assert repo.quota_detail("missing") is None
    db.close()


@pytest.mark.parametrize("raw", [None, "", "not-json", "[]", "null", "1", "true"])
def test_invalid_or_missing_stored_quota_detail_is_unknown(raw):
    assert _quota_detail_value(raw) is None


def test_deeply_corrupt_quota_detail_is_unknown_without_breaking_account_list():
    assert _quota_detail_value("[" * 3000 + "0" + "]" * 3000) is None


def test_legacy_42_upgrade_adds_unknown_detail_and_preserves_balance_and_then_new_detail(tmp_path):
    db, repo = repository(tmp_path)
    cid = repo.add(provider="miora", credential_data={"access_token": "synthetic", "uid": "one"})
    repo.save_quota(cid, Quota(remaining=7, total=10, probed_at=1))
    conn = db.connect()
    conn.execute("ALTER TABLE credentials DROP COLUMN quota_detail")
    conn.execute("PRAGMA user_version = 42")
    apply_schema(conn)
    row = repo.list_all()[0]
    assert row["quota_detail"] is None and row["quota_remaining"] == 7
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    detail = {
        "miora_subscription": {
            "tier": "free",
            "status": "free",
            "vip": False,
            "expires_at": None,
            "probed_at": 2,
        }
    }
    repo.save_quota(cid, Quota(remaining=7, total=10, probed_at=2, detail=detail))
    apply_schema(conn)
    assert (
        json.loads(
            conn.execute("SELECT quota_detail FROM credentials WHERE id=?", (cid,)).fetchone()[0]
        )
        == detail
    )
    db.close()
