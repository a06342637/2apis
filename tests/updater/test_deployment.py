import json
import shutil
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from src.updater.deployment import (
    Deployment,
    HostConfig,
    code_manifest,
    copy_path,
    manifest,
    preserve_owner,
    readable_code,
    run_command,
    verify_backup,
)

IMAGE = "sha256:" + "a" * 64
SHA = "b" * 40


def test_load_and_validate_config(host, tmp_path):
    values = asdict(host)
    for key, value in values.items():
        if key == "compose_files":
            values[key] = [str(path) for path in value]
        elif hasattr(value, "as_posix"):
            values[key] = str(value)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(values))
    assert HostConfig.load(path) == host
    replace(host, current_commit="").validate()
    for bad in (replace(host, project="-bad"), replace(host, compose_files=()),
                replace(host, source_root=host.source_root.relative_to(host.source_root.anchor)),
                replace(host, state_dir=host.source_root / "nested"),
                replace(host, state_dir=host.source_root.parent),
                replace(host, data_dir=host.source_root / "src"),
                replace(host, health_timeout=0), replace(host, drain_timeout=1),
                replace(host, env_file=host.source_root / ".." / "other")):
        with pytest.raises(ValueError):
            bad.validate()


def test_manifest_copy_replace_and_restore(host, new_source, tmp_path):
    # 一个真实 SQLite 库验证停机备份包含有效数据库且回滚恢复数据/密钥。
    with closing(sqlite3.connect(host.data_dir / "main.db")) as conn, conn:
        conn.execute("create table accounts (name text)")
        conn.execute("insert into accounts values ('kept')")
    (host.source_root / "web").mkdir()
    (host.source_root / "web" / "node_modules").mkdir()
    (host.source_root / "web" / "node_modules" / "ignored").write_text("skip")
    (host.data_dir / "dist").mkdir()
    (host.data_dir / "dist" / "kept").write_text("user data")
    assert all("node_modules" not in key for key in code_manifest(host.source_root))
    deployment = Deployment(host)
    backup = tmp_path / "backup"
    deployment.backup(backup)
    assert (backup / "code" / "src" / "app.py").read_text() == "old app"
    deployment.replace_code(new_source)
    assert not (host.source_root / "src").exists()
    assert (host.source_root / "README.md").read_text() == "new code"
    assert host.env_file.read_text() == "APP_SECRET=original"
    (host.data_dir / "extra.txt").write_text("new data")
    (host.secrets_dir / "users.txt").write_text("new keys")
    deployment.restore(backup)
    assert (host.source_root / "README.md").read_text() == "old code"
    assert (host.data_dir / "extra.txt").read_text() == "persistent"
    assert (host.data_dir / "dist" / "kept").read_text() == "user data"
    assert (host.secrets_dir / "users.txt").read_text() == "private"
    assert host.compose_files[0].read_text() == "original deployment"
    shutil.rmtree(host.data_dir)
    shutil.rmtree(host.secrets_dir)
    deployment.restore(backup)
    (backup / "data" / "extra.txt").write_text("tampered")
    with pytest.raises(ValueError, match="文件校验"):
        deployment.restore(backup)
    # 有效备份之外的数据绝不因恢复请求而删除。
    assert (host.data_dir / "extra.txt").read_text() == "persistent"
    copy_path(tmp_path / "absent", tmp_path / "absent-copy")


def test_backup_sqlite_integrity_branch(host, tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    Deployment(host).backup(backup)
    (backup / "data" / "bad.db").write_bytes(b"SQLite format 3\x00")
    expected = manifest(backup)
    expected.pop("manifest.json")
    (backup / "manifest.json").write_text(json.dumps(expected))
    conn = SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchone=lambda: ("bad",)),
                           close=lambda: None)
    monkeypatch.setattr("src.updater.deployment.sqlite3.connect", lambda *_a, **_k: conn)
    with pytest.raises(ValueError, match="数据库完整性"):
        verify_backup(backup)


def test_missing_and_corrupt_database_backup(host, tmp_path):
    backup = tmp_path / "backup"
    Deployment(host).backup(backup)
    primary = backup / "data" / "coding2api.sqlite3"
    primary.unlink()
    def resign():
        expected = manifest(backup)
        expected.pop("manifest.json")
        (backup / "manifest.json").write_text(json.dumps(expected))
    resign()
    with pytest.raises(ValueError, match="缺少主数据库"):
        verify_backup(backup)
    primary.write_bytes(b"not a sqlite database" * 100)
    resign()
    with pytest.raises(ValueError, match="无法读取"):
        verify_backup(backup)


