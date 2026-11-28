"""密钥组归属快照、实际账号交集、实时/查询/CSV与老库兼容。"""
from types import SimpleNamespace

from fastapi import Request

from src.api.deps import _api_key_principal
from src.config import Settings
from src.db.groups import CredentialGroupRepository
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.db.repo import ApiKeyRepository
from src.request_context import (
    get_api_key_group_scope,
    set_api_key_group_scope,
    set_api_key_id,
)
from src.stats.collector import StatsCollector
from src.stats.csv import EXPORT_HEADERS, event_cells
from src.stats.groups import capture_group_scope, decode_group_attribution, event_group_attribution
from src.stats.query import StatsQuery
from tests.helpers.constants import SECRET


def test_snapshot_intersects_key_groups_with_actual_account_and_survives_edits(repo, tmp_path):
    credentials, db = repo
    a = credentials.add(provider="miora", credential_data={"authToken": "synthetic-a"})
    b = credentials.add(provider="miora", credential_data={"authToken": "synthetic-b"})
    groups = CredentialGroupRepository(db)
    selected = groups.create("miora", "生产组", [a, b])
    overlapping = groups.create("miora", "账号的其他组", [a])
    keys = ApiKeyRepository(db, credentials._cipher)
    key = keys.create("alice", "synthetic-key", provider_binding="miora",
                      group_ids=[selected["id"]])
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    services = SimpleNamespace(settings=settings, api_keys=keys,
                               users=SimpleNamespace(is_active=lambda _: True),
                               user_repo=SimpleNamespace(get=lambda _: {}),
                               credential_groups=groups)
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    principal = _api_key_principal(services, request, key["api_key"])
    assert set(principal.credential_ids) == {a, b}
    assert len(get_api_key_group_scope()) == 1
    groups.update(selected["id"], name="改名后的组", credential_ids=[b])
    events = []
    collector, query = StatsCollector(db, on_record=events.append), StatsQuery(db)
    expected = [{"id": selected["id"], "name": "生产组"}]
    try:
        collector.record(username="alice", provider="miora", model="m", ok=True,
                         credential_id=a, input_tokens=10, output_tokens=20, credit=2)
        assert events[0]["credential_groups"] == expected
        groups.delete(selected["id"])
        keys.delete(key["id"], "alice")
        assert query.events()["events"][0]["credential_groups"] == expected
        assert query.events(sort="model")["events"][0]["credential_groups"] == expected
        exported = query.export_events()[0]
        assert exported["credential_groups"] == expected
        assert "group_attribution" not in exported
        assert event_cells(exported)[EXPORT_HEADERS.index("分组")] == "生产组"
        assert query.overview()["requests"] == 1
        assert query.overview()["credit"] == 2
        assert overlapping["id"] not in str(expected)
    finally:
        set_api_key_id(None)
    assert get_api_key_group_scope() == ()
    collector.record(username="alice", provider="miora", model="m", ok=True, credential_id=a)
    assert query.events()["events"][0]["credential_groups"] == []


def test_snapshot_handles_missing_metadata_and_multiple_bound_groups():
    assert capture_group_scope((), None) == ()
    assert capture_group_scope(("group",), None) is None
    assert capture_group_scope(("group",), SimpleNamespace(get=lambda _: None)) is None
    def failed(_):
        raise RuntimeError("database unavailable")
    assert capture_group_scope(("group",), SimpleNamespace(get=failed)) is None
    snapshot = capture_group_scope(("one", "two"), SimpleNamespace(get=lambda group_id: {
        "name": group_id, "credential_ids": ["account"],
    }))
    try:
        set_api_key_group_scope(snapshot)
        assert decode_group_attribution(event_group_attribution("account")) == [
            {"id": "one", "name": "one"}, {"id": "two", "name": "two"}]
        assert event_group_attribution("other") == "[]"
        assert event_group_attribution(None) == "[]"
        set_api_key_group_scope(None)
        assert event_group_attribution("account") is None
        assert decode_group_attribution(None) is None
        cells = event_cells({"credential_groups": [{"id": "g", "name": "=unsafe"}]})
        assert cells[-1] == "'=unsafe"
    finally:
        set_api_key_id(None)


def test_v43_migration_keeps_old_events_unknown_and_is_idempotent(repo):
    _, db = repo
    collector = StatsCollector(db)
    collector.record(username="alice", provider="miora", model="m", ok=True, input_tokens=10)
    conn = db.connect()
    conn.execute("ALTER TABLE usage_events DROP COLUMN group_attribution")
    conn.execute("PRAGMA user_version=43")
    conn.commit()
    apply_schema(conn)
    apply_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    old = StatsQuery(db).events()["events"][0]
    assert old["credential_groups"] is None and old["input_tokens"] == 10
