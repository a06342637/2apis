"""Qoder 网页 Cookie 录入端点：写入/清除、校验、审计、导出不泄漏。"""

from __future__ import annotations

import uuid
from itertools import count

import pytest

from src.audit.actions import ACTION_CREDENTIAL_WEB_COOKIE
from src.db import repo as repo_module


def _add_qoder(app):
    return app.state.credentials.add(
        provider="qoder",
        credential_data={"access_token": "dt-1", "uid": "u-1", "auth_source": "oauth"})


def _add_codebuddy(app):
    return app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})


@pytest.mark.parametrize(("cookie_value", "force_audit_uuid"), [
    ("synthetic-cookie-secret-do-not-log-42", False),
    ("abc", True),
], ids=["synthetic-secret", "uuid-collision"])
def test_set_and_clear_web_cookie_round_trip(admin_client, monkeypatch, caplog,
                                             cookie_value, force_audit_uuid):
    app, client = admin_client
    credential_id = _add_qoder(app)
    if force_audit_uuid:
        original_id = repo_module._new_id
        sequence = count(0x592)

        def collision_id(prefix):
            if prefix != "audit":
                return original_id(prefix)
            # 有效 v4 UUID，复现发布任务的合法 audit ID 恰好含短 Cookie 值。
            fixed = uuid.UUID(f"faf97156abc54{next(sequence):03x}8000000000000000")
            with monkeypatch.context() as patch:
                patch.setattr(repo_module.uuid, "uuid4", lambda: fixed)
                return original_id(prefix)

        monkeypatch.setattr(repo_module, "_new_id", collision_id)
    cookie = f"qoder_session_cookie={cookie_value}; other=1"

    response = client.put(f"/api/credentials/{credential_id}/web-cookie",
                          json={"cookie": f"  {cookie}  "})
    assert response.status_code == 200 and response.json() == {"ok": True}
    # 落库的是 trim 后的值；其余凭证字段保持原样
    data = app.state.credentials.credential_data(credential_id)
    assert data["web_cookie"] == cookie
    assert data["access_token"] == "dt-1" and data["uid"] == "u-1"
    # 返回体不回显 Cookie
    assert cookie_value not in response.text

    events = app.state.audit.query(action=ACTION_CREDENTIAL_WEB_COOKIE)
    assert len(events) == 1
    assert events[0]["target"] == credential_id
    assert events[0]["detail"] == "已设置"
    if force_audit_uuid:
        assert events[0]["id"] == "audit_faf97156abc54592"
    # id/target 是合法元数据，短 Cookie 值可能恰好出现在随机 UUID 中。
    # 检查所有可承载审计内容的字段，并拒绝额外的凭据字段。
    assert set(events[0]) == {"id", "ts", "actor", "action", "target", "detail", "ip", "ok"}
    payload = {field: value for field, value in events[0].items() if field not in {"id", "target"}}
    assert cookie_value not in str(payload)
    assert cookie not in caplog.text
    assert cookie not in str(app.state.services.log_buffer.snapshot())
    if not force_audit_uuid:
        # 长的非 hex 秘密不会与日志内合法请求/凭证 ID 碰撞，仍验证纯值泄漏。
        assert cookie_value not in caplog.text
        assert cookie_value not in str(app.state.services.log_buffer.snapshot())

    # 空串 = 清除，探测回退汇总口径
    cleared = client.put(f"/api/credentials/{credential_id}/web-cookie",
                         json={"cookie": ""})
    assert cleared.status_code == 200
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == ""
    details = [e["detail"] for e in
               app.state.audit.query(action=ACTION_CREDENTIAL_WEB_COOKIE)]
    assert "已清除" in details


