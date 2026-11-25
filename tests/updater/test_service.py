from types import SimpleNamespace

import pytest

from src.updater.deployment import Deployment, code_manifest
from src.updater.service import UpdateService
from src.updater.state import StateStore

SHA = "b" * 40
LATEST = {"version": "0.1.0", "commit": SHA, "ci": "success"}
IMAGE = "sha256:" + "a" * 64


def make(host, new_source, fail=None, ci=None):
    calls, pending = [], []
    actual = Deployment(host)
    def step(name, work=None):
        def execute(*args):
            calls.append(name)
            if fail == name:
                raise RuntimeError(f"{name} failed")
            return work(*args) if work else None
        return execute
    deployment = SimpleNamespace(current_image=lambda: IMAGE,
        build=step("build", lambda *_: "new-image"), stop=step("stop"),
        backup=step("backup", actual.backup),
        replace_code=step("replace", lambda *_: actual.replace_code(new_source)),
        start=step("start"), restore=step("restore", actual.restore))
    github = SimpleNamespace(latest=lambda _: dict(LATEST, ci=ci or "success"))
    service = UpdateService(host, github=github, deployment=deployment, spawn=pending.append)
    return service, calls, pending


def test_same_version_different_commit_check_and_source(host, new_source):
    service, _, _ = make(host, new_source)
    assert not service.status()["update_available"]
    status = service.dispatch("check", {})
    assert status["update_available"] and status["latest"]["version"] == "0.1.0"
    assert service.dispatch("status", {}) == status
    assert service.dispatch("source", {"source": "other/repo"})["source"] == "other/repo"
    for action, payload in (("evil", {}), ("check", None), ("source", {"source": "a/../../b"}),
                            ("start", {"commit": SHA, "force": 1})):
        with pytest.raises(ValueError):
            service.dispatch(action, payload)
    service.store.save(current={"version": "0.1.0", "commit": SHA})
    assert not service.dispatch("check", {})["update_available"]


@pytest.mark.parametrize("ci", ["pending", "failed"])
def test_ci_never_bypassed_by_force(host, new_source, ci):
    service, calls, _ = make(host, new_source, ci=ci)
    with pytest.raises(ValueError, match="CI"):
        service.dispatch("start", {"commit": SHA, "force": True})
    assert calls == [] and service.status()["job"] is None


def test_stale_target_dirty_force_and_concurrent_start(host, new_source):
    service, calls, pending = make(host, new_source)
    with pytest.raises(ValueError, match="main 已变化"):
        service.dispatch("start", {"commit": "c" * 40})
    (host.source_root / "README.md").write_text("local change")
    with pytest.raises(ValueError, match="本地源码"):
        service.dispatch("start", {"commit": SHA})
    status = service.dispatch("start", {"commit": SHA, "force": True})
    assert status["dirty"] and status["job"]["status"] == "running" and len(pending) == 1
    assert "old_image" not in status["job"]
    for action in ("source", "check", "start"):
        with pytest.raises(ValueError, match="已有更新"):
            service.dispatch(action, {})
    pending[0]()
    assert calls == ["build", "stop", "backup", "replace", "start"]
    assert service.status()["job"]["status"] == "success"
    assert service.status()["current"]["commit"] == SHA
    assert service.store.snapshot()["current_manifest"] == code_manifest(host.source_root)
    assert host.env_file.read_text() == "APP_SECRET=original"


@pytest.mark.parametrize("failure,expected", [("build", "failed"), ("backup", "rolled_back"),
                                              ("replace", "rolled_back"),
                                              ("start", "recovery_failed"),
                                              ("stop", "recovery_failed")])
def test_failure_and_recovery(host, new_source, failure, expected):
    service, calls, pending = make(host, new_source, fail=failure)
    service.dispatch("start", {"commit": SHA})
    pending[0]()
    job = service.status()["job"]
    assert job["status"] == expected and job["finished_at"]
    assert service.status()["current"]["commit"] == "a" * 40
    if expected == "recovery_failed":
        with pytest.raises(ValueError, match="已有更新"):
            service.dispatch("check", {})
        service.deployment.start = lambda *_: None
        service.deployment.stop = lambda: None
        service.recover()
        assert service.status()["job"]["status"] == "rolled_back"
        assert (host.source_root / "README.md").read_text() == "old code"
    elif failure == "backup":
        assert "restore" not in calls


def test_build_rechecks_ci_and_helper_restart(host, new_source):
    service, calls, pending = make(host, new_source)
    service.dispatch("start", {"commit": SHA})
    service.github.latest = lambda _: {**LATEST, "ci": "failed"}
    pending[0]()
    assert service.status()["job"]["status"] == "failed" and calls == ["build"]
    service.github.latest = lambda _: LATEST
    service.dispatch("start", {"commit": SHA})
    service.store.phase("stopping", 45, "stopping")
    # 进程重启重新加载磁盘，当前任务自动恢复，而不是再次构建。
    recovered = UpdateService(host, github=service.github, deployment=service.deployment)
    recovered.recover()
    assert recovered.status()["job"]["status"] == "rolled_back"
    recovered.recover()
    assert recovered.status()["job"]["status"] == "rolled_back"


def test_spawn_failure_and_generic_error(host, new_source):
    service, _, _ = make(host, new_source)
    def fail_spawn(_work):
        raise OSError("secret must not leak")
    service.spawn = fail_spawn
    service.dispatch("start", {"commit": SHA})
    assert service.status()["job"]["error"] == "主机更新步骤失败"
    assert service.status()["job"]["status"] == "failed"


def test_source_edits_during_build_stop_before_drain(host, new_source):
    service, calls, pending = make(host, new_source)
    service.dispatch("start", {"commit": SHA})
    (host.source_root / "README.md").write_text("edit during build")
    pending[0]()
    assert service.status()["job"]["status"] == "failed" and calls == ["build"]


def test_real_concurrent_start_has_one_job(host, new_source):
    from concurrent.futures import ThreadPoolExecutor
    service, _, pending = make(host, new_source)
    def attempt():
        try:
            return service.dispatch("start", {"commit": SHA})["job"]["id"]
        except ValueError:
            return "busy"
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert results.count("busy") == 1 and len(pending) == 1


def test_empty_recovery_and_real_spawn(monkeypatch, host, new_source):
    service, _, _ = make(host, new_source)
    service.recover()
    seen = []
    monkeypatch.setattr("src.updater.service.threading.Thread", lambda **kw:
                        SimpleNamespace(start=lambda: seen.append(kw)))
    UpdateService._spawn(lambda: None)
    assert seen[0]["daemon"] is False


def test_state_snapshot_isolation_durable_and_log_limit(tmp_path):
    store = StateStore(tmp_path)
    store.save(job={"logs": []})
    for _ in range(302):
        store.phase("building", 1, "message")
    assert len(store.snapshot()["job"]["logs"]) == 300
    snapshot = store.snapshot()
    snapshot["job"]["logs"].clear()
    assert len(StateStore(tmp_path).snapshot()["job"]["logs"]) == 300
