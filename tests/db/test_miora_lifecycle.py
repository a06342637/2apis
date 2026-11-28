"""Only a fresh confirmed zero can delete an automatically paused account."""
import logging

import pytest

from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import MIORA_EXHAUSTED_GRACE
from src.provider.base import Quota
from src.stats.collector import StatsCollector


def account(repo, provider="miora"):
    credentials, _ = repo
    return credentials.add(provider=provider, credential_data={"access_token": "fixture-only"},
                           nickname="测试账号", now=1)


def balance(credentials, ident, stamp, remaining=0, **kwargs):
    credentials.save_quota(ident, Quota(remaining=remaining, total=250, probed_at=stamp, **kwargs))


def test_miora_pause_and_delete_at_grace_boundary(repo, caplog):
    credentials, db = repo
    ident = account(repo)
    StatsCollector(db).record(username="alice", provider="miora", model="synthetic",
                              credential_id=ident, input_tokens=123, output_tokens=45,
                              ok=True, now=100)
    with caplog.at_level(logging.INFO, logger="src.credentials.miora_lifecycle"):
        balance(credentials, ident, 100)
        assert not credentials.list_all()[0]["enabled"]
        balance(credentials, ident, 100 + MIORA_EXHAUSTED_GRACE - 1)
        assert credentials.provider_of(ident) == "miora"
        balance(credentials, ident, 100 + MIORA_EXHAUSTED_GRACE)
    assert credentials.provider_of(ident) is None
    assert all(value in caplog.text for value in (ident, "测试账号", "自动暂停", "已自动删除",
                                                "输入词元数=123", "输出词元数=45"))
    assert "fixture-only" not in caplog.text


def test_positive_balance_resume_and_invalid_or_stale_probe_never_delete(repo):
    credentials, _ = repo
    ident = account(repo)
    balance(credentials, ident, 100, 2)
    balance(credentials, ident, 99)
    assert credentials.list_all()[0]["enabled"]
    balance(credentials, ident, 101)
    credentials.mark_probe_failed(ident, 200)
    balance(credentials, ident, 300, probe_failed=True)
    balance(credentials, ident, 310, None)
    balance(credentials, ident, 320, -1)
    balance(credentials, ident, None)
    balance(credentials, ident, 330, float("inf"))
    balance(credentials, ident, 331, 4)
    assert credentials.list_all()[0]["enabled"]
    balance(credentials, ident, 400)
    credentials.mark_relogin_required(ident, "expired")
    balance(credentials, ident, 500_000)
    assert credentials.provider_of(ident) == "miora"


@pytest.mark.parametrize("manual", [True, False])
def test_admin_change_cancels_previous_countdown(repo, manual):
    credentials, _ = repo
    ident = account(repo)
    balance(credentials, ident, 100)
    if manual:
        credentials.set_enabled(ident, False, now=200)
    else:
        credentials.set_enabled(ident, True, now=200)
        balance(credentials, ident, 199)
        assert credentials.list_all()[0]["enabled"]
    balance(credentials, ident, 500_000)
    assert credentials.provider_of(ident) == "miora"


def test_migration_preserves_old_rows_and_non_miora_not_paused(repo):
    credentials, db = repo
    ident = account(repo, "codearts")
    conn = db.connect()
    conn.execute("ALTER TABLE credentials DROP COLUMN miora_exhausted_at")
    conn.execute("PRAGMA user_version = 44")
    apply_schema(conn)
    apply_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT miora_exhausted_at FROM credentials").fetchone()[0] is None
    balance(credentials, ident, 100)
    balance(credentials, "missing", None)
    assert credentials.list_all()[0]["enabled"]
