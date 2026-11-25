import json
from io import BytesIO

import pytest

from src.updater.source import GitHubSource, validate_commit, validate_source

SHA = "b" * 40


@pytest.mark.parametrize("source", ["", "../evil", "x/..", "https://github.com/x/y",
                                    "x/y;touch", "-x/y", "x/-y", None, 1, "x/y/z"])
def test_source_rejects_shell_and_path(source):
    with pytest.raises(ValueError):
        validate_source(source)


def test_valid_source_commit():
    assert validate_source("a06342637/2apis") == "a06342637/2apis"
    assert validate_commit(SHA) == SHA
    for value in (None, "main", "-abc", "a" * 39, "A" * 40):
        with pytest.raises(ValueError):
            validate_commit(value)


def github(runs, jobs=None, total_count=None):
    def read(url):
        if url.endswith("commits/main"):
            return json.dumps({"sha": SHA}).encode()
        if url.endswith("pyproject.toml"):
            assert f"/{SHA}/" in url
            return b'[project]\nversion="0.1.0"'
        if "/jobs?" in url:
            result = {"jobs": jobs}
            if total_count is not None:
                result["total_count"] = total_count
            return json.dumps(result).encode()
        return json.dumps({"workflow_runs": runs}).encode()
    return GitHubSource(read)


def run(**values):
    return {"id": 1, "head_sha": SHA, "event": "push", "status": "completed",
            "conclusion": "success", **values}


def job(name, **values):
    return {"name": name, "status": "completed", "conclusion": "success", **values}


def test_ci_requires_all_jobs_and_latest_run():
    jobs = [job(name) for name in ("backend", "frontend", "compose", "security", "platform")]
    assert github([run()], jobs).latest("a/b") == {
        "version": "0.1.0", "commit": SHA, "ci": "success"}
    assert github([run()], jobs[:-1]).latest("a/b")["ci"] == "failed"
    assert github([run()], jobs, 101).latest("a/b")["ci"] == "failed"
    assert github([run()], [*jobs, job("extra", conclusion="skipped")]).latest("a/b")[
        "ci"] == "failed"
    assert github([run()], [job("backend", status="queued")]).latest("a/b")["ci"] == "failed"
    assert github([run(), run(id=2, conclusion="failure")], jobs).latest("a/b")["ci"] == "failed"
    assert github([run(status="in_progress")]).latest("a/b")["ci"] == "pending"
    assert github([]).latest("a/b")["ci"] == "pending"
    assert github([run(head_sha="c" * 40), run(event="workflow_dispatch")]).latest("a/b")[
        "ci"] == "pending"


def test_fixed_https_reader(monkeypatch):
    seen = []
    def open_request(request, timeout):
        seen.append((request.full_url, timeout, request.headers))
        return BytesIO(b"ok")
    monkeypatch.setattr("src.updater.source.urlopen", open_request)
    assert GitHubSource().read("https://api.github.com/repos/a/b") == b"ok"
    assert seen[0][1] == 30
