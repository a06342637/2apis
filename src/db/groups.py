"""正式账号分组：稳定组ID、同渠道成员、请求时动态解析绑定集合。"""

from __future__ import annotations

import re
import sqlite3
import time
import uuid

from ..auth.account_binding import normalize_credential_ids
from .conn import Database


class GroupConflict(ValueError):
    """分组已被其他管理员修改，不能覆盖较新的成员或名称。"""


def group_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 128:
        raise ValueError("group name must be non-empty text up to 128 characters")
    name = raw.strip()
    if any(ord(char) < 32 for char in name):
        raise ValueError("group name must not contain control characters")
    return name


class CredentialGroupRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _members(conn: sqlite3.Connection, group_id: str) -> list[str]:
        return [row[0] for row in conn.execute(
            "SELECT m.credential_id FROM credential_group_members m "
            "JOIN credentials c ON c.id=m.credential_id "
            "JOIN credential_groups g ON g.id=m.group_id AND g.provider=c.provider "
            "WHERE m.group_id=? ORDER BY m.credential_id", (group_id,))]

    @classmethod
    def _read(cls, conn: sqlite3.Connection, group_id: str) -> dict | None:
        row = conn.execute("SELECT * FROM credential_groups WHERE id=?", (group_id,)).fetchone()
        if row is None:
            return None
        members = cls._members(conn, group_id)
        return {**dict(row), "credential_ids": members, "member_count": len(members)}

    @staticmethod
    def _validate_members(conn: sqlite3.Connection, provider: str, members: list[str]) -> None:
        for account_id in members:
            row = conn.execute("SELECT provider FROM credentials WHERE id=?",
                               (account_id,)).fetchone()
            if row is None:
                raise ValueError(f"credential not found: {account_id}")
            if row[0] != provider:
                raise ValueError(f"credential {account_id} does not belong to provider {provider}")

    @staticmethod
    def _replace_members(conn: sqlite3.Connection, group_id: str, members: list[str]) -> None:
        conn.execute("DELETE FROM credential_group_members WHERE group_id=?", (group_id,))
        conn.executemany(
            "INSERT INTO credential_group_members (group_id,credential_id) VALUES (?,?)",
            [(group_id, account_id) for account_id in members])

    def create(self, provider: str, name: object, credential_ids: object = None) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", provider):
            raise ValueError("group provider must be a registered provider ID")
        title = group_name(name)
        members = normalize_credential_ids([] if credential_ids is None else credential_ids)
        group_id, now = f"group-{uuid.uuid4().hex}", int(time.time())
        try:
            with self._db.transaction() as conn:
                self._validate_members(conn, provider, members)
                conn.execute(
                    "INSERT INTO credential_groups (id,provider,name,created_at,updated_at) "
                    "VALUES (?,?,?,?,?)", (group_id, provider, title, now, now))
                self._replace_members(conn, group_id, members)
        except sqlite3.IntegrityError as error:
            raise ValueError("group name already exists for this provider") from error
        return {"id": group_id, "provider": provider, "name": title,
                "created_at": now, "updated_at": now,
                "credential_ids": sorted(members), "member_count": len(members)}

    def get(self, group_id: str) -> dict | None:
        return self._read(self._db.connect(), group_id)

    def list_all(self, provider: str = "") -> list[dict]:
        conn = self._db.connect()
        rows = conn.execute(
            "SELECT * FROM credential_groups WHERE (?='' OR provider=?) "
            "ORDER BY provider,name,id", (provider, provider)).fetchall()
        members: dict[str, list[str]] = {}
        for row in conn.execute("SELECT m.group_id,m.credential_id FROM credential_group_members m "
                                "JOIN credentials c ON c.id=m.credential_id "
                                "JOIN credential_groups g ON g.id=m.group_id "
                                "AND g.provider=c.provider "
                                "ORDER BY m.credential_id"):
            members.setdefault(row[0], []).append(row[1])
        return [{**dict(row), "credential_ids": members.get(row["id"], []),
                 "member_count": len(members.get(row["id"], []))} for row in rows]

    def update(self, group_id: str, *, name: str | None = None,
               credential_ids: list[str] | None = None,
               expected_updated_at: int | None = None) -> dict:
        try:
            with self._db.transaction() as conn:
                before = self._read(conn, group_id)
                if before is None:
                    raise ValueError("credential group not found")
                if expected_updated_at is not None and expected_updated_at != before["updated_at"]:
                    raise GroupConflict("credential group changed; reload before editing")
                title = before["name"] if name is None else group_name(name)
                members = (before["credential_ids"] if credential_ids is None
                           else normalize_credential_ids(credential_ids))
                self._validate_members(conn, before["provider"], members)
                if title == before["name"] and sorted(members) == before["credential_ids"]:
                    return before
                now = max(int(time.time()), before["updated_at"] + 1)
                conn.execute("UPDATE credential_groups SET name=?,updated_at=? WHERE id=?",
                             (title, now, group_id))
                self._replace_members(conn, group_id, members)
        except sqlite3.IntegrityError as error:
            raise ValueError("group name already exists for this provider") from error
        return {**before, "name": title, "updated_at": now,
                "credential_ids": sorted(members), "member_count": len(members)}

    def delete(self, group_id: str) -> bool:
        with self._db.transaction() as conn:
            result = conn.execute("DELETE FROM credential_groups WHERE id=?", (group_id,))
            return result.rowcount > 0

    def resolve(self, group_ids: list[str], provider_binding: str = "") -> tuple[str, ...]:
        members: list[str] = []
        for group_id in normalize_credential_ids(group_ids):
            group = self.get(group_id)
            if group is None:
                raise ValueError(f"credential group not found: {group_id}")
            if provider_binding and group["provider"] != provider_binding:
                raise ValueError(f"credential group does not belong to provider {provider_binding}")
            members.extend(group["credential_ids"])
        if group_ids and not members:
            raise ValueError("bound credential groups have no remaining members")
        return tuple(dict.fromkeys(members))
