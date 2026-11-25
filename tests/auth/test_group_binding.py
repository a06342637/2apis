"""正式组解析失败关闭，空数组才保留原全池/账号集合语义。"""

from types import SimpleNamespace

import pytest

from src.auth.group_binding import resolve_credential_scope
from src.auth.rbac import ForbiddenError


def test_group_scope_keeps_legacy_accounts_and_resolves_bound_groups():
    assert resolve_credential_scope({}, None) == ()
    assert resolve_credential_scope({"credential_ids": [" a ", "a"]}, None) == ("a",)
    seen = []
    groups = SimpleNamespace(resolve=lambda ids, provider: seen.append((ids, provider)) or ("a",))
    assert resolve_credential_scope({"group_ids": ["group", "group"], "provider_binding": "miora"},
                                    groups) == ("a",)
    assert seen == [(["group"], "miora")]


@pytest.mark.parametrize("record", [
    {"group_ids": ["group"]}, {"group_ids": ["group"], "credential_ids": ["account"]},
    {"group_ids": None}, {"group_ids": "group"}, {"group_ids": [1]},
    {"credential_ids": None},
])
def test_unavailable_or_damaged_scope_is_never_full_pool(record):
    with pytest.raises(ForbiddenError):
        resolve_credential_scope(record, None)


def test_group_repository_error_becomes_clear_forbidden():
    def missing(_ids, _provider):
        raise ValueError("credential group not found")

    with pytest.raises(ForbiddenError, match="not found"):
        resolve_credential_scope({"group_ids": ["group"]}, SimpleNamespace(resolve=missing))
