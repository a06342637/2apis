import type { Credential, CredentialGroup } from "../../api/types";
import { providerLabel } from "../../api/providers";
import { providerLedger, type ProviderGroup } from "../../lib/credentialList";

export interface AccountSection {
  key: string;
  /** 真实自建组才有 ID；未分组/成员关系不可读的渠道行不冒充组内行。 */
  groupId?: string;
  name: string;
  items: Credential[];
  memberCount: number;
  membershipKnown: boolean;
  selectable: boolean;
}

/** 二级组只重排展示位置；组内沿用渠道的筛选/排序，同一账号可出现在多个组。 */
export function accountSections(group: ProviderGroup, saved: CredentialGroup[] | undefined): AccountSection[] | null {
  if (!saved) return null;
  const own = saved.filter((item) => item.provider === group.provider);
  const assigned = new Set(own.flatMap((item) => item.credential_ids ?? []));
  const membershipKnown = own.every((item) => Array.isArray(item.credential_ids));
  const sections: AccountSection[] = own.map((item) => {
    const members = new Set(item.credential_ids ?? []);
    return { key: item.id, groupId: item.id, name: item.name, memberCount: item.member_count,
      membershipKnown: Array.isArray(item.credential_ids), selectable: true,
      items: group.items.filter((credential) => members.has(credential.id)) };
  });
  const remaining = group.items.filter((credential) => !assigned.has(credential.id));
  if (remaining.length || own.length === 0) sections.push({
    key: `ungrouped-${group.key}`, name: membershipKnown ? "未分组" : "渠道账号",
    items: remaining, memberCount: remaining.length, membershipKnown: true, selectable: own.length > 0,
  });
  return sections;
}

/** 已保存的空组仍保留渠道入口，不制造虚拟账号或改变额度统计。 */
export function withAccountGroupProviders(groups: ProviderGroup[], saved: CredentialGroup[]): ProviderGroup[] {
  const result = [...groups];
  const seen = new Set(groups.map((group) => group.provider));
  for (const item of saved) {
    if (seen.has(item.provider)) continue;
    seen.add(item.provider);
    result.push({ key: item.provider, provider: item.provider, label: providerLabel(item.provider),
      ledger: providerLedger(item.provider), items: [], states: {}, remaining: null, total: null,
      expiring: null, expiringSecondary: null, checkin: { done: 0, failed: 0, pending: 0, lastAt: null } });
  }
  return result;
}