def test_web_cookie_validation_and_capability(admin_client):
    app, client = admin_client
    qoder = _add_qoder(app)
    codebuddy = _add_codebuddy(app)

    # 非字符串 / 超长都 400（不做 Cookie 语法校验，上游结构会变）
    assert client.put(f"/api/credentials/{qoder}/web-cookie",
                      json={"cookie": 1}).status_code == 400
    assert client.put(f"/api/credentials/{qoder}/web-cookie",
                      json={"cookie": "x" * 10_001}).status_code == 400
    # 缺失凭证 / 渠道不支持该能力
    assert client.put("/api/credentials/ghost/web-cookie",
                      json={"cookie": "sid=1"}).status_code == 400
    assert client.put(f"/api/credentials/{codebuddy}/web-cookie",
                      json={"cookie": "sid=1"}).status_code == 400


def test_export_omits_web_cookie(admin_client):
    """导出件本来就有明文 token，不该再多一份浏览器会话凭据。"""
    app, client = admin_client
    credential_id = _add_qoder(app)
    client.put(f"/api/credentials/{credential_id}/web-cookie",
               json={"cookie": "qoder_session_cookie=abc"})

    exported = client.get(f"/api/credentials/{credential_id}/export").json()
    assert "web_cookie" not in exported["credential"]
    # 落库值仍在（导出只是脱这项）
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == (
        "qoder_session_cookie=abc")

def _add_codearts(app):
    return app.state.credentials.add(
        provider="codearts",
        credential_data={"access_key_id": "AK", "secret_access_key": "SK"})


def test_codearts_web_cookie_and_export_omits_it(admin_client):
    """CodeArts 同样支持网页 Cookie；导出件不得带浏览器会话凭据。"""
    app, client = admin_client
    credential_id = _add_codearts(app)

    response = client.put(f"/api/credentials/{credential_id}/web-cookie",
                          json={"cookie": "  devclouddevuibjJ_SESSION_ID=abc  "})
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert "abc" not in response.text
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == (
        "devclouddevuibjJ_SESSION_ID=abc")

    exported = client.get(f"/api/credentials/{credential_id}/export").json()
    assert "web_cookie" not in exported["credential"]
    assert exported["credential"]["access_key_id"] == "AK"


def test_list_exposes_web_cookie_state_and_setting_resets_it(admin_client):
    """管理台提示走列表字段；重新粘贴后立即清掉旧标记（下轮探测再定）。"""
    import time

    from src.provider.base import Quota

    app, client = admin_client
    credential_id = _add_codearts(app)
    app.state.credentials.save_quota(
        credential_id,
        Quota(remaining=1.0, total=2.0, probed_at=int(time.time()),
              web_cookie_state="expired"))

    rows = client.get("/api/credentials").json()["credentials"]
    row = next(item for item in rows if item["id"] == credential_id)
    assert row["web_cookie_state"] == "expired"

    response = client.put(f"/api/credentials/{credential_id}/web-cookie",
                          json={"cookie": "sid=new"})
    assert response.status_code == 200
    assert app.state.credentials.credential_data(credential_id)["web_cookie"] == "sid=new"
    rows = client.get("/api/credentials").json()["credentials"]
    assert next(item for item in rows if item["id"] == credential_id)[
        "web_cookie_state"] is None


def test_set_web_cookie_holds_edit_lock(admin_client):
    """写入必须持凭证编辑锁：`save_credential_data` 是整份覆盖。

    不加锁时 RefreshTask 的「读 → await 上游 → 整份覆写」会把刚设置的
    cookie 用旧快照覆盖回去（一次性 refresh_token 场景下凭证直接失效）。
    """
    app, client = admin_client
    credential_id = _add_qoder(app)
    lock = app.state.credentials.edit_lock(credential_id)

    seen: list[bool] = []
    original_set = app.state.credentials.set_web_cookie_state

    def spy(*args, **kwargs):
        seen.append(lock.locked())          # 锁内才能看到 locked()=True
        return original_set(*args, **kwargs)

    app.state.credentials.set_web_cookie_state = spy
    try:
        response = client.put(f"/api/credentials/{credential_id}/web-cookie",
                              json={"cookie": "sid=1"})
    finally:
        app.state.credentials.set_web_cookie_state = original_set
    assert response.status_code == 200
    assert seen == [True]
    assert lock.locked() is False           # 出锁归还
