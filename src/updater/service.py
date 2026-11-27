"""独立 helper 的持久化更新状态机：构建、排空、备份、切换、回滚。"""

from __future__ import annotations

import threading
import time
import uuid

from .deployment import Deployment, HostConfig, code_manifest
from .source import GitHubSource, validate_commit, validate_source
from .state import StateStore


class UpdateService:
    def __init__(self, config: HostConfig, github=None, deployment=None, spawn=None):
        config.validate()
        self.config = config
        self.store = StateStore(config.state_dir)
        self.github = github or GitHubSource()
        self.deployment = deployment or Deployment(config)
        self.spawn = spawn or self._spawn
        self.control = threading.Lock()
        state = self.store.snapshot()
        if "source" not in state:
            self.store.save(source=config.source, latest=None, dirty=False,
                            current={"version": config.current_version,
                                     "commit": config.current_commit},
                            current_manifest=code_manifest(config.source_root))

    @staticmethod
    def _spawn(work) -> None:
        threading.Thread(target=work, name="2apis-update", daemon=False).start()

    def status(self) -> dict:
        state = self.store.snapshot()
        latest = state.get("latest")
        job = state["job"]
        # 内部恢复路径、image、hash 清单不下发应用。
        public_job = None
        if job:
            public_job = {key: job[key] for key in ("id", "status", "phase", "progress", "logs",
                          "error", "started_at", "finished_at")}
        return {"enabled": True, "source": state["source"], "branch": "main",
                "current": state["current"], "latest": latest, "dirty": state["dirty"],
                "update_available": bool(latest and latest != {**state["current"],
                                                                "ci": latest["ci"]}),
                "job": public_job}

    def _check(self) -> dict:
        state = self.store.snapshot()
        latest = self.github.latest(state["source"])
        dirty = code_manifest(self.config.source_root) != state["current_manifest"]
        self.store.save(latest=latest, dirty=dirty)
        return latest

    def dispatch(self, action: str, payload: dict) -> dict:
        if action == "status":
            return self.status()
        if action not in {"check", "source", "start"} or not isinstance(payload, dict):
            raise ValueError("不支持的更新动作")
        with self.control:
            state = self.store.snapshot()
            if state["job"] and state["job"]["status"] in {"running", "recovery_failed"}:
                raise ValueError("已有更新正在执行，或回滚待主机管理员修复")
            if action == "check":
                self._check()
            elif action == "source":
                source = validate_source(payload.get("source"))
                self.store.save(source=source, latest=None)
            else:
                commit = validate_commit(payload.get("commit"))
                force = payload.get("force", False)
                if not isinstance(force, bool):
                    raise ValueError("force 必须是布尔值")
                latest = self._check()
                if latest["commit"] != commit:
                    raise ValueError("main 已变化，请重新检查并选择最新提交")
                if latest["ci"] != "success":
                    raise ValueError("更新目标 CI 尚未全部通过，禁止更新（强制也不绕过 CI）")
                if self.store.snapshot()["dirty"] and not force:
                    raise ValueError("本地源码已修改；勾选覆盖本地源码后重试，修改会先备份")
                old_image = self.deployment.current_image()
                job_id = uuid.uuid4().hex
                self.store.save(job={"id": job_id, "status": "running", "phase": "queued",
                                     "progress": 0, "logs": [], "error": "",
                                     "started_at": int(time.time()), "finished_at": None,
                                     "old_image": old_image, "backup_ready": False,
                                     "source": state["source"], "target": latest,
                                     "force": force,
                                     "old_current": state["current"],
                                     "old_manifest": state["current_manifest"]})
                try:
                    self.spawn(self._execute)
                except Exception as error:
                    self._fail(error)
        return self.status()

    def _execute(self) -> None:
        job = self.store.snapshot()["job"]
        directory = self.config.state_dir / job["id"]
        try:
            directory.mkdir(mode=0o700)
            self.store.phase("building", 10, "正在获取固定提交并构建镜像，旧服务继续运行")
            image = self.deployment.build(job["source"], job["target"]["commit"],
                                          directory / "source")
            # 构建可能很久；切换前重新判定同一提交仍在 main、CI 未被新失败重跑覆盖。
            latest = self.github.latest(job["source"])
            if latest["commit"] != job["target"]["commit"] or latest["ci"] != "success":
                raise ValueError("构建后目标 main/CI 已变化，停止切换")
            if not job["force"] and code_manifest(self.config.source_root) != job["old_manifest"]:
                raise ValueError("构建期间本地源码发生修改，停止切换，请重新检查")
            self.store.phase("stopping", 45, "构建完成，停止旧服务并等待在途请求排空")
            self.deployment.stop()
            self.store.phase("backup", 55, "正在备份并校验源码、SQLite、密钥与部署配置")
            self.deployment.backup(directory / "backup")
            self.store.phase("switching", 70, "备份校验通过，正在切换源码与镜像", backup_ready=True)
            self.deployment.replace_code(directory / "source")
            self.store.phase("starting", 85, "正在启动新服务并等待健康检查")
            self.deployment.start(image)
            self.store.save(current={key: job["target"][key] for key in ("version", "commit")},
                            current_manifest=code_manifest(self.config.source_root), dirty=False)
            self.store.phase("done", 100, "更新成功，账号、API Key 与原密钥已保留",
                             status="success", finished_at=int(time.time()))
        except Exception as error:
            self._fail(error)

    def _fail(self, error: Exception) -> None:
        job = self.store.snapshot()["job"]
        message = (str(error) if isinstance(error, (ValueError, RuntimeError))
                   else "主机更新步骤失败")
        if job["phase"] in {"queued", "building"}:
            self.store.phase("done", 100, "更新失败，旧服务继续运行", status="failed",
                             error=message, finished_at=int(time.time()))
            return
        try:
            self.store.phase("rollback", 90, "更新中断，正在回滚原镜像与已校验备份", error=message)
            self.deployment.stop()
            if job["backup_ready"]:
                self.deployment.restore(self.config.state_dir / job["id"] / "backup")
            self.deployment.start(job["old_image"])
            self.store.save(current=job["old_current"], current_manifest=job["old_manifest"])
            self.store.phase("done", 100, "已回滚，原服务健康检查通过", status="rolled_back",
                             finished_at=int(time.time()))
        except Exception:
            self.store.phase("rollback", 90, "自动回滚未完成，请主机管理员检查助手日志与备份",
                             status="recovery_failed", finished_at=int(time.time()))

    def recover(self) -> None:
        """helper 被杀/重启后继续安全恢复，不重复执行未知完成度的更新。"""
        state = self.store.snapshot()
        if state["job"] and state["job"]["status"] in {"running", "recovery_failed"}:
            if state["job"]["phase"] == "building":
                try:
                    self.deployment.stop_interrupted_build()
                except Exception:
                    # 保留 building 阶段供下次重试清理，不能误入已切换镜像的回滚路径。
                    self.store.phase("building", 10,
                        "中断构建器回收失败，旧服务继续运行，请主机管理员检查",
                        status="recovery_failed", error="主机助手重启，无法停止中断的构建器",
                        finished_at=int(time.time()))
                    return
            self._fail(RuntimeError("主机助手重启，恢复中断的更新"))
