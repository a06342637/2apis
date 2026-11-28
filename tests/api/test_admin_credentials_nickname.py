"""凭证昵称独立编辑、共享展示、导出与权限。"""

from __future__ import annotations

import json

import pytest

from src.audit.actions import ACTION_CREDENTIAL_NICKNAME, ACTION_LABELS, ACTIONS
from src.auth.session import create_session_token
from tests.helpers.constants import SECRET


def _row(client, credential_id):
    return next(row for row in client.get("/api/credentials").json()["credentials"]
                if row["id"] == credential_id)


def _url(credential_id):
    return f"/api/credentials/{credential_id}/nickname"


def test_rename_all_providers_and_preserve_existing_export_contracts(admin_client, caplog):
    app, client = admin_client
    repo = app.state.credentials
    ids = []
    expected = []
    for provider in app.state.services.registry:
        raw = {"token": f"test-{provider}", "web_cookie": "browser", "password": "private"}
        cid = repo.add(provider=provider, nickname="旧称", tags=["导出"], credential_data=raw)
        ids.append(cid)
        before = repo.zero_snapshot(cid)
        nickname = f"备用 🚀 {provider}"
        with caplog.at_level("INFO"):
            renamed = client.put(_url(cid), json={"nickname": f"  {nickname}\u3000"})
        assert renamed.status_code == 200 and renamed.json() == {"nickname": nickname}
        after = repo.zero_snapshot(cid)
        assert after["updated_at"] > before["updated_at"]
        assert dict(after) == {**dict(before), "nickname": nickname,
                               "updated_at": after["updated_at"]}
        assert _row(client, cid)["nickname"] == repo.nickname_of(cid) == nickname
        exported_credential = {k: value for k, value in raw.items() if k != "password"}
        if provider in ("qoder", "codearts"):
            del exported_credential["web_cookie"]
        entry = {"provider": provider, "nickname": nickname, "tags": ["导出"],
                 "credential": exported_credential}
        exported = client.get(f"/api/credentials/{cid}/export")
        assert exported.status_code == 200 and exported.json() == entry
        assert exported.headers["Cache-Control"] == "no-store"
        assert exported.headers["Content-Disposition"] == (
            f'attachment; filename="coding2api-credential-{cid}.json"')
        expected.append(entry)
    assert {"miora", "trae", "zen", "kilo"}.issubset(app.state.services.registry)
    batch = client.post("/api/credentials/export", json={"ids": ids[::-1] + ids[:1]})
    assert batch.status_code == 200 and batch.json() == expected[::-1]
    assert batch.headers["Cache-Control"] == "no-store"
    assert batch.headers["Content-Disposition"] == (
        'attachment; filename="coding2api-credentials.json"')
    events = app.state.audit.query(action=ACTION_CREDENTIAL_NICKNAME)
    assert {e["target"] for e in events} == set(ids)
    assert len(events) == len(ids)
    assert all(e["actor"] == "root" and e["detail"] == "已更新" for e in events)
    assert "备用 🚀" not in caplog.text
    assert ACTION_CREDENTIAL_NICKNAME in ACTIONS
    assert ACTION_LABELS[ACTION_CREDENTIAL_NICKNAME] == "修改凭证昵称"


def test_previous_usage_shows_live_nickname_and_clear_restores_id_fallback(admin_client):
    app, client = admin_client
    repo = app.state.credentials
    cid = repo.add(provider="miora", nickname="旧名字", credential_data={"token": "synthetic"})
    app.state.stats_collector.record(username="root", provider="miora", model="m", ok=True,
                                     credential_id=cid)

    def stats_names():
        events = client.get("/api/stats/events", params={"credential_id": cid}).json()["events"]
        accounts = client.get("/api/stats/by-credential").json()["credentials"]
        return (events[0]["credential_name"], accounts[0]["credential_name"], repo.nickname_of(cid))

    assert stats_names() == ("旧名字",) * 3
    assert client.put(_url(cid), json={"nickname": "新名字"}).status_code == 200
    assert stats_names() == ("新名字",) * 3
    before_clear = repo.edit_snapshot(cid)["updated_at"]
    cleared = client.put(_url(cid), json={"nickname": "  \t  "})
    assert cleared.status_code == 200 and cleared.json() == {"nickname": ""}
    assert _row(client, cid)["nickname"] == ""
    assert repo.edit_snapshot(cid)["updated_at"] > before_clear
    assert stats_names() == (None, None, None)
    assert client.get(f"/api/credentials/{cid}/export").json()["nickname"] == ""
    assert client.post("/api/credentials/export", json={"ids": [cid]}).json()[0]["nickname"] == ""
    assert {e["detail"] for e in app.state.audit.query(action=ACTION_CREDENTIAL_NICKNAME)} == {
        "已更新", "（清空）"}


def test_rename_rejects_invalid_input_without_audit_or_touching_row(admin_client):
    app, client = admin_client
    repo = app.state.credentials
    cid = repo.add(provider="trae", nickname="原昵称", credential_data={"token": "synthetic"})
    before = repo.zero_snapshot(cid)
    rejected = [{}, *({"nickname": value} for value in (
        None, True, 1, [], {}, "x" * 129, "换\n行", "空\x00字节", "x\x85y", "x\u2028y", "\ud800"))]
    for payload in rejected:
        # 用 ASCII JSON 发孤立 UTF-16 代理项，避开测试客户端的 UTF-8 编码异常。
        response = client.put(_url(cid), content=json.dumps(payload),
                              headers={"Content-Type": "application/json"})
        assert response.status_code == 400
    assert client.put(_url("missing"), json={"nickname": "普通"}).status_code == 400
    assert repo.zero_snapshot(cid) == before
    assert app.state.audit.query(action=ACTION_CREDENTIAL_NICKNAME) == []
    assert client.put(_url(cid), json={"nickname": "🚀" * 128}).json() == {"nickname": "🚀" * 128}


@pytest.mark.parametrize("role,status", [("operator", 200), ("viewer", 403), (None, 401)])
def test_rename_requires_operator_and_csrf(admin_client, role, status):
    app, client = admin_client
    repo = app.state.credentials
    cid = repo.add(provider="trae", nickname="原昵称", credential_data={"token": "synthetic"})
    client.cookies.clear()
    if role:
        app.state.user_repo.create(role, "unused", role=role)
        client.cookies.set("coding2api_session", create_session_token(role, SECRET))
    if role == "operator":
        rejected = client.put(_url(cid), json={"nickname": "跨站"},
                              headers={"Origin": "https://example.test"})
        assert rejected.status_code == 403
        assert repo.nickname_of(cid) == "原昵称"
        assert app.state.audit.query(action=ACTION_CREDENTIAL_NICKNAME) == []
    response = client.put(_url(cid), json={"nickname": "新昵称"},
                          headers={"Origin": "http://testserver",
                                   "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == status
    assert repo.nickname_of(cid) == ("新昵称" if role == "operator" else "原昵称")
