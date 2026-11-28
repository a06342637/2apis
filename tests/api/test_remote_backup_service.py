"""Scheduled and manual backups against synthetic storage."""
from contextlib import contextmanager
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.db.crypto import CredentialCipher
from src.remote_backup import service, transports
from src.remote_backup.config import BackupError, Config, Oss, Sftp, Store, merge_config
from tests.helpers.constants import SECRET

OSS = {"enabled": True, "bucket": "example-bucket", "key_id": "fixture-key",
       "key_secret": "fixture-secret", "keep": 1}
SFTP = {"enabled": True, "host": "example.test", "username": "backup", "password": "fixture",
        "fingerprint": "SHA256:" + "a" * 43}


class Files:
    def __init__(self):
        self.saved = {"unrelated.sqlite3": b"foreign",
                      "coding2api-20201128T000000Z-0123456789ab.sqlite3": b"old"}
        self.fail_put = False
        self.fail_delete = False

    def entries(self):
        for name, body in self.saved.items():
            yield name, len(body), 100

    def put(self, name, path):
        if self.fail_put:
            raise OSError("confidential network diagnostics")
        self.saved[name] = path.read_bytes()

    def get(self, name, path):
        return transports.copy_bounded(BytesIO(self.saved[name]), path)

    def delete(self, name):
        if self.fail_delete:
            raise OSError("no permission")
        del self.saved[name]


@pytest.fixture
def job(tmp_path, monkeypatch):
    def snapshot(_db, *, directory, keep):
        assert keep == 0
        path = Path(directory) / "synthetic.sqlite3"
        path.write_bytes(b"test database")
        return path

    def stage(pending, path, *, filename, size):
        pending.write_bytes(path.read_bytes())
        return {"filename": filename, "size": size}

    files = {Oss: Files(), Sftp: Files()}

    @contextmanager
    def target(config, namespace):
        assert namespace.startswith("coding2api-")
        yield files[type(config)]

    monkeypatch.setattr(service, "backup_database", snapshot)
    monkeypatch.setattr(service, "open_target", target)
    moment = [int(datetime(2026, 11, 28, 5, 0, tzinfo=UTC).timestamp())]
    store = Store(tmp_path / "remote", CredentialCipher(SECRET))
    backup = service.RemoteBackups(str(tmp_path / "app.sqlite3"), store, stage,
                                   clock=lambda: moment[0])
    return backup, files, moment


@pytest.mark.parametrize("payload", [
    {"timezone": "nonexistent/zone"}, {"oss": {"directory": "foo/../bar"}},
    {"enabled": True}, {"oss": {"enabled": True}}, {"sftp": {"enabled": True}},
    {"oss": {"endpoint": "localhost"}}, {"sftp": {"host": "http://example.test"}},
])
def test_validation_rejects_bad_destinations_and_plan(payload):
    with pytest.raises(ValidationError):
        merge_config(Config(), payload)


async def test_manual_and_catchup_are_separate_and_save_no_plaintext(job, caplog):
    backup, files, moment = job
    assert backup.status()["config"]["enabled"] is False
    with pytest.raises(BackupError, match="至少一个"):
        backup.start()
    backup.save({"enabled": True, "oss": OSS, "sftp": SFTP, "time": "04:30"})
    backup.save({"oss": {"key_secret": ""}})
    data = backup.status()
    assert "key_secret" not in data["config"]["oss"]
    assert data["config"]["oss"]["has_key_secret"]
    assert b"fixture-secret" not in (backup.store.directory / "config.enc").read_bytes()
    assert backup.config().oss.key_secret == "fixture-secret"
    assert backup.namespace() == backup.namespace()
    scheduled = backup.store.state()["last_slot"]
    files[Sftp].fail_put = True
    backup.start()
    assert backup.status()["running"]
    with pytest.raises(BackupError, match="正在执行"):
        backup.start()
    await backup.job
    result = backup.status()["last_run"]
    assert result["scheduled"] is False
    assert [row["status"] for row in result["targets"]] == ["success", "failed"]
    assert backup.store.state()["last_slot"] == scheduled
    assert "unrelated.sqlite3" in files[Oss].saved
    assert len(files[Oss].saved) == 2
    assert "confidential network diagnostics" not in caplog.text
    assert files[Sftp].saved.keys() == {
        "unrelated.sqlite3", "coding2api-20201128T000000Z-0123456789ab.sqlite3"}
    moment[0] += 4 * 86400
    files[Sftp].fail_put = False
    files[Oss].fail_delete = True
    backup.tick()
    await backup.job
    assert backup.status()["last_run"]["scheduled"] is True
    assert backup.status()["last_run"]["targets"][0]["status"] == "warning"
    previous = backup.job
    backup.tick()
    assert backup.job is previous


