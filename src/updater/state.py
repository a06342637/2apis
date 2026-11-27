"""主机持久化进度：页面刷新、应用换容器不丢更新任务。"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "state.json"
        self.lock = threading.RLock()
        self.value = (json.loads(self.path.read_text(encoding="utf-8"))
                      if self.path.exists() else {"job": None})

    def save(self, **values) -> None:
        with self.lock:
            self.value.update(values)
            temp = self.path.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as handle:
                os.chmod(temp, 0o600)
                json.dump(self.value, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)

    def phase(self, name: str, progress: int, message: str, **values) -> None:
        with self.lock:
            now = int(time.time())
            job = dict(self.value["job"])
            job.update(phase=name, progress=progress, step=message, step_started_at=now, **values)
            job["logs"] = [*job["logs"], {"time": now, "message": message}][-300:]
            self.save(job=job)

    def activity(self, job_id: str, message: str | None = None,
                 progress: int | None = None) -> None:
        """只记录固定步骤说明及等待心跳，不下发命令输出或私有配置。"""
        with self.lock:
            current = self.value.get("job")
            if not current or current["id"] != job_id or current["status"] != "running":
                return
            now = int(time.time())
            job = dict(current)
            if message is not None:
                job.update(step=message, step_started_at=now)
                if progress is not None:
                    job["progress"] = max(job["progress"], progress)
            else:
                elapsed = max(0, now - job.get("step_started_at", job["started_at"]))
                message = f"等待当前步骤完成，已持续 {elapsed} 秒；主机助手仍在响应"
            job["heartbeat_at"] = now
            job["logs"] = [*job["logs"], {"time": now, "message": message}][-300:]
            self.save(job=job)

    def snapshot(self) -> dict:
        with self.lock:
            # 返回副本，worker 继续更新时不影响已发送的响应。
            return json.loads(json.dumps(self.value))
