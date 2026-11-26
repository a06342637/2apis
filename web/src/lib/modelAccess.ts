import type { ModelInfo } from "../api/types";

export interface ModelAccess {
  label: string;
  description: string;
  vip: boolean;
  available: boolean | undefined;
  requiredTier: string | undefined;
}

const TIERS: Record<string, string> = { basic: "Basic", standard: "Standard", pro: "Pro" };

/** 权限只使用账号目录元数据；共享模型按当前渠道读，绝不从价格或名称猜 VIP。 */
export function modelAccess(model: ModelInfo, provider?: string): ModelAccess {
  const channel = provider ?? (model.providers.length === 1 ? model.providers[0] : undefined);
  const detail = channel ? model.by_provider?.[channel] : undefined;
  // 多渠道条目没有本渠道权限时，不继承别的渠道聚合出来的 VIP 要求。
  const metadata = detail ?? (model.providers.length <= 1 ? model : {});
  const requiredTier = metadata.required_account_tier;
  const available = metadata.account_available;
  const reason = metadata.account_unavailable_reason;
  const vip = Boolean(requiredTier && requiredTier !== "free" && reason !== "feature_flag_off");
  if (reason === "feature_flag_off") {
    return { label: "暂不可用", description: "上游暂未开放此模型，与 VIP 套餐权限无关。",
      vip: false, available, requiredTier };
  }
  if (vip) {
    const tierLabel = Object.hasOwn(TIERS, requiredTier!) ? TIERS[requiredTier!] : requiredTier;
    return { label: "VIP", description: `需 Miora ${tierLabel} 套餐${available === false
      ? "；当前账号池不可用。" : "；仅分配给有权限的账号。"}`, vip, available, requiredTier };
  }
  return { label: available === false ? "暂不可用" : "当前账号可用",
    description: available === false ? "当前允许的账号均不可调用此模型。"
      : available === true ? "当前账号目录明确允许调用。" : "上游未声明额外套餐限制，调用按账号目录判断。",
    vip, available, requiredTier };
}

/** 稳定保留用途分组内原有顺序：普通/未知先，VIP 后，其余不可用最后。 */
export function sortModelsByAccess<T extends ModelInfo>(models: readonly T[], provider?: string): T[] {
  const rank = (model: T) => {
    const access = modelAccess(model, provider);
    return access.vip ? 1 : access.available === false ? 2 : 0;
  };
  return models.map((model, index) => ({ model, index })).sort((left, right) =>
    rank(left.model) - rank(right.model) || left.index - right.index).map(({ model }) => model);
}

/** 渠道映射后的展示条目直接使用保留的权限，未知旧条目保持原有顺序。 */
export function sortProviderModelsByAccess<T extends { access?: ModelAccess }>(models: readonly T[]): T[] {
  const rank = (model: T) => model.access?.vip ? 1 : model.access?.available === false ? 2 : 0;
  return models.map((model, index) => ({ model, index })).sort((left, right) =>
    rank(left.model) - rank(right.model) || left.index - right.index).map(({ model }) => model);
}