async def test_list_test_restore_delete_are_namespace_scoped(job):
    backup, _, _ = job
    backup.save({"oss": OSS})
    with pytest.raises(BackupError, match="先保存"):
        backup.operate("sftp", "list")
    with pytest.raises(BackupError, match="未知"):
        backup.operate("missing", "list")
    with pytest.raises(BackupError, match="文件名"):
        backup.operate("oss", "delete", "../file")
    assert backup.operate("oss", "test") == {"ok": True}
    entries = backup.operate("oss", "list")["backups"]
    assert len(entries) == 1
    name = entries[0]["name"]
    restored = backup.operate("oss", "restore", name)
    assert restored["restart_required"] and restored["filename"] == name
    with pytest.raises(BackupError, match="已有待恢复"):
        backup.operate("oss", "restore", name)
    assert backup.operate("oss", "delete", name) == {"ok": True}
    with pytest.raises(BackupError, match="远端操作失败"):
        backup.operate("oss", "delete", name)
    assert not backup.operate("oss", "list")["backups"]


def test_size_limit_and_host_key_verification(tmp_path, monkeypatch):
    import base64
    import hashlib
    from types import SimpleNamespace

    monkeypatch.setattr(transports, "MAX_BYTES", 3)
    with pytest.raises(BackupError, match="超过"):
        transports.copy_bounded(BytesIO(b"abcd"), tmp_path / "output")
    key = SimpleNamespace(asbytes=lambda: b"public")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(b"public").digest()).decode()[:-1]
    transports.HostKey(fingerprint).missing_host_key(None, "test", key)
    with pytest.raises(BackupError, match="指纹"):
        transports.HostKey("not-matching").missing_host_key(None, "test", key)

def test_admin_routes_and_log_filter(job, admin_client):
    from src.api.admin_remote_backups import create_router
    from src.api.deps import principal_from_request
    from src.auth.rbac import Principal

    backup, _, _ = job
    app, client = admin_client
    # Independent router namespace with the same authentication and exception handlers.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.webapp.handlers import register_exception_handlers

    route_app = FastAPI()
    route_app.include_router(create_router(backup))
    register_exception_handlers(route_app)
    current = [Principal("reader", role="viewer")]
    route_app.dependency_overrides[principal_from_request] = lambda: current[0]
    with TestClient(route_app) as browser:
        root = "/api/remote-backups"
        assert browser.get(root).status_code == 403
        current[0] = Principal("root", True)
        assert browser.get(root).headers["cache-control"] == "no-store"
        assert browser.put(root, json={"enabled": True}).status_code == 400
        assert browser.post(root + "/run").status_code == 400
        assert browser.put(root, json={"oss": OSS}).json()["config"]["oss"]["has_key_secret"]
        assert browser.post(root + "/oss/test").json()["ok"]
        name = browser.get(root + "/oss/files").json()["backups"][0]["name"]
        assert browser.get(f"{root}/oss/files/{name}").content == b"old"
        assert browser.get(f"{root}/oss/files/invalid").status_code == 400
        assert browser.post(f"{root}/oss/files/{name}/restore").json()["staged"]
        assert browser.delete(f"{root}/oss/files/{name}").json()["ok"]
        assert browser.post(root + "/run").status_code == 202
        browser.portal.call(backup.close, browser.portal.call(_idle_scheduler))
        (backup.store.directory / "state.json").write_text("not json")
        assert browser.get(root).status_code == 400
    log_buffer = app.state.log_buffer
    repo = app.state.services.log_repo
    assert repo is not None
    log_buffer.append(level="INFO", logger_name="src.remote_backup.service", message="备份已完成")
    log_buffer.append(level="INFO", logger_name="src.credentials.miora_lifecycle",
                      message="积分恢复")
    assert repo.flush()
    persisted = client.get("/api/logs?category=backup").json()
    assert persisted["persisted"]
    assert any(row["message"] == "备份已完成" for row in persisted["lines"])
    app.state.services.log_repo = None
    rows = client.get("/api/logs?category=backup").json()["lines"]
    assert any(row["message"] == "备份已完成" for row in rows)
    assert not any(row["logger"] == "src.credentials.miora_lifecycle" for row in rows)


