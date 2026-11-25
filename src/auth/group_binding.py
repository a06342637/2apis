"""Key的组作用域每请求现读成员；损坏/删除/空组绝不隐式扩展到全池。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .account_binding import normalize_credential_ids
from .rbac import ForbiddenError

if TYPE_CHECKING:
    from ..db.groups import CredentialGroupRepository


def resolve_credential_scope(
    record: dict, groups: CredentialGroupRepository | None
) -> tuple[str, ...]:
    try:
        account_ids = normalize_credential_ids(record.get("credential_ids", []))
        group_ids = normalize_credential_ids(record.get("group_ids", []))
        if account_ids and group_ids:
            raise ValueError("credential_ids and group_ids cannot both be non-empty")
        if not group_ids:
            return tuple(account_ids)
        if groups is None:
            raise ValueError("credential group service unavailable")
        return groups.resolve(group_ids, record.get("provider_binding") or "")
    except ValueError as error:
        raise ForbiddenError(str(error)) from error
