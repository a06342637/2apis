"""批量删除只影响所选账号，逐条隔离失败，并保留权限、CSRF和审计边界。"""

import pytest

from src.api.admin_credentials import BATCH_MAX_IDS
from tests.api.test_admin_credentials_batch import _add
from tests.api.test_admin_credentials_batch import batch_app as batch_app


def test_batch_delete_deduplicates_preserves_order_and_audits_only_deleted_targets(batch_app):
    app, client = batch_app
    first, second, untouched = (_add(app) for _ in range(3))
    response = client.post("/api/credentials/batch", json={
        "action": "delete", "ids": [second, first, "missing", first, " ", second],
    })
    assert response.status_code == 200
    assert response.json() == {
        "action": "delete", "total": 3, "succeeded": 2, "failed": 1,
        "results": [{"id": second, "ok": True}, {"id": first, "ok": True},
                    {"id": "missing", "ok": False, "error": "credential not found"}],
    }
    remaining = {row["id"] for row in app.state.credentials.list_all()}
    assert untouched in remaining and first not in remaining and second not in remaining
    events = app.state.audit.query(action="credential.delete")
    assert len(events) == 3 and {event["target"] for event in events} == {first, second, None}
    assert next(event for event in events if event["target"] is None)["detail"] == (
        "批量 delete：2/3 成功")
    provider = app.state.executor._deps.providers["codebuddy"]
    assert provider.probed == provider.checked_in == []


def test_batch_delete_database_failure_does_not_delete_or_hide_other_results(
    batch_app, monkeypatch,
):
    app, client = batch_app
    bad, good = _add(app), _add(app)
    original = app.state.credentials.delete

    def delete(cid):
        if cid == bad:
            raise RuntimeError("synthetic-private-delete-error")
        return original(cid)

    monkeypatch.setattr(app.state.credentials, "delete", delete)
    response = client.post("/api/credentials/batch", json={"action": "delete", "ids": [bad, good]})
    assert response.status_code == 200
    assert response.json()["results"] == [
        {"id": bad, "ok": False, "error": "action_failed"}, {"id": good, "ok": True}]
    assert "synthetic-private-delete-error" not in response.text
    remaining = {row["id"] for row in app.state.credentials.list_all()}
    assert bad in remaining and good not in remaining


def test_batch_delete_audit_failure_still_reports_completed_deletion(batch_app, monkeypatch):
    app, client = batch_app
    cid = _add(app)

    def audit_failure(**_kwargs):
        raise RuntimeError("synthetic-private-audit-error")

    monkeypatch.setattr(app.state.audit, "record", audit_failure)
    response = client.post("/api/credentials/batch", json={"action": "delete", "ids": [cid]})
    assert response.status_code == 200
    assert response.json()["results"] == [{"id": cid, "ok": True}]
    assert response.json()["succeeded"] == 1 and response.json()["failed"] == 0
    assert "synthetic-private-audit-error" not in response.text
    assert cid not in {row["id"] for row in app.state.credentials.list_all()}


@pytest.mark.parametrize("identity, status", [("anonymous", 401), ("viewer", 403)])
def test_batch_delete_requires_operator_before_any_mutation(batch_app, identity, status):
    app, client = batch_app
    cid = _add(app)
    if identity == "anonymous":
        client.cookies.clear()
    else:
        assert client.post("/api/auth/login", json={
            "username": "guest", "password": "guestpw"}).status_code == 200
    response = client.post("/api/credentials/batch", json={"action": "delete", "ids": [cid]})
    assert response.status_code == status
    assert cid in {row["id"] for row in app.state.credentials.list_all()}
    assert app.state.audit.query(action="credential.delete") == []


def test_batch_delete_rejects_cross_origin_before_any_mutation(batch_app):
    app, client = batch_app
    cid = _add(app)
    response = client.post("/api/credentials/batch", headers={"Origin": "https://evil.example"},
                           json={"action": "delete", "ids": [cid]})
    assert response.status_code == 403
    assert cid in {row["id"] for row in app.state.credentials.list_all()}


def test_batch_delete_rejects_oversized_batch_before_any_mutation(batch_app):
    app, client = batch_app
    cid = _add(app)
    response = client.post("/api/credentials/batch", json={"action": "delete", "ids": [cid] + [
        f"missing-{index}" for index in range(BATCH_MAX_IDS)]})
    assert response.status_code == 400
    assert cid in {row["id"] for row in app.state.credentials.list_all()}
