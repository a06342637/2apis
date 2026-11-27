"""等待日志有界且属于原任务；心跳不表示构建完成，也不泄露命令输出。"""
import threading
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from src.updater import activity
from src.updater.deployment import Deployment
from src.updater.service import UpdateService
from src.updater.state import StateStore
from tests.updater.test_build_resources import runner
from tests.updater.test_deployment import SHA
from tests.updater.test_service import make


def store_job(tmp_path):
    store = StateStore(tmp_path / "activity-state")
    store.save(job={"id": "one", "status": "running", "phase": "building", "progress": 10,
                    "logs": [], "started_at": 100, "finished_at": None, "error": ""})
    return store


def test_step_and_wait_logs_persist_without_faking_progress_and_keep_300_entries(
        tmp_path, monkeypatch):
    store = store_job(tmp_path)
    monkeypatch.setattr("src.updater.state.time.time", lambda: 110)
    store.activity("one", "正在构建镜像", 25)
    store.activity("one", "正在等待镜像", 14)
    assert store.snapshot()["job"]["progress"] == 25
    monkeypatch.setattr("src.updater.state.time.time", lambda: 145)
    for _ in range(305):
        store.activity("one")
    job = StateStore(store.directory).snapshot()["job"]
    assert len(job["logs"]) == 300
    assert job["step"] == "正在等待镜像" and job["step_started_at"] == 110
    assert job["heartbeat_at"] == 145 and job["progress"] == 25
    assert "35 秒" in job["logs"][-1]["message"]
    store.phase("starting", 85, "等待健康检查")
    assert store.snapshot()["job"]["step_started_at"] == 145


def test_legacy_job_wait_clock_clamps_future_start_and_message_without_percentage(
        tmp_path, monkeypatch):
    store = store_job(tmp_path)
    monkeypatch.setattr("src.updater.state.time.time", lambda: 90)
    store.activity("one")
    assert "0 秒" in store.snapshot()["job"]["logs"][-1]["message"]
    store.activity("one", "校验源码")
    assert store.snapshot()["job"]["progress"] == 10


@pytest.mark.parametrize("state", [None, "different", "success"])
def test_late_activity_cannot_touch_finished_or_replaced_job(tmp_path, state):
    store = store_job(tmp_path)
    job = store.snapshot()["job"]
    if state is None:
        job = None
    elif state == "different":
        job["id"] = "two"
    else:
        job["status"] = state
    store.save(job=job)
    before = store.path.read_bytes()
    store.activity("one", "旧任务迟到日志", 40)
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("failure", [False, True])
def test_real_heartbeat_reports_pending_work_and_joins_on_exit(monkeypatch, failure):
    monkeypatch.setattr(activity, "ACTIVITY_INTERVAL_SECONDS", 0.01)
    received = threading.Event()
    calls = []
    def report(job_id):
        calls.append(job_id)
        received.set()
    with (pytest.raises(RuntimeError) if failure else nullcontext(),
          activity.report_activity(SimpleNamespace(activity=report), "one")):
        assert received.wait(15)
        if failure:
            raise RuntimeError("synthetic work failure")
    assert calls and set(calls) == {"one"}
    assert not any(thread.name == "2apis-update-activity" for thread in threading.enumerate())


def test_service_activity_start_failure_is_terminal_and_does_not_stop_app(
        host, new_source, monkeypatch):
    service, calls, pending = make(host, new_source)
    service.dispatch("start", {"commit": SHA})
    def fail(_self):
        raise OSError("private-start-error")
    monkeypatch.setattr(activity.Thread, "start", fail)
    pending[0]()
    job = service.status()["job"]
    assert job["status"] == "failed" and "private-start-error" not in job["error"]
    assert calls == []


def test_real_service_wires_build_steps_and_public_optional_activity_fields(
        host, tmp_path, monkeypatch):
    service = UpdateService(host)
    service.store.save(job={"id": "one", "status": "running", "phase": "building", "progress": 10,
                           "logs": [], "started_at": 100, "finished_at": None, "error": ""})
    stage = tmp_path / "observed-stage"
    service.deployment.run, _calls = runner(stage)
    monkeypatch.setattr("src.updater.state.time.time", lambda: 200)
    assert service.deployment.build("a/b", SHA, stage) == f"2apis-update:{SHA}"
    status = service.status()
    messages = [row["message"] for row in status["job"]["logs"]]
    assert len(messages) == 6 and "GitHub" in messages[0] and "收尾" in messages[-1]
    assert status["job"]["progress"] == 40 and status["job"]["heartbeat_at"] == 200
    assert status["server_time"] == 200 and "old_manifest" not in status["job"]
    assert service.deployment.progress is not None
    # 默认直接 Deployment 调用仍兼容已有 runner，不要求订阅日志。
    assert Deployment(host).progress(10, "固定说明") is None
