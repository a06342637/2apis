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
        self.value = json.loads(self.path.read_text()) if self.path.exists() else {"job": None}

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
            job = dict(self.value["job"])
            job.update(phase=name, progress=progress, **values)
            job["logs"] = [*job["logs"], {"time": int(time.time()), "message": message}][-300:]
            self.save(job=job)

    def snapshot(self) -> dict:
        with self.lock:
            # 返回副本，worker 继续更新时不影响已发送的响应。
            return json.loads(json.dumps(self.value))
