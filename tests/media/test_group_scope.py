"""媒体任务重新读Key组成员；先前Principal不得保留被移出的账号权限。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.auth.rbac import ForbiddenError
from src.compat.openai.request import InvalidRequest
from src.db.groups import CredentialGroupRepository
from src.engine.scheduler import Candidate


@pytest.fixture(autouse=True)
def private_directory_boundary(monkeypatch):
    # 组成员授权契约不测试平台 ACL；私有目录保护另有 Windows/POSIX 专用测试。
    monkeypatch.setattr("src.db.conn.secure_dir", lambda *_args, **_kwargs: True)


async def test_media_fresh_principal_resolves_real_group_changes_and_deletion(media_env, repo):
    env = media_env
    credentials, database = repo
    ids = [credentials.add(provider="miora", credential_data={"uid": f"synthetic-{index}"})
           for index in range(2)]
    groups = CredentialGroupRepository(database)
    group = groups.create("miora", "Images", ids[:1])
    env.services.credential_groups = groups
    env.state.rows = [Candidate(account_id, "miora", health=100) for account_id in ids]
    env.state.data = {account_id: {"id": account_id} for account_id in ids}
    env.state.key.update(credential_ids=[], group_ids=[group["id"]])
    principal = replace(env.principal, group_ids=(group["id"],), credential_ids=(ids[0],))
    first = env.manager.fresh_principal(principal)
    assert first.credential_ids == (ids[0],)
    groups.update(group["id"], credential_ids=ids[1:])
    fresh = env.manager.fresh_principal(principal)
    assert fresh.credential_ids == (ids[1],) and fresh.group_ids == (group["id"],)
    with pytest.raises(InvalidRequest):
        env.manager._candidate(fresh, ids[0])
    request = {"model": "Pretty Picture", "kind": "image"}
    assert await env.manager.choose(fresh, request) == ids[1]
    groups.update(group["id"], credential_ids=[])
    with pytest.raises(ForbiddenError, match="no remaining"):
        env.manager.fresh_principal(principal)
    groups.delete(group["id"])
    with pytest.raises(ForbiddenError, match="not found"):
        env.manager.fresh_principal(principal)
