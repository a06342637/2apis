"""固定 argv 的 Docker 部署与停机后备份事务。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .source import validate_commit, validate_source

# 只切换代码根；密钥、账号、数据库、Compose、Tunnel 始终由主机配置保留。
CODE_ROOTS = ("src", "scripts", "web", "tests", "docs", ".github", "Dockerfile",
              "pyproject.toml", "uv.lock", "README.md", "PROPOSAL.md", "AGENTS.md",
              ".dockerignore", ".gitignore")
IGNORED = {".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", ".ruff_cache"}


@dataclass(frozen=True)
class HostConfig:
    source_root: Path
    state_dir: Path
    data_dir: Path
    secrets_dir: Path
    compose_files: tuple[Path, ...]
    env_file: Path
    socket_path: Path
    secret_file: Path
    project: str = "2apis"
    service: str = "coding2api"
    source: str = "a06342637/2apis"
    current_version: str = "0.1.0"
    current_commit: str = ""
    health_timeout: int = 180
    drain_timeout: int = 180

    @classmethod
    def load(cls, path: Path) -> HostConfig:
        values = json.loads(path.read_text())
        for name in ("source_root", "state_dir", "data_dir", "secrets_dir", "env_file",
                     "socket_path", "secret_file"):
            values[name] = Path(values[name])
        values["compose_files"] = tuple(Path(item) for item in values["compose_files"])
        result = cls(**values)
        result.validate()
        return result

    def validate(self) -> None:
        validate_source(self.source)
        if self.current_commit:
            validate_commit(self.current_commit)
        for name in (self.project, self.service):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
                raise ValueError("Compose project/service 必须是普通名称")
        paths = (self.source_root, self.state_dir, self.data_dir, self.secrets_dir,
                 self.env_file, self.socket_path, self.secret_file, *self.compose_files)
        if not self.compose_files or any(not path.is_absolute() for path in paths):
            raise ValueError("更新助手的所有主机路径必须是绝对路径，并指定 Compose 文件")
        if any(path.resolve() != path for path in paths):
            raise ValueError("更新主机路径必须规范化，不能使用链接或 ..")
        for protected in (self.source_root, self.data_dir, self.secrets_dir):
            if self.state_dir.is_relative_to(protected) or protected.is_relative_to(self.state_dir):
                raise ValueError("更新状态/备份目录必须与源码、数据库、密钥目录分离")
        for protected in (self.data_dir, self.secrets_dir):
            for name in CODE_ROOTS:
                managed = self.source_root / name
                if protected.is_relative_to(managed) or managed.is_relative_to(protected):
                    raise ValueError("数据与密钥路径不能位于受更新管理的代码根中")
        if self.health_timeout < 1 or self.drain_timeout < 95:
            raise ValueError("健康等待必须为正数，排空窗口不得小于 95 秒")


def files(directory: Path, *, ignore_generated: bool = False):
    for path in sorted(directory.rglob("*")):
        if ignore_generated and any(part in IGNORED for part in path.relative_to(directory).parts):
            continue
        if path.is_symlink():
            raise ValueError("更新文件不允许符号链接")
        if path.is_file():
            yield path


def manifest(directory: Path, *, ignore_generated: bool = False) -> dict:
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files(directory, ignore_generated=ignore_generated)}


def preserve_owner(source: Path, target: Path) -> None:
    # root helper 的 copytree 会把原 appuser 数据变成 root；恢复时必须保留属主。
    # Windows 只运行 mock 验证，真实主机 helper 入口限定 Linux root。
    if os.name == "posix":
        pairs = [(source, target)]
        if target.is_dir():
            pairs.extend((source / path.relative_to(target), path) for path in target.rglob("*"))
        for original, copied in pairs:
            metadata = original.stat()
            os.chown(copied, metadata.st_uid, metadata.st_gid, follow_symlinks=False)


def copy_path(source: Path, target: Path, *, ignore_generated: bool = False) -> None:
    if source.is_symlink():
        raise ValueError("更新文件不允许符号链接")
    if source.is_dir():
        # 先校验所有链接，避免 shutil.copytree 跟随链接读取目录外的密钥。
        list(files(source, ignore_generated=ignore_generated))
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED)
                        if ignore_generated else None)
        preserve_owner(source, target)
    elif source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        preserve_owner(source, target)


def code_manifest(source: Path) -> dict:
    result = {}
    for name in CODE_ROOTS:
        path = source / name
        if path.is_symlink():
            raise ValueError("更新文件不允许符号链接")
        if path.is_dir():
            result.update({f"{name}/{key}": value for key, value in
                           manifest(path, ignore_generated=True).items()})
        elif path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def readable_code(source: Path) -> None:
    # helper 的 0027 umask 可能让 Git 检出文件为 0640；Docker COPY 默认 root:root，
    # 必须让容器内 appuser 可读，执行位仍按 Git 原文件保留。
    for name in CODE_ROOTS:
        root = source / name
        paths = [root, *root.rglob("*")] if root.is_dir() else [root]
        for path in paths:
            if path.is_symlink():
                raise ValueError("构建源码不允许符号链接")
            if not path.exists():
                continue
            mode = 0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644
            os.chmod(path, mode)


def verify_backup(directory: Path) -> None:
    expected = json.loads((directory / "manifest.json").read_text())
    actual = manifest(directory)
    actual.pop("manifest.json")
    if actual != expected:
        raise ValueError("更新备份文件校验失败")
    if not (directory / "data" / "coding2api.sqlite3").is_file():
        raise ValueError("更新备份缺少主数据库")
    for path in files(directory / "data"):
        # 数据文件包括主库和独立备份；用文件头而非扩展名识别 SQLite。
        with path.open("rb") as handle:
            sqlite = handle.read(16) == b"SQLite format 3\x00" or path.suffix in {".sqlite3", ".db"}
        if sqlite:
            try:
                uri = f"{path.resolve().as_uri()}?mode=ro"
                with closing(sqlite3.connect(uri, uri=True)) as conn:
                    if conn.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise ValueError("更新备份数据库完整性校验失败")
            except sqlite3.DatabaseError as error:
                raise ValueError("更新备份数据库无法读取") from error


def run_command(argv: list[str], *, cwd: Path | None = None, timeout: int = 1800) -> str:
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", DOCKER_BUILDKIT="1")
    result = subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, capture_output=True,
                            text=True, check=False)  # nosec B603 - 固定 argv，不使用 shell
    if result.returncode:
        # 不把命令输出发给管理台，Compose/构建输出可能含环境变量和私密配置。
        raise RuntimeError(f"{argv[0]} 命令失败（退出码 {result.returncode}）")
    return result.stdout.strip()


class Deployment:
    def __init__(self, config: HostConfig, run=run_command, sleep=time.sleep):
        self.config = config
        self.run = run
        self.sleep = sleep
        self.override = config.state_dir / "active-image.json"

    def compose(self, *args: str) -> str:
        cfg = self.config
        argv = ["docker", "compose", "--project-name", cfg.project, "--project-directory",
                str(cfg.source_root), "--env-file", str(cfg.env_file)]
        for path in (*cfg.compose_files, self.override):
            if path != self.override or path.exists():
                argv.extend(["-f", str(path)])
        return self.run([*argv, *args], timeout=cfg.health_timeout + cfg.drain_timeout + 120)

    def current_image(self) -> str:
        cfg = self.config
        if not cfg.data_dir.is_dir() or not cfg.secrets_dir.is_dir() or not cfg.env_file.is_file():
            raise ValueError("原数据、密钥目录与环境配置必须存在")
        container = self.compose("ps", "-q", self.config.service)
        if not container:
            raise ValueError("原服务未运行，无法建立更新回滚点")
        image = self.run(["docker", "inspect", "--format", "{{.Image}}", container])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("无法读取原服务镜像 ID")
        return image

    def build(self, source: str, commit: str, stage: Path) -> str:
        source, commit = validate_source(source), validate_commit(commit)
        git = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "protocol.file.allow=never"]
        self.run([*git, "clone", "--no-checkout", "--filter=blob:none", "--",
                  f"https://github.com/{source}.git", str(stage)])
        self.run([*git, "fetch", "--depth=1", "origin", commit], cwd=stage)
        actual = self.run([*git, "rev-parse", "FETCH_HEAD"], cwd=stage)
        if actual != commit:
            raise ValueError("Git 获取的提交与固定更新目标不一致")
        self.run([*git, "checkout", "--detach", "--force", commit], cwd=stage)
        # 不跟随仓库里的符号链接；Docker build 的上下文来自固定提交。
        code_manifest(stage)
        readable_code(stage)
        image = f"2apis-update:{commit}"
        self.run(["docker", "build", "--tag", image, str(stage)], timeout=3600)
        return image

    def stop(self) -> None:
        self.compose("stop", "--timeout", str(self.config.drain_timeout), self.config.service)

    def start(self, image: str) -> None:
        temp = self.override.with_suffix(".tmp")
        temp.write_text(json.dumps({"services": {self.config.service: {"image": image}}}))
        os.replace(temp, self.override)
        self.compose("up", "-d", "--no-build", "--no-deps", "--force-recreate", self.config.service)
        for _ in range(self.config.health_timeout):
            container = self.compose("ps", "-q", self.config.service)
            if container:
                health = self.run(["docker", "inspect", "--format",
                                   "{{if .State.Health}}{{.State.Health.Status}}{{end}}",
                                   container])
                if health == "healthy":
                    return
                if health == "unhealthy":
                    break
            self.sleep(1)
        raise RuntimeError("更新后服务未通过健康检查")

    def backup(self, target: Path) -> None:
        target.mkdir(mode=0o700)
        cfg = self.config
        for name in CODE_ROOTS:
            copy_path(cfg.source_root / name, target / "code" / name, ignore_generated=True)
        for name, path in (("data", cfg.data_dir), ("secrets", cfg.secrets_dir),
                           ("env", cfg.env_file)):
            copy_path(path, target / name)
        for index, path in enumerate(cfg.compose_files):
            copy_path(path, target / "config" / str(index))
        # 即使停机超时 SIGKILL 留下 WAL，也生成包含全部已提交事务的独立主库。
        # 只签名封闭快照；校验时无需创建/修改备份中的 WAL/SHM 文件。
        primary = cfg.data_dir / "coding2api.sqlite3"
        snapshot = target / "data" / ".snapshot.sqlite3"
        uri = f"{primary.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("VACUUM INTO ?", (str(snapshot),))
        copied = target / "data" / primary.name
        os.replace(snapshot, copied)
        shutil.copystat(primary, copied)
        preserve_owner(primary, copied)
        for suffix in ("-wal", "-shm"):
            copied.with_name(primary.name + suffix).unlink(missing_ok=True)
        (target / "manifest.json").write_text(json.dumps(manifest(target)))
        verify_backup(target)

    def replace_code(self, source: Path) -> None:
        for name in CODE_ROOTS:
            target = self.config.source_root / name
            if target.is_symlink():
                raise ValueError("原源码存在符号链接，停止更新")
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            copy_path(source / name, target, ignore_generated=True)

    def restore(self, backup: Path) -> None:
        verify_backup(backup)
        self.replace_code(backup / "code")
        cfg = self.config
        for name, target in (("data", cfg.data_dir), ("secrets", cfg.secrets_dir)):
            # 只清理通过验证的 root-owned 配置目录，备份必须完整校验后才允许恢复。
            if target.exists():
                shutil.rmtree(target)
            copy_path(backup / name, target)
        copy_path(backup / "env", cfg.env_file)
        for index, target in enumerate(cfg.compose_files):
            copy_path(backup / "config" / str(index), target)
