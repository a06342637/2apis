"""GitHub 固定提交来源与 CI 门禁，不执行仓库中的运维文档。"""

from __future__ import annotations

import json
import re
import tomllib
from urllib.request import Request, urlopen


def validate_source(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value
    ) or value.split("/")[1] in {".", ".."}:
        raise ValueError("更新仓库必须是合法 GitHub owner/repo")
    return value


def validate_commit(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("更新目标必须是完整的 40 位 Git commit")
    return value


class GitHubSource:
    def __init__(self, read=None):
        self.read = read or self._read

    @staticmethod
    def _read(url: str) -> bytes:
        request = Request(url, headers={"User-Agent": "2apis-updater/0.1.0",
                                       "Accept": "application/vnd.github+json"})
        with urlopen(request, timeout=30) as response:  # nosec B310 - 固定 https GitHub 地址
            return response.read(4 * 1024 * 1024)

    def latest(self, source: str) -> dict:
        source = validate_source(source)
        base = f"https://api.github.com/repos/{source}"
        commit = validate_commit(json.loads(self.read(f"{base}/commits/main"))["sha"])
        content = self.read(f"https://raw.githubusercontent.com/{source}/{commit}/pyproject.toml")
        version = str(tomllib.loads(content.decode())["project"]["version"])
        # 检查名来自已完成的 CI workflow jobs；不接受只造一个 success commit status。
        runs = json.loads(self.read(
            f"{base}/actions/workflows/ci.yml/runs?head_sha={commit}&per_page=100"
        ))["workflow_runs"]
        eligible = [run for run in runs if run["head_sha"] == commit
                    and run["event"] in {"push", "pull_request"}]
        if not eligible:
            return {"version": version, "commit": commit, "ci": "pending"}
        run = max(eligible, key=lambda item: item["id"])
        ci = "pending"
        if run["status"] == "completed":
            ci = "failed"
            if run["conclusion"] == "success":
                jobs = json.loads(self.read(f"{base}/actions/runs/{run['id']}/jobs?per_page=100"))
                required = {"backend", "frontend", "compose", "security", "platform"}
                passed = {job["name"] for job in jobs["jobs"]
                          if job["status"] == "completed" and job["conclusion"] == "success"}
                if required <= passed and all(job["conclusion"] == "success"
                                              for job in jobs["jobs"]) and jobs.get(
                    "total_count", len(jobs["jobs"])) == len(jobs["jobs"]):
                    ci = "success"
        return {"version": version, "commit": commit, "ci": ci}
