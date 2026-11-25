import { credentialState, STATE_LABEL, STATE_TONE } from "../../api/display";
import { PROVIDER_LABEL } from "../../api/providers";
import type { Credential } from "../../api/types";
import { Badge, Notice } from "../../ui";

/** 未验证、已删除或跨渠道的非空选择必须阻止提交，不能悄悄变成全部账号。 */
export function credentialSelectionIssue(
  selected: string[],
  credentials: Credential[] | undefined,
  binding: string,
): string | null {
  if (selected.length === 0) return null;
  if (!credentials) return "账号列表尚未验证，请等待加载完成或重试。";
  const indexed = new Map(credentials.map((item) => [item.id, item]));
  if (selected.some((id) => !indexed.has(id))) {
    return "选择中有已删除或无权访问的账号，请明确移除后再保存。";
  }
  if (binding && selected.some((id) => indexed.get(id)?.provider !== binding)) {
    return "选择中有其他渠道的账号，请改回渠道或明确移除这些选择。";
  }
  return null;
}

export function CredentialSelector({
  credentials,
  selected,
  binding,
  onChange,
  testidPrefix,
  loading = false,
  onRetry,
}: {
  credentials: Credential[] | undefined;
  selected: string[];
  binding: string;
  onChange: (ids: string[]) => void;
  testidPrefix: string;
  loading?: boolean;
  onRetry?: () => void;
}) {
  const indexed = new Map((credentials ?? []).map((item) => [item.id, item]));
  const compatible = (credentials ?? []).filter((item) => !binding || item.provider === binding);
  const incompatible = selected.filter((id) => {
    const item = indexed.get(id);
    return !item || (binding && item.provider !== binding);
  });
  const issue = credentialSelectionIssue(selected, credentials, binding);
  const toggle = (id: string, checked: boolean) =>
    onChange(checked ? [...selected, id] : selected.filter((value) => value !== id));

  const row = (id: string, item: Credential | undefined, invalid = false) => {
    const state = item && credentialState(item);
    const expired = item && item.token_expires_at > 0 && item.token_expires_at <= Date.now() / 1000;
    return (
      <label key={id} className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs hover:bg-muted/40">
        <input
          type="checkbox"
          checked={selected.includes(id)}
          data-testid={`${testidPrefix}-credential-${id}`}
          onChange={(event) => toggle(id, event.target.checked)}
        />
        <span className="min-w-0 flex-1 break-all">
          {item ? `${item.nickname || item.id} · ${PROVIDER_LABEL[item.provider]}` : id}
        </span>
        {item && state && <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>}
        {expired && <Badge tone="warn">凭证已过期</Badge>}
        {invalid && <Badge tone="danger">{item ? "跨渠道" : credentials ? "已删除 / 无权访问" : "等待验证"}</Badge>}
      </label>
    );
  };

  return (
    <fieldset className="space-y-2" data-testid={`${testidPrefix}-credentials`}>
      <legend className="text-sm font-medium">账号范围（可选）</legend>
      <p className="text-xs text-muted-foreground">
        不勾选 = 当前渠道全部账号；勾选多个后只在这些账号中负载均衡，均不可用时返回错误。
      </p>
      <p className="text-xs" data-testid={`${testidPrefix}-credential-summary`}>
        {selected.length ? `已指定 ${selected.length} 个账号` : "当前渠道全部账号（含以后添加的账号）"}
      </p>
      {credentials ? (
        <div className="max-h-48 overflow-y-auto rounded-md border border-border p-1">
          {compatible.map((item) => row(item.id, item))}
          {compatible.length === 0 && <p className="p-2 text-xs text-muted-foreground">当前渠道没有账号。</p>}
          {incompatible.map((id) => row(id, indexed.get(id), true))}
        </div>
      ) : (
        <>
          <p className="text-xs text-muted-foreground">
            {loading ? "正在加载账号…" : "账号列表加载失败；仍可选择不限定账号，或重试后指定。"}
          </p>
          {incompatible.map((id) => row(id, undefined, true))}
          {!loading && onRetry && <button type="button" className="text-xs underline" onClick={onRetry}>重试账号列表</button>}
        </>
      )}
      {issue && <Notice tone="warn">{issue}</Notice>}
      {selected.length > 0 && (
        <button type="button" className="text-xs underline" data-testid={`${testidPrefix}-clear-credentials`}
                onClick={() => onChange([])}>
          清空选择，允许当前渠道全部账号
        </button>
      )}
      <p className="text-xs text-muted-foreground">暂停、禁用或过期账号仍可勾选，恢复可用后参与调度。</p>
    </fieldset>
  );
}
