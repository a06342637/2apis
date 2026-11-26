"""Miora 目录明确下发的账号权限；不从价格、型号或套餐名称猜权限。"""

from __future__ import annotations

from typing import Any


def model_access(row: dict[str, Any], parent: dict[str, Any] | None = None) -> dict[str, Any]:
    """变体缺权限字段时继承父目录，显式值优先，与官方模型选择器一致。"""
    inherited = parent or {}
    available = row.get("available")
    if not isinstance(available, bool):
        available = inherited.get("available")
    tier = row.get("requiredTier", inherited.get("requiredTier"))
    reason = row.get("unavailableReason", inherited.get("unavailableReason"))
    return {
        "account_available": available if isinstance(available, bool) else None,
        "required_account_tier": tier.strip() if isinstance(tier, str) and tier.strip() else None,
        "account_unavailable_reason": (
            reason.strip() if isinstance(reason, str) and reason.strip() else None
        ),
    }
