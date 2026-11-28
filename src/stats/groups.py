"""按鉴权时的密钥分组快照记录归属，不用当前账号全部分组回填历史。"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..request_context import GroupScope, get_api_key_group_scope

if TYPE_CHECKING:
    from ..db.groups import CredentialGroupRepository


def capture_group_scope(group_ids: tuple[str, ...],
                        groups: CredentialGroupRepository | None) -> GroupScope | None:
    if not group_ids:
        return ()
    if groups is None:
        return None
    try:
        snapshot = []
        for group_id in group_ids:
            group = groups.get(group_id)
            if group is None:
                return None
            snapshot.append((group_id, group["name"], tuple(group["credential_ids"])))
        return tuple(snapshot)
    except Exception:  # noqa: BLE001 - 统计元数据读取失败不能中断已通过鉴权的推理
        return None


def event_group_attribution(credential_id: str | None) -> str | None:
    scope = get_api_key_group_scope()
    if scope is None:
        return None
    return json.dumps([{"id": group_id, "name": name} for group_id, name, members in scope
                       if credential_id in members], ensure_ascii=False, separators=(",", ":"))


def decode_group_attribution(raw: str | None) -> list[dict[str, str]] | None:
    return None if raw is None else json.loads(raw)
