"""固定 argv 的 Docker 部署与停机后备份事务。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .source import validate_commit, validate_source

# 只切换代码根；密钥、账号、数据库、Compose、Tunnel 始终由主机配置保留。
CODE_ROOTS = ("src", "scripts", "web", "tests", "docs", ".github", "Dockerfile",
              "pyproject.toml", "uv.lock", "README.md", "PROPOSAL.md", "AGENTS.md",
              ".dockerignore", ".gitignore")
IGNORED = {".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", ".ruff_cache"}
BUILDKIT_IMAGE = (
    "moby/buildkit:buildx-stable-1@sha256:"
    "baf6138eb8c37644800c7c913fba051e840ebb9ba13eaf92d92ef9a1712d0b4c"
)
BUILDER_LIMITS = {
    "Memory": 256 * 1024 * 1024, "MemorySwap": 512 * 1024 * 1024,
    "CpuQuota": 25000, "CpuPeriod": 100000,
}
logger = logging.getLogger(__name__)
LOGIN_COLUMNS = ("data_enc", "token_issued_at", "token_expires_at", "login_secret")
MAX_COMMAND_STDOUT_BYTES = 1024 * 1024


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
    current_version: str = "0.1.2"
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
    # 构建日志可能很大：写入自动清理的私有临时文件，禁止PIPE无界积累内存。
    # stderr 不用于任何调用结果，直接丢弃；超时仍由 subprocess.run 杀死并回收进程。
    with tempfile.TemporaryFile(mode="w+b") as output:
        result = subprocess.run(argv, cwd=cwd, env=env, timeout=timeout, stdout=output,
                                stderr=subprocess.DEVNULL, check=False)  # nosec B603
        if result.returncode:
            # 不把命令输出发给管理台，Compose/构建输出可能含环境变量和私密配置。
            raise RuntimeError(f"{argv[0]} 命令失败（退出码 {result.returncode}）")
        output.seek(0)
        captured = output.read(MAX_COMMAND_STDOUT_BYTES + 1)
        if len(captured) > MAX_COMMAND_STDOUT_BYTES:
            raise RuntimeError(f"{argv[0]} 命令输出超过安全大小限制")
        return captured.decode("utf-8", errors="replace").strip()


class Deployment:
    def __init__(self, config: HostConfig, run=run_command, sleep=time.sleep,
                 progress: Callable[[int, str], None] | None = None):
        self.config = config
        self.run = run
        self.sleep = sleep
        self.progress = progress or (lambda _percent, _message: None)
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
        self.progress(10, "正在连接 GitHub 并获取源码")
        self.run([*git, "clone", "--no-checkout", "--filter=blob:none", "--",
                  f"https://github.com/{source}.git", str(stage)])
        self.progress(14, "正在获取本次更新的固定提交")
        self.run([*git, "fetch", "--depth=1", "origin", commit], cwd=stage)
        actual = self.run([*git, "rev-parse", "FETCH_HEAD"], cwd=stage)
        if actual != commit:
            raise ValueError("Git 获取的提交与固定更新目标不一致")
        self.progress(18, "提交校验通过，正在准备构建源码")
        self.run([*git, "checkout", "--detach", "--force", commit], cwd=stage)
        # 不跟随仓库里的符号链接；Docker build 的上下文来自固定提交。
        code_manifest(stage)
        readable_code(stage)
        image = f"2apis-update:{commit}"
        builder = f"apis-update-{self.config.project}"
        failed = True
        try:
            self.progress(22, "正在启动并校验受限构建器")
            self._prepare_builder(builder)
            self.progress(25, "正在构建前端及后端镜像；此步骤可能需要数分钟，旧服务继续运行")
            self.run(["docker", "buildx", "build", "--builder", builder, "--load",
                      "--tag", image, str(stage)], timeout=3600)
            self.progress(40, "镜像构建完成，正在收尾并释放构建器资源")
            failed = False
        finally:
            self._stop_builder(builder, failed=failed)
        return image

    def _stop_builder(self, builder: str, *, failed: bool) -> None:
        try:
            # stop 保留构建缓存；不能让独立BuildKit在1GB主机上常驻吃内存。
            self.run(["docker", "buildx", "stop", builder], timeout=60)
        except Exception:  # noqa: BLE001 - 清理失败不能掩盖已有构建失败
            if failed:
                return
            raise RuntimeError("构建完成但无法停止受限构建器，请主机管理员检查") from None

    def stop_interrupted_build(self) -> None:
        """助手重启后回收本项目构建容器，不依赖进程私有的 buildx 注册目录。"""
        container = f"buildx_buildkit_apis-update-{self.config.project}0"
        existing = self.run(["docker", "ps", "--all", "--filter",
                             f"name=^{re.escape(container)}$", "--format", "{{.Names}}"],
                            timeout=60)
        if not existing:
            return
        if existing != container:
            raise RuntimeError("中断构建器名称不匹配，停止回收")
        # docker stop 对已经停止的容器也幂等；保留镜像与缓存，不做 prune/rm。
        self.run(["docker", "stop", "--time", "10", container], timeout=60)

    def _prepare_builder(self, builder: str) -> None:
        try:
            description = self.run(["docker", "buildx", "inspect", builder], timeout=60)
        except RuntimeError:
            # 缺少buildx/不可创建也会明确失败；绝不回退不受资源限制的docker build。
            self.run(["docker", "buildx", "create", "--name", builder,
                      "--driver", "docker-container", "--driver-opt", f"image={BUILDKIT_IMAGE}",
                      "--driver-opt", "memory=256m", "--driver-opt", "memory-swap=512m",
                      "--driver-opt", "cpu-quota=25000", "--driver-opt", "cpu-period=100000"],
                     timeout=60)
        else:
            # 已有builder须在启动前校验，避免同名无约束容器先占用主机资源。
            self._verify_builder(builder, description)
        description = self.run(["docker", "buildx", "inspect", builder, "--bootstrap"], timeout=300)
        self._verify_builder(builder, description)

    def _verify_builder(self, builder: str, description: str) -> None:
        # buildx 0.13.1支持inspect的Driver文本；不依赖新版inspect --format。
        if not re.search(r"^Driver:\s+docker-container\s*$", description, re.MULTILINE):
            raise ValueError("更新构建器必须使用独立docker-container驱动")
        raw = self.run(["docker", "inspect", "--format", "{{json .HostConfig}}",
                        f"buildx_buildkit_{builder}0"], timeout=60)
        try:
            limits = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("无法验证更新构建器的主机资源限制") from error
        if not isinstance(limits, dict) or any(
                limits.get(key) != value for key, value in BUILDER_LIMITS.items()):
            raise ValueError("更新构建器必须限制内存256MiB、交换总量512MiB与CPU25%")

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
        rotated = self._rotated_logins(backup / "data" / "coding2api.sqlite3")
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
        self._preserve_rotated_logins(rotated)

    def _rotated_logins(self, backup_database: Path) -> list[tuple]:
        """旧/新程序均已停机后，只取相同账号中能证明较新的登录字段。"""
        current_database = self.config.data_dir / "coding2api.sqlite3"
        if not current_database.is_file():
            logger.warning("回滚时当前凭证库缺失，恢复原备份；无法保留新续期票据")
            return []
        current_secret = self._app_secret(self.config.env_file)
        old_secret = self._app_secret(backup_database.parent.parent / "env")
        if not current_secret or current_secret != old_secret:
            logger.warning("回滚密钥缺失或不一致，恢复原备份；无法保留新续期票据")
            return []
        try:
            with (closing(sqlite3.connect(
                      f"{current_database.as_uri()}?mode=ro", uri=True)) as current,
                  closing(sqlite3.connect(
                      f"{backup_database.as_uri()}?mode=ro&immutable=1", uri=True)) as old):
                required = {"id", "provider", *LOGIN_COLUMNS}
                for conn in (current, old):
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(credentials)")}
                    if not required <= columns:
                        logger.warning("回滚凭证字段不兼容，恢复原备份；无法保留新续期票据")
                        return []
                if current.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise sqlite3.DatabaseError("current database failed integrity check")
                rotated = []
                for row in current.execute(
                        "SELECT id,provider,data_enc,token_issued_at,token_expires_at,login_secret "
                        "FROM credentials"):
                    before = old.execute(
                        "SELECT data_enc,token_issued_at,token_expires_at FROM credentials "
                        "WHERE id=? AND provider=?", row[:2]).fetchone()
                    if before is None or not row[2] or row[2] == before[0]:
                        continue
                    times = (row[3], row[4], before[1], before[2])
                    # 0/NULL表示未知，无法据此证明比备份中的票据更新。
                    if not all(isinstance(value, int) and value > 0 for value in times):
                        continue
                    issued, expires, old_issued, old_expires = times
                    if (expires > issued > 0 and issued >= old_issued and expires >= old_expires
                            and (issued > old_issued or expires > old_expires)):
                        rotated.append((*row, before[0]))
                return rotated
        except sqlite3.Error:
            logger.warning("回滚时当前凭证库无法安全读取，恢复原备份；可能需要重新登录")
            return []

    @staticmethod
    def _app_secret(path: Path) -> str | None:
        """只比较明确的单条字面量APP_SECRET，歧义/读取失败时不继承新密文。"""
        try:
            values = []
            for line in path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if not separator or key.strip().removeprefix("export ").strip() != "APP_SECRET":
                    continue
                value = value.strip()
                if value.startswith(("'", '"')):
                    if len(value) < 2 or value[-1] != value[0] or "\\" in value:
                        return None
                    value = value[1:-1]
                else:
                    value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
                # Compose/dotenv插值或转义可能使同一文本在两次启动解出不同密钥。
                if "$" in value or "\\" in value:
                    return None
                values.append(value)
        except (OSError, UnicodeError):
            return None
        return values[0] if len(values) == 1 and values[0] else None

    def _preserve_rotated_logins(self, rotated: list[tuple]) -> None:
        if not rotated:
            return
        try:
            primary = self.config.data_dir / "coding2api.sqlite3"
            with closing(sqlite3.connect(primary)) as conn, conn:
                for account_id, provider, data, issued, expires, secret, old_data in rotated:
                    conn.execute(
                        "UPDATE credentials SET data_enc=?,token_issued_at=?,token_expires_at=?,"
                        "login_secret=? WHERE id=? AND provider=? AND data_enc=?",
                        (data, issued, expires, secret, account_id, provider, old_data))
                preserved = conn.total_changes
        except sqlite3.Error:
            # 窄UPDATE是一个事务；失败时保留完整原备份数据库，不改其schema或密钥。
            logger.warning("回滚保留新续期票据失败，已保留原备份数据库；可能需要重新登录")
            return
        logger.warning("回滚已保留 %s 个账号的新续期票据；其余数据和schema仍来自原备份", preserved)
