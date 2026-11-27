import type { Credential } from "../../api/types";

export interface MioraAccountPlanView {
  label: string;
  tone: "ok" | "accent" | "muted" | "warn";
  description: string;
  expiresAt: number | null;
  remainingSeconds: number | null;
  expiring: boolean;
}

const PAID_NAMES: Record<string, string> = { basic: "Basic", standard: "Standard", pro: "Pro" };
const UNKNOWN: MioraAccountPlanView = { label: "套餐未获取", tone: "muted",
  description: "尚未获取明确套餐信息；可探测额度后更新，不能据余额推断免费或 VIP。",
  expiresAt: null, remainingSeconds: null, expiring: false };

/** 会员日期统一显示北京时间，和 Miora 账号自动昵称及导出日期同一口径。 */
export function mioraPlanExpiryTime(expiresAt: number): string {
  return new Date(expiresAt * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
}

/** 套餐仅说明账号状态，模型权限和消耗倍率仍由上游模型目录决定。 */
export function mioraAccountPlan(credential: Credential, now: number): MioraAccountPlanView | null {
  if (credential.provider !== "miora") return null;
  const subscription = credential.quota_detail?.miora_subscription;
  if (!subscription) return UNKNOWN;
  const expires = subscription.expires_at;
  const validExpiry = typeof expires === "number" && expires > 0
    && Number.isFinite(new Date(expires * 1000).getTime());
  const paidName = subscription.tier ? PAID_NAMES[subscription.tier] : undefined;
  if (subscription.status === "expired" || (subscription.status !== "unknown" && validExpiry && expires <= now)) {
    return { label: "套餐已到期", tone: "warn",
      description: `${paidName ? `${paidName} ` : ""}套餐已到期${validExpiry ? `（${mioraPlanExpiryTime(expires)}，北京时间）` : ""}；不再标记为有效 VIP。`,
      expiresAt: validExpiry ? expires : null, remainingSeconds: validExpiry ? 0 : null, expiring: false };
  }
  if (credential.health === null) {
    return { ...UNKNOWN, description: "最新额度探测未成功，旧套餐记录尚未确认；不据缓存余额判断免费或 VIP。" };
  }
  if (subscription.status === "free" && subscription.tier === "free" && subscription.vip === false) {
    return { label: "免费", tone: "ok", description: "免费账号；无需 VIP 的模型仍按实际倍率扣赠送积分。",
      expiresAt: null, remainingSeconds: null, expiring: false };
  }
  if (subscription.status === "active" && paidName && subscription.vip === true && validExpiry && expires > now) {
    return { label: `VIP · ${paidName}`, tone: "accent", description: `${paidName} 套餐，到期时间：${mioraPlanExpiryTime(expires)}（北京时间）。具体模型权限以上游目录为准。`,
      expiresAt: expires, remainingSeconds: Math.ceil(expires - now), expiring: expires - now <= 7 * 86400 };
  }
  return UNKNOWN;
}
