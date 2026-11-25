import { PROVIDER_LABEL } from "../../api/providers";
import type { Credential, CredentialGroup } from "../../api/types";
import { Notice } from "../../ui";
import { credentialSelectionIssue, CredentialSelector } from "./CredentialSelector";

export type AccountScopeMode = "all" | "accounts" | "groups";

export function accountScopeMode(credentialIds: string[], groupIds: string[]): AccountScopeMode {
  return groupIds.length ? "groups" : credentialIds.length ? "accounts" : "all";
}

export function accountScopeIssue(mode: AccountScopeMode, credentialIds: string[],
  groupIds: string[], credentials: Credential[] | undefined,
  groups: CredentialGroup[] | undefined, binding: string): string | null {
  if (credentialIds.length && groupIds.length) return "账号和分组不能同时指定，请重新选择调度模式。";
  if (mode === "all") return null;
  if (mode === "accounts") {
    return credentialIds.length ? credentialSelectionIssue(credentialIds, credentials, binding)
      : "请勾选至少一个账号，或明确改为全部账号。";
  }
  if (!groupIds.length) return "请勾选至少一个分组，或明确改为全部账号。";
  if (!groups) return "分组列表尚未验证，请等待加载完成或重试。";
  const indexed = new Map(groups.map((group) => [group.id, group]));
  if (groupIds.some((id) => !indexed.has(id))) return "选择中有已删除或无权访问的分组，请明确移除后再保存。";
  if (binding && groupIds.some((id) => indexed.get(id)?.provider !== binding)) {
    return "选择中有其他渠道的分组，请改回渠道或明确移除这些选择。";
  }
  return null;
}

export function AccountScopeSelector({ mode, credentialIds, groupIds, credentials, groups,
  binding, onChange, testidPrefix, accountsLoading, groupsLoading, retryAccounts, retryGroups }: {
  mode: AccountScopeMode; credentialIds: string[]; groupIds: string[];
  credentials: Credential[] | undefined; groups: CredentialGroup[] | undefined; binding: string;
  onChange: (mode: AccountScopeMode, credentials: string[], groups: string[]) => void;
  testidPrefix: string; accountsLoading?: boolean; groupsLoading?: boolean;
  retryAccounts?: () => void; retryGroups?: () => void;
}) {
  const issue = accountScopeIssue(mode, credentialIds, groupIds, credentials, groups, binding);
  const indexed = new Map((groups ?? []).map((group) => [group.id, group]));
  const visible = (groups ?? []).filter((group) => !binding || group.provider === binding);
  const missing = groupIds.filter((id) => !indexed.has(id) || (binding && indexed.get(id)?.provider !== binding));
  const groupRow = (id: string) => {
    const group = indexed.get(id);
    return <label key={id} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs">
      <input type="checkbox" checked={groupIds.includes(id)} data-testid={`${testidPrefix}-group-${id}`}
        onChange={(event) => onChange("groups", [], event.target.checked
          ? [...groupIds, id] : groupIds.filter((value) => value !== id))} />
      <span>{group ? `${group.name} · ${PROVIDER_LABEL[group.provider]} · ${group.member_count} 个账号`
        : `${id} · 已删除 / 无权访问 / 待验证`}</span>
    </label>;
  };
  return <div className="space-y-3" data-testid={`${testidPrefix}-account-scope`}>
    <fieldset className="flex flex-wrap gap-4">
      <legend className="mb-2 text-sm font-medium">调度范围</legend>
      {([["all", "全部账号"], ["accounts", "按账号"], ["groups", "按分组"]] as const).map(([value, label]) =>
        <label key={value} className="flex items-center gap-1.5 text-xs">
          <input type="radio" name={`${testidPrefix}-scope-mode`} value={value} checked={mode === value}
            data-testid={`${testidPrefix}-scope-${value}`} onChange={() => onChange(value, [], [])} />{label}
        </label>)}
    </fieldset>
    {mode !== "groups" ? <CredentialSelector credentials={credentials} selected={credentialIds}
      binding={binding} testidPrefix={testidPrefix} loading={accountsLoading} onRetry={retryAccounts}
      onChange={(ids) => onChange("accounts", ids, [])} onClearAll={() => onChange("all", [], [])}
      emptySummary={mode === "accounts" ? "尚未指定账号" : undefined}
      selectionHint={mode === "accounts" ? "至少勾选一个账号；暂停或禁用的账号恢复后参与调度。" : undefined} />
      : <fieldset className="space-y-2" data-testid={`${testidPrefix}-groups`}>
        <legend className="text-sm font-medium">选择账号分组（可多选）</legend>
        <p className="text-xs text-muted-foreground">绑定稳定分组 ID，修改组成员后新请求立即使用新范围；空组或删除组不会扩大范围。</p>
        {groups ? <div className="max-h-48 overflow-y-auto rounded-md border border-border p-1">
          {visible.map((group) => groupRow(group.id))}
          {!visible.length && <p className="p-2 text-xs text-muted-foreground">当前渠道没有账号分组，请到凭证页创建。</p>}
          {missing.map(groupRow)}
        </div> : <>
          <p className="text-xs text-muted-foreground">{groupsLoading ? "正在加载账号分组…" : "账号分组加载失败，请重试。"}</p>
          {missing.map(groupRow)}
          {!groupsLoading && retryGroups && <button type="button" className="text-xs underline" onClick={retryGroups}>重试分组列表</button>}
        </>}
        {groupIds.length > 0 && groups && groupIds.every((id) => indexed.get(id)?.member_count === 0) &&
          <Notice tone="warn">所选分组当前没有成员，请求将返回错误；添加成员后自动生效。</Notice>}
      </fieldset>}
    {issue && (mode === "groups" || !credentialIds.length) && <Notice tone="warn">{issue}</Notice>}
  </div>;
}
