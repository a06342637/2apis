"""token 预刷新：只处理进入 REFRESH_SKEW_HOURS 窗口的凭证。

终态识别：一次性 refresh_token 被消费后，每轮重试用的都是同一张废票，
表现为「每小时刷一条一模一样的 warning」而凭证其实早就死了。上游明确说
票据作废时（`UpstreamReloginRequired`）改写 disabled_reason 并停手，把处置
交给人；其余失败照旧只记 warning，等下一轮。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from ..db.crypto import CredentialDecryptError
from ..db.repo import CredentialRepository
from ..error_text import error_text
from ..provider.base import UpstreamReloginRequired
from ..request_context import credential_scope
from . import TaskReport
from .pacer import Pacer

logger = logging.getLogger(__name__)

# 落库的「需重新登录」原因：固定文案，绝不写上游原文（见 run_once 注释）。
RELOGIN_REASON = "刷新令牌已失效，需重新登录"


class RefreshTask:
    """token 预刷新：只处理进入 refresh_skew 窗口的凭证。"""

    def __init__(self, credentials: CredentialRepository, providers: dict,
                 *, skew_seconds: int, now: Callable[[], int] | None = None,
                 pacer: Pacer | None = None) -> None:
        self._credentials = credentials
        self._providers = providers
        self.skew_seconds = skew_seconds
        self._now = now or (lambda: int(time.time()))
        self._pacer = pacer
        self._rotations: set[asyncio.Task] = set()
        self._stopping = False

    def resume(self) -> None:
        """仅 runner 明确重新启动时恢复维护，停机闸门不改数据库启停状态。"""
        self._stopping = False

    async def run_once(self, *, credential_id: str | None = None) -> TaskReport:
        report = TaskReport()
        current = self._now()
        for candidate in self._credentials.candidates():
            if self._stopping:
                break
            if credential_id is not None and candidate.credential_id != credential_id:
                continue
            # 软暂停只停 API 调度，预刷新仍须维护登录态；硬禁用票据不再尝试。
            if candidate.disabled:
                report.skipped += 1
                continue
            provider = self._providers.get(candidate.provider)
            # 解不开（APP_SECRET 换过 / 密文损坏）→ 跳过这条凭证：一条坏凭证
            # 不该让整轮预刷新失败（早先直接冒泡 → 整轮任务挂掉）。
            try:
                data = self._credentials.credential_data(candidate.credential_id)
            except CredentialDecryptError:
                logger.error("预刷新跳过 %s：凭证解密失败（APP_SECRET 是否被更换过？）",
                             candidate.credential_id)
                report.skipped += 1
                continue
            if provider is None or data is None:
                report.skipped += 1
                continue
            if not _needs_refresh(provider, data, self.skew_seconds, current):
                report.skipped += 1
                continue
            report.attempted += 1
            # 与 quota_probe/growth 共用同一 Pacer：ExchangeToken 同样是
            # 打上游的写请求，不过节流会绕开全局频率风控对策
            if self._pacer is not None:
                await self._pacer.wait_turn()
            # 编辑锁：读 → await 上游 → 整份覆写必须与账号切换等写者互斥，
            # 否则两边都用自己读到的旧快照写回，后写者覆盖先写者。
            async with self._credentials.edit_lock(candidate.credential_id):
                # 持锁后重读：等锁期间可能已被别的写者改过（如刚切了账号），
                # 用旧 data 刷新会把那次改动覆盖掉。
                try:
                    data = self._credentials.credential_data(candidate.credential_id)
                except CredentialDecryptError:
                    logger.error("预刷新跳过 %s：凭证解密失败（APP_SECRET 是否被更换过？）",
                                 candidate.credential_id)
                    report.skipped += 1
                    continue
                if data is None:
                    report.skipped += 1
                    continue
                # 同一坏刷新票的前一个等待者可能刚标硬禁用；锁前快照不能再消费它。
                # 只查该账号当前的维护资格，软暂停仍保持登录态。
                if not self._credentials.maintenance_allowed(candidate.credential_id):
                    report.skipped += 1
                    continue
                # 等锁期间可能已被另一轮续期或手工登录更新，不能再消费一张新刷新票。
                if not _needs_refresh(provider, data, self.skew_seconds, current):
                    report.skipped += 1
                    continue
                if self._stopping:
                    report.skipped += 1
                    continue
                # shield 的单位必须包含上游轮转与同步落库；取消不能在两者之间丢票。
                operation = asyncio.create_task(self._rotate_and_save(
                    provider, candidate.credential_id, data, report))
                self._rotations.add(operation)
                try:
                    await _wait_committed(operation)
                finally:
                    self._rotations.discard(operation)
        return report

    async def _rotate_and_save(self, provider, credential_id: str, data: dict,
                               report: TaskReport) -> None:
        """由 run_once 的编辑锁保护，停机也先完成票据与凭证状态的同步写入。"""
        try:
            with credential_scope(credential_id):
                refreshed = await provider.refresh(data)
        except UpstreamReloginRequired as error:
            logger.warning("refresh for %s needs re-login, disabling credential: %s",
                           credential_id, error_text(error))
            # 固定文案避免上游原文或票据被回显；失效状态也必须在停机前落库。
            self._credentials.mark_relogin_required(credential_id, RELOGIN_REASON)
            report.failed += 1
            return
        except Exception as error:  # noqa: BLE001 - 一条失败不阻断其他账号维护
            logger.warning("refresh failed for %s: %s", credential_id, error_text(error))
            report.failed += 1
            return
        try:
            self._credentials.save_credential_data(credential_id, refreshed)
        except Exception as error:  # noqa: BLE001 - 落库失败必须留痕，不能算作刷新成功
            logger.error("refresh credential commit failed for %s: %s",
                         credential_id, error_text(error))
            raise
        report.succeeded += 1

    async def drain(self) -> None:
        """停机前排空手工触发等非周期路径的活动刷新，仍不取消上游已提交的请求。"""
        self._stopping = True
        if not self._rotations:
            return
        results = await _wait_committed(asyncio.gather(*self._rotations, return_exceptions=True))
        for result in results:
            if isinstance(result, BaseException):
                raise result


async def _wait_committed(operation: asyncio.Future):
    """取消只延后到操作完成；重复取消也不能取消轮转任务或跳过同步落库。"""
    cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as error:
            cancellation = error
        except Exception:  # noqa: BLE001 - 下方 result 重新抛出操作的真实错误
            break
    try:
        return operation.result()
    finally:
        if cancellation is not None:
            raise cancellation


def _needs_refresh(provider, data: dict, skew_seconds: int, now: int) -> bool:
    builder = getattr(provider, "credential_from", None)
    if builder is None:
        return False
    credential = builder(data)
    needs = getattr(credential, "needs_refresh", None)
    return bool(needs(skew_seconds, now)) if callable(needs) else False
