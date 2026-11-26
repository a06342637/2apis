"""Miora 订阅状态的匿名化展示；不作为模型调用权限的授权依据。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

TIERS = {"free": "free", "basic": "basic", "standard": "standard", "pro": "pro",
         "std": "standard", "trial_basic": "basic", "trial_standard": "standard",
         "trial_pro": "pro"}
BILLING_TIMEZONE = timezone(timedelta(hours=8))


def _expiry(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        # 官方网页把无时区 expireAt 当本地日期解析；中文账务按北京时间统一展示。
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=BILLING_TIMEZONE)
        return int(moment.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def subscription_state(data: Any, *, now: int) -> dict[str, Any]:
    """只保留套餐/有效期/探测时间；失败或未知值不能推成普通账号。"""
    result = {"tier": None, "status": "unknown", "vip": None,
              "expires_at": None, "probed_at": now}
    if not isinstance(data, dict):
        return result
    code = data.get("tierCode")
    tier = TIERS.get(code.strip().lower()) if isinstance(code, str) else None
    expires = _expiry(data.get("expireAt"))
    result.update(tier=tier, expires_at=expires)
    if tier is None:
        return result
    if data.get("status") == "expired" or expires is not None and expires <= now:
        result.update(status="expired", vip=False)
    elif tier == "free" and data.get("status") == "active":
        result.update(status="free", vip=False)
    elif tier != "free" and data.get("status") == "active" and expires is not None:
        result.update(status="active", vip=True)
    return result


def subscription_signature(detail: Any, *, now: int) -> tuple[Any, ...] | None:
    """只在套餐/状态/到期边界变更时失效目录，探测时间变更不刷新模型。"""
    plan = detail.get("miora_subscription") if isinstance(detail, dict) else None
    if not isinstance(plan, dict):
        return None
    value = plan.get("expires_at")
    valid = ((isinstance(value, int) and not isinstance(value, bool))
             or (isinstance(value, float) and math.isfinite(value)))
    expires = value if valid else None
    expired = expires is not None and expires <= now
    return (plan.get("tier"), "expired" if expired else plan.get("status"),
            False if expired else plan.get("vip"), expires)