async def _idle_scheduler():
    import asyncio

    return asyncio.create_task(asyncio.sleep(60))


def test_schedule_before_today_time_and_invalid_state(job):
    backup, _, _ = job
    now = int(datetime(2026, 11, 27, 19, 1, tzinfo=UTC).timestamp())
    last, nxt = service.slots(Config(time="04:30"), now)
    assert last < now < nxt and nxt - last == 86400
    backup.store.directory.mkdir()
    (backup.store.directory / "config.enc").write_bytes(b"corrupt")
    with pytest.raises(BackupError, match="原部署密钥"):
        backup.config()
    (backup.store.directory / "instance").write_text("corrupt")
    with pytest.raises(BackupError, match="实例标识"):
        backup.namespace()


async def test_scheduler_shuts_down_and_bad_state_does_not_stop_it(job, monkeypatch, caplog):
    import asyncio

    backup, _, _ = job
    backup.save({"enabled": True, "oss": OSS})
    backup.tick()
    assert backup.job is None
    (backup.store.directory / "state.json").write_text("bad")
    original = asyncio.sleep
    reached = asyncio.Event()

    async def sleep(_seconds):
        reached.set()
        await original(60)

    monkeypatch.setattr(service.asyncio, "sleep", sleep)
    scheduler = asyncio.create_task(backup.schedule())
    await asyncio.wait_for(reached.wait(), 1)
    await backup.close(scheduler)
    assert scheduler.cancelled()
    assert "无法读取定时任务" in caplog.text


async def test_snapshot_validation_and_status_write_failures(job, monkeypatch, caplog):
    backup, _, _ = job
    backup.save({"oss": OSS})
    backup.tick()
    assert backup.job is None
    monkeypatch.setattr(service, "MAX_BYTES", 1)
    backup.start()
    backup.tick()
    await backup.job
    assert "512 MB" in backup.status()["last_run"]["error"]
    monkeypatch.setattr(service, "MAX_BYTES", 100)

    def fail(*args, **kwargs):
        raise OSError("sensitive path")

    monkeypatch.setattr(service, "backup_database", fail)
    backup.start()
    await backup.job
    assert backup.status()["last_run"]["error"] == "创建数据库快照失败"
    monkeypatch.setattr(backup.store, "save_state", fail)
    backup.start()
    await backup.job
    assert "无法保存本轮状态" in caplog.text
    assert "sensitive path" not in caplog.text


def test_download_failure_and_pending_restore_race(job, tmp_path, monkeypatch):
    backup, files, _ = job
    backup.save({"oss": OSS})
    with pytest.raises(BackupError, match="下载失败"):
        backup.download("oss", "coding2api-20201128T000000Z-abcdefabcdef.sqlite3", tmp_path / "bad")
    remote = files[Oss]
    get = remote.get

    def concurrent_restore(name, path):
        size = get(name, path)
        pending = service.pending_restore_path(backup.db_path)
        pending.parent.mkdir()
        pending.touch()
        return size

    monkeypatch.setattr(remote, "get", concurrent_restore)
    name = backup.operate("oss", "list")["backups"][0]["name"]
    with pytest.raises(BackupError, match="已有待恢复"):
        backup.operate("oss", "restore", name)
