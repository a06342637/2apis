import { PROVIDER_LABEL } from "../api/providers";
import type { Provider } from "../api/types";
import { useLoginLink, type LoginLinkCancelRegistry } from "../hooks/useLoginLink";
import { Button, Field, Input } from "../ui";

export function CredentialLoginLink({ provider, cancelRegistry, disabled, onNotice, onImported }: {
  provider: Provider;
  cancelRegistry: LoginLinkCancelRegistry;
  disabled: boolean;
  onNotice: (message: string) => void;
  onImported: (message: string) => void;
}) {
  const { row, refresh, copy, complete, cancel, setCallback } = useLoginLink(
    provider, cancelRegistry, { onNotice, onImported }, disabled);
  const prefix = `login-link-${provider}`;
  const busy = disabled || row.loading || row.completing;
  return (
    <details className="rounded border border-border p-3" data-testid={prefix}>
      <summary className="cursor-pointer text-xs font-medium" data-testid={`${prefix}-summary`}>
        {PROVIDER_LABEL[provider]} 登录链接
      </summary>
      <div className="mt-3 space-y-2">
        <p className="text-xs text-muted-foreground">不打开弹窗，可复制到另一浏览器完成授权。</p>
        <Field label={`${PROVIDER_LABEL[provider]} 登录链接`} htmlFor={`${prefix}-url`}>
          <Input id={`${prefix}-url`} data-testid={`${prefix}-url`} value={row.authUrl}
            readOnly placeholder="点击获取登录链接" />
        </Field>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" disabled={busy || !row.authUrl || !row.state}
            data-testid={`${prefix}-copy`} onClick={() => void copy()}>复制链接</Button>
          <Button size="sm" disabled={busy} data-testid={`${prefix}-refresh`}
            onClick={() => void refresh()}>{row.loading ? "获取中…" : row.authUrl ? "刷新链接" : "获取链接"}</Button>
          {provider === "officeace" && <Button size="sm" disabled={busy}
            data-testid={`${prefix}-switch`} onClick={() => void refresh(true)}>换账号刷新</Button>}
          {row.state && <Button size="sm" variant="ghost" data-testid={`${prefix}-cancel`}
            onClick={() => void cancel()}>取消链接登录</Button>}
        </div>
        <Field label={`${PROVIDER_LABEL[provider]} 回调链接`} htmlFor={`${prefix}-callback`}
          hint={row.flow === "poll" ? "可留空，授权后自动检测。" : "浏览器打不开回调时粘贴地址栏的整条链接。"}>
          <Input id={`${prefix}-callback`} data-testid={`${prefix}-callback`} value={row.callback}
            disabled={!row.state || row.loading || row.completing}
            onChange={event => setCallback(event.target.value)}
            placeholder={provider === "codearts"
              ? "http://127.0.0.1:12800/oauth/callback?code=…&state=…" : "完整授权回调链接"} />
        </Field>
        <Button size="sm" variant="primary" disabled={busy || !row.state
          || (row.flow !== "poll" && !row.callback.trim())} data-testid={`${prefix}-complete`}
          onClick={() => void complete()}>{row.completing ? "完成中…" : "完成登录"}</Button>
        {row.hint && <p role="status" data-testid={`${prefix}-hint`}
          className={`text-xs ${row.tone === "err" ? "text-red-600" : row.tone === "ok"
            ? "text-green-600" : "text-muted-foreground"}`}>{row.hint}</p>}
      </div>
    </details>
  );
}
