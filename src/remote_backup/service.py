"""Remote backup jobs, scheduled catch-up, and instance-scoped file management."""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
import uuid
from contextlib import suppress
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from ..db.backup import backup_database, pending_restore_path
from ..db.crypto import CredentialDecryptError
from .config import BackupError, Config, Store, merge_config, public_config, write_private
from .transports import MAX_BYTES, open_target

logger = logging.getLogger(__name__)
TARGETS = ("oss", "sftp")
LABELS = {"oss": "阿里云 OSS", "sftp": "SSH/SFTP"}
NAME = re.compile(r"coding2api-\d{8}T\d{6}Z-[a-f0-9]{12}\.sqlite3")
EPOCH = date(1970, 1, 1)


def slots(config: Config, now: int) -> tuple[int, int]:
    zone = ZoneInfo(config.timezone)
    today = datetime.fromtimestamp(now, zone).date()
    hour, minute = map(int, config.time.split(":"))
    day = today - timedelta(days=(today - EPOCH).days % config.days)

    def scheduled(d):
        return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=zone).timestamp())

    if scheduled(day) > now:
        day -= timedelta(days=config.days)
    return scheduled(day), scheduled(day + timedelta(days=config.days))


class RemoteBackups:
    def __init__(self, db_path: str, store: Store, stage_restore, *, clock=time.time):
        self.db_path = db_path
        self.store = store
        self.stage_restore = stage_restore
        self.clock = clock
        self.job: asyncio.Task | None = None
        self.lock = Lock()

    def config(self) -> Config:
        try:
            return self.store.config()
        except (CredentialDecryptError, OSError, ValueError):
            raise BackupError("无法读取远端备份配置，请检查原部署密钥和文件权限") from None

    def status(self) -> dict:
        config = self.config()
        state = self.store.state()
        return {"config": public_config(config), "last_run": state.get("last_run"),
                "next_run": slots(config, int(self.clock()))[1] if config.enabled else None,
                "running": self.job is not None and not self.job.done()}

    def save(self, payload: dict) -> None:
        with self.lock:
            self.ensure_idle()
            old = self.config()
            config = merge_config(old, payload)
            self.store.save(config)
            if (old.enabled, old.days, old.time, old.timezone) != (
                    config.enabled, config.days, config.time, config.timezone):
                state = self.store.state()
                state["last_slot"] = slots(config, int(self.clock()))[0]
                self.store.save_state(state)

    def namespace(self) -> str:
        with self.lock:
            path = self.store.directory / "instance"
            if not path.is_file():
                write_private(path, uuid.uuid4().hex.encode())
            name = path.read_text().strip()
            if not re.fullmatch(r"[a-f0-9]{32}", name):
                raise BackupError("备份实例标识无效")
            return "coding2api-" + name

    def ensure_idle(self) -> None:
        if self.job is not None and not self.job.done():
            raise BackupError("有远端备份任务正在执行，请稍后重试")

    def target(self, name: str, config: Config):
        if name not in TARGETS:
            raise BackupError("未知的远端备份目标")
        target = getattr(config, name)
        if not target.enabled:
            raise BackupError("请先保存并启用此备份目标")
        return target

    @staticmethod
    def entries(remote):
        rows = ({"name": name, "size": int(size or 0), "created_at": int(stamp or 0)}
                for name, size, stamp in remote.entries() if NAME.fullmatch(name))
        return sorted(rows, key=lambda row: (row["created_at"], row["name"]), reverse=True)

    def download(self, target: str, name: str, temporary: Path) -> None:
        config = self.target(target, self.config())
        if not NAME.fullmatch(name):
            raise BackupError("无效的远端备份文件名")
        try:
            with open_target(config, self.namespace()) as remote:
                remote.get(name, temporary)
        except Exception as error:  # noqa: BLE001 - 不返回 SDK 原始异常
            logger.warning("远端备份 %s：下载失败（%s）", LABELS[target], type(error).__name__)
            raise BackupError("远端备份下载失败，请检查连接和权限") from None
        logger.info("远端备份 %s：已下载 %s", LABELS[target], name)

    def operate(self, target: str, action: str, name: str = "") -> dict:
        config = self.target(target, self.config())
        if action in {"restore", "delete"} and not NAME.fullmatch(name):
            raise BackupError("无效的远端备份文件名")
        try:
            with open_target(config, self.namespace()) as remote:
                if action == "test":
                    next(iter(remote.entries()), None)
                    logger.info("远端备份 %s：连接测试成功", LABELS[target])
                    return {"ok": True}
                if action == "list":
                    return {"backups": self.entries(remote)}
                if action == "delete":
                    remote.delete(name)
                    logger.info("远端备份 %s：已删除 %s", LABELS[target], name)
                    return {"ok": True}
                pending = pending_restore_path(self.db_path)
                if pending.is_file():
                    raise BackupError("已有待恢复备份，请先重启应用该备份")
                with tempfile.TemporaryDirectory(dir=self.store.directory) as directory:
                    temp = Path(directory) / name
                    size = remote.get(name, temp)
                    with self.lock:
                        if pending.is_file():
                            raise BackupError("已有待恢复备份，请先重启应用该备份")
                        pending.parent.mkdir(parents=True, exist_ok=True)
                        meta = self.stage_restore(pending, temp, filename=name, size=size)
                logger.info("远端备份 %s：已暂存恢复 %s，重启后生效", LABELS[target], name)
                return {"staged": True, "restart_required": True, **meta}
        except BackupError as error:
            logger.warning("远端备份 %s：%s 失败：%s", LABELS[target], action, error)
            raise
        except Exception as error:  # noqa: BLE001 - 不公开传输异常中的地址和敏感字段
            logger.warning("远端备份 %s：%s 失败（%s）", LABELS[target], action,
                           type(error).__name__)
            raise BackupError("远端操作失败，请检查连接、访问权限及备份文件") from None

    def _copy_to(self, target, config, snapshot, filename) -> dict:
        label = LABELS[target]
        try:
            logger.info("远端备份 %s：开始上传 %s", label, filename)
            with open_target(config, self.namespace()) as remote:
                remote.put(filename, snapshot)
                removed = 0
                try:
                    others = [row for row in self.entries(remote) if row["name"] != filename]
                    for row in others[config.keep - 1:]:
                        remote.delete(row["name"])
                        removed += 1
                except Exception as error:  # noqa: BLE001 - 保留已成功上传文件，单独显示清理失败
                    logger.warning("远端备份 %s：上传成功，旧备份清理失败（%s）",
                                   label, type(error).__name__)
                    return {"target": target, "status": "warning", "removed": removed,
                            "message": "上传成功，旧备份清理失败"}
            logger.info("远端备份 %s：已上传 %s，%d 字节，清理 %d 份",
                        label, filename, snapshot.stat().st_size, removed)
            return {"target": target, "status": "success", "removed": removed,
                    "message": "上传成功"}
        except Exception as error:  # noqa: BLE001 - 单目标失败不会中止其他目标
            logger.warning("远端备份 %s：上传失败（%s）", label, type(error).__name__)
            return {"target": target, "status": "failed", "message": "上传失败，请检查连接和权限"}

    def _run(self, config: Config, slot: int | None) -> None:
        started = int(self.clock())
        stamp = datetime.fromtimestamp(started, ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
        filename = f"coding2api-{stamp}-{uuid.uuid4().hex[:12]}.sqlite3"
        report = {"started_at": started, "scheduled": slot is not None, "name": filename}
        logger.info("远端备份 %s：开始创建一致性快照", "定时" if slot is not None else "手动")
        try:
            self.namespace()
            with tempfile.TemporaryDirectory(dir=self.store.directory) as directory:
                snapshot = backup_database(self.db_path, directory=directory, keep=0)
                if snapshot.stat().st_size > MAX_BYTES:
                    raise BackupError("快照超过 512 MB 恢复上限")
                report["targets"] = [self._copy_to(name, getattr(config, name), snapshot, filename)
                                     for name in TARGETS if getattr(config, name).enabled]
        except Exception as error:  # noqa: BLE001 - 把失败转换成可见的任务结果
            logger.error("远端备份：创建快照失败（%s）", type(error).__name__)
            report["error"] = str(error) if isinstance(error, BackupError) else "创建数据库快照失败"
        report["finished_at"] = int(self.clock())
        with self.lock:
            state = self.store.state()
            state["last_run"] = report
            if slot is not None:
                state["last_slot"] = slot
            self.store.save_state(state)

    def start(self, slot: int | None = None) -> None:
        with self.lock:
            self.ensure_idle()
            config = self.config()
            if not any(getattr(config, name).enabled for name in TARGETS):
                raise BackupError("请先保存并启用至少一个远端目标")
            self.job = asyncio.create_task(self._run_async(config, slot))

    async def _run_async(self, config, slot):
        try:
            await asyncio.to_thread(self._run, config, slot)
        except (OSError, ValueError):
            logger.error("远端备份：无法保存本轮状态，请检查数据目录")

    def tick(self) -> None:
        config = self.config()
        if not config.enabled or (self.job is not None and not self.job.done()):
            return
        slot = slots(config, int(self.clock()))[0]
        if self.store.state().get("last_slot", 0) < slot:
            self.start(slot)

    async def schedule(self) -> None:
        while True:
            try:
                self.tick()
            except (OSError, ValueError):
                logger.error("远端备份：无法读取定时任务配置或状态")
            await asyncio.sleep(30)

    async def close(self, scheduler: asyncio.Task) -> None:
        scheduler.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler
        if self.job is not None:
            await self.job
