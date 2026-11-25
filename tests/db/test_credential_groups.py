"""正式组身份/同渠道成员/升级/动态作用域的数据库契约。"""

from __future__ import annotations

import pytest

from src.db.groups import CredentialGroupRepository, GroupConflict, group_name


def accounts(repo):
    credentials, _database = repo
    return [credentials.add(provider=provider, credential_data={"uid": f"synthetic-{index}"})
            for index, provider in enumerate(("miora", "miora", "codearts"))]


def test_group_identity_name_uniqueness_membership_and_optimistic_update(repo, monkeypatch):
    credentials, database = repo
    first, second, foreign = accounts(repo)
    groups = CredentialGroupRepository(database)
    monkeypatch.setattr("src.db.groups.time.time", lambda: 1000)
    created = groups.create("miora", "  Team  ", [second, first, first])
    other = groups.create("codearts", "Team", [foreign])
    empty = groups.create("miora", "Empty")
    assert created["credential_ids"] == sorted([first, second])
    assert created["member_count"] == 2 and created["name"] == "Team"
    assert groups.get(created["id"]) == created
    assert {row["id"] for row in groups.list_all("miora")} == {created["id"], empty["id"]}
    assert len(groups.list_all()) == 3 and groups.get("missing") is None
    with pytest.raises(ValueError, match="already exists"):
        groups.create("miora", "Team")
    with pytest.raises(ValueError, match="already exists"):
        groups.update(empty["id"], name="Team", credential_ids=[second])
    assert groups.get(empty["id"])["credential_ids"] == []
    changed = groups.update(created["id"], name="Renamed", credential_ids=[second],
                            expected_updated_at=created["updated_at"])
    assert changed["id"] == created["id"] and changed["updated_at"] == 1001
    assert changed["credential_ids"] == [second]
    with pytest.raises(GroupConflict):
        groups.update(created["id"], name="stale", expected_updated_at=1000)
    assert groups.update(created["id"], name="Renamed", credential_ids=[second, second]) == changed
    assert groups.update(created["id"]) == changed
    credentials.set_enabled(second, False)
    with database.transaction() as conn:
        conn.execute("UPDATE credentials SET disabled=1 WHERE id=?", (second,))
    assert groups.resolve([created["id"], empty["id"]], "miora") == (second,)
    assert groups.resolve([created["id"], created["id"]]) == (second,)
    with pytest.raises(ValueError, match="not found"):
        groups.resolve([created["id"], "missing"])
    with pytest.raises(ValueError, match="provider codearts"):
        groups.resolve([created["id"]], "codearts")
    with pytest.raises(ValueError, match="no remaining"):
        groups.resolve([empty["id"]])
    assert groups.resolve([]) == ()
    assert groups.delete(created["id"]) and not groups.delete(created["id"])
    assert groups.get(other["id"]) == other
    assert not database.connect().execute(
        "SELECT 1 FROM credential_group_members WHERE group_id=?", (created["id"],)).fetchall()


@pytest.mark.parametrize("name", [None, "", " ", 123, "a" * 129, "a\nb", "a\x00b"])
def test_group_name_rejects_empty_long_nontext_or_control_characters(name):
    with pytest.raises(ValueError):
        group_name(name)


@pytest.mark.parametrize("provider", ["", "-bad", "Miora", "miora/other"])
def test_invalid_provider_cannot_create_group(repo, provider):
    _credentials, database = repo
    groups = CredentialGroupRepository(database)
    with pytest.raises(ValueError, match="provider"):
        groups.create(provider, "Team")
    assert groups.list_all() == []


def test_group_members_must_exist_and_belong_to_the_same_provider(repo):
    _credentials, database = repo
    first, _second, foreign = accounts(repo)
    groups = CredentialGroupRepository(database)
    with pytest.raises(ValueError, match="not found"):
        groups.create("miora", "Missing", [first, "missing"])
    with pytest.raises(ValueError, match="does not belong"):
        groups.create("miora", "Foreign", [foreign])
    assert groups.list_all() == []
    created = groups.create("miora", "Team", [first])
    with pytest.raises(ValueError, match="does not belong"):
        groups.update(created["id"], name="bad change", credential_ids=[foreign])
    assert groups.get(created["id"]) == created
    with pytest.raises(ValueError, match="not found"):
        groups.update("missing", name="name")


def test_account_deletion_cascades_members_and_provider_drift_never_expands_group(repo):
    credentials, database = repo
    first, second, _foreign = accounts(repo)
    groups = CredentialGroupRepository(database)
    created = groups.create("miora", "Team", [first, second])
    credentials.delete(first)
    assert groups.resolve([created["id"]]) == (second,)
    with database.transaction() as conn:
        conn.execute("UPDATE credentials SET provider='codearts' WHERE id=?", (second,))
    assert groups.get(created["id"])["member_count"] == 0
    assert groups.list_all()[0]["credential_ids"] == []
    with pytest.raises(ValueError, match="no remaining"):
        groups.resolve([created["id"]])
