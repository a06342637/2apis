"""Miora 真实积分包与最早到期汇总；会员有效期不替代积分作废时间。"""

from __future__ import annotations

import math
from typing import Any

from ..base import UpstreamProtocolViolation
from .subscription import _expiry

PACKS_API = "/api/miora-billing/overview/packs"
MAX_CREDIT_PACKAGES = 512
EXPIRING_WINDOW_SECONDS = 7 * 24 * 3600
PACK_NAMES = {"subscription": "基础积分", "bonus": "福利积分", "compensation": "补偿积分",
              "welcome": "注册赠送积分", "topup": "购买积分"}


def _amount(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def next_expiring_credit(data: Any, *, now: int, remaining: float) -> tuple[int, float] | None:
    """credit.nextExpiring 只代表该批积分，不是全部余额的截止时间。"""
    row = data.get("nextExpiring") if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return None
    end, credit = _expiry(row.get("expiredAt")), _amount(row.get("credit"))
    if end is None or end <= now or credit is None or credit <= 0 or remaining <= 0:
        return None
    return end, min(credit, remaining)


def credit_packages(data: Any, *, now: int) -> tuple[list[dict], list[tuple[int, float]] | None]:
    """官网 groups/items 的独立包快照；无效整批明细退回真实余额汇总。"""
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        raise UpstreamProtocolViolation("Miora 积分包目录格式异常")
    packages: list[dict] = []
    ladder: list[tuple[int, float]] = []
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            raise UpstreamProtocolViolation("Miora 积分包分组格式异常")
        for row in group["items"]:
            if len(packages) >= MAX_CREDIT_PACKAGES:
                raise UpstreamProtocolViolation("Miora 积分包数量超过安全限制")
            if not isinstance(row, dict):
                raise UpstreamProtocolViolation("Miora 积分包格式异常")
            total, used = _amount(row.get("total")), _amount(row.get("used"))
            if total is None or used is None:
                raise UpstreamProtocolViolation("Miora 积分包余额格式异常")
            end = _expiry(row.get("endAt"))
            status = row.get("status")
            available = status == "active" and (end is None or end > now)
            left = max(0.0, total - used)
            subtype = row.get("subPackType")
            name = PACK_NAMES.get(subtype) if isinstance(subtype, str) else None
            source_name = row.get("packageName")
            name = name or (source_name.strip() if isinstance(source_name, str) else "")
            package = {"name": name or "Miora 积分包", "total": total, "used": used, "end": end,
                       "usable": available}
            # 未知日期不能猜永不过期；7天内到期量只由本包明确的endAt推导。
            package["expiring"] = (left if available and end <= now + EXPIRING_WINDOW_SECONDS
                                   else 0.0) if end is not None else None
            packages.append(package)
            if available and end is not None and left > 0:
                ladder.append((end, left))
    return packages, sorted(ladder) or None