def test_wal_snapshot_is_standalone_and_repeatably_verified(host, tmp_path):
    primary = host.data_dir / "coding2api.sqlite3"
    with closing(sqlite3.connect(primary)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("insert into accounts values ('committed in wal')")
        conn.commit()
        assert primary.with_name(primary.name + "-wal").exists()
        backup = tmp_path / "wal-backup"
        Deployment(host).backup(backup)
        verify_backup(backup)
        verify_backup(backup)
        copied = backup / "data" / primary.name
        assert stat.S_IMODE(copied.stat().st_mode) == stat.S_IMODE(primary.stat().st_mode)
        assert not copied.with_name(primary.name + "-wal").exists()
        assert not copied.with_name(primary.name + "-shm").exists()
        with closing(sqlite3.connect(copied)) as snapshot:
            assert snapshot.execute("select name from accounts").fetchone() == ("committed in wal",)


def test_symlink_rejection_without_windows_privilege(host, tmp_path, monkeypatch):
    path = host.source_root / "README.md"
    original = type(path).is_symlink
    monkeypatch.setattr(type(path), "is_symlink", lambda item: item == path or original(item))
    for work in (lambda: code_manifest(host.source_root),
                 lambda: manifest(host.source_root), lambda: copy_path(path, tmp_path / "out"),
                 lambda: Deployment(host).replace_code(tmp_path)):
        with pytest.raises(ValueError, match="链接"):
            work()


def test_command_runner_uses_argv_and_never_logs_output(monkeypatch, tmp_path):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=" success \n")
    monkeypatch.setattr("src.updater.deployment.subprocess.run", run)
    assert run_command(["git", "status"], cwd=tmp_path, timeout=5) == "success"
    assert "shell" not in calls[0][1] and calls[0][1]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    monkeypatch.setattr("src.updater.deployment.subprocess.run", lambda *_a, **_k:
                        SimpleNamespace(returncode=2, stdout="APP_SECRET=sensitive"))
    with pytest.raises(RuntimeError, match="退出码 2") as caught:
        run_command(["git", "status"])
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize("platform", ["posix", "nt"])
def test_owner_restored_all_platform_branches(host, new_source, monkeypatch, platform):
    calls = []
    monkeypatch.setattr("src.updater.deployment.os", SimpleNamespace(name=platform,
                        chown=lambda *_a, **_k: calls.append((_a, _k))))
    preserve_owner(host.source_root, new_source)
    preserve_owner(host.source_root / "README.md", new_source / "README.md")
    assert len(calls) == (3 if platform == "posix" else 0)


def test_root_umask_source_remains_readable_by_nonroot_image(new_source, monkeypatch):
    code = new_source / "src"
    code.mkdir()
    executable = code / "run"
    executable.write_text("executable")
    ordinary = code / "app.py"
    ordinary.write_text("python")
    original_stat = type(code).stat
    monkeypatch.setattr(type(code), "stat", lambda path, *_a, **_k:
                        SimpleNamespace(st_mode=stat.S_IFREG | 0o750) if path == executable
                        else original_stat(path, *_a, **_k))
    calls = []
    monkeypatch.setattr("src.updater.deployment.os", SimpleNamespace(
                        chmod=lambda path, mode: calls.append((path, mode))))
    readable_code(new_source)
    assert (code, 0o755) in calls and (executable, 0o755) in calls and (ordinary, 0o644) in calls
    generated = code / "node_modules"
    generated.mkdir()
    monkeypatch.setattr(type(code), "is_symlink", lambda path: path == generated)
    with pytest.raises(ValueError, match="符号链接"):
        readable_code(new_source)


def test_compose_current_image_and_build_fixed_commit(host, tmp_path):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if "rev-parse" in argv:
            return SHA
        if argv[:3] == ["docker", "buildx", "inspect"]:
            return "Name: apis-update-2apis\nDriver: docker-container\nNodes:\n"
        if "{{json .HostConfig}}" in argv:
            return json.dumps({"Memory": 268435456, "MemorySwap": 536870912,
                               "CpuQuota": 25000, "CpuPeriod": 100000})
        if "inspect" in argv:
            return IMAGE
        if "ps" in argv:
            return "container"
        if "clone" in argv:
            stage = tmp_path / "stage"
            stage.mkdir()
            (stage / "README.md").write_text("new")
        return ""
    deployment = Deployment(host, run=run)
    assert deployment.current_image() == IMAGE
    assert deployment.build("a/b", SHA, tmp_path / "stage") == f"2apis-update:{SHA}"
    assert calls[-2][0] == ["docker", "buildx", "build", "--builder", "apis-update-2apis",
                            "--load", "--tag", f"2apis-update:{SHA}", str(tmp_path / "stage")]
    assert calls[-1][0] == ["docker", "buildx", "stop", "apis-update-2apis"]
    deployment.stop()
    assert calls[-1][0][-4:] == ["stop", "--timeout", "180", "coding2api"]
    host.env_file.unlink()
    with pytest.raises(ValueError, match="原数据"):
        deployment.current_image()


def test_missing_image_and_mismatched_git(host, tmp_path):
    with pytest.raises(ValueError, match="原服务未运行"):
        Deployment(host, run=lambda *_a, **_k: "").current_image()
    with pytest.raises(ValueError, match="镜像 ID"):
        Deployment(host, run=lambda *_a, **_k: "container").current_image()
    with pytest.raises(ValueError, match="不一致"):
        Deployment(host, run=lambda *_a, **_k: "wrong").build("a/b", SHA, tmp_path / "stage")


@pytest.mark.parametrize("health", ["healthy", "unhealthy", "starting", "no-container"])
def test_start_health_and_override(host, health):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if "ps" in argv:
            return "" if health == "no-container" else "container"
        return health
    sleeps = []
    deployment = Deployment(host, run=run, sleep=sleeps.append)
    if health == "healthy":
        deployment.start(IMAGE)
    else:
        with pytest.raises(RuntimeError, match="健康检查"):
            deployment.start(IMAGE)
    assert json.loads(deployment.override.read_text())["services"]["coding2api"]["image"] == IMAGE
    assert all("-f" in argv for argv in calls if "compose" in argv)
    assert not sleeps if health in {"healthy", "unhealthy"} else len(sleeps) == 2
