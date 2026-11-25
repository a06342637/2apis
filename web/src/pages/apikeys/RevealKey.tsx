import { Eye, EyeOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { revealApiKey } from "../../api/apiKeys";
import { ApiError } from "../../api/client";
import { useDialogFocus } from "../../hooks/useDialogFocus";
import { Button, Field, Input } from "../../ui";

export const REVEAL_KEY_TIMEOUT_MS = 60_000;
const OLD_KEY_MESSAGE = "旧 Key 未保存可恢复的明文，请创建新 Key。";

function revealError(caught: unknown): string {
  if (caught instanceof ApiError) {
    if (caught.code === "key_not_recoverable") return OLD_KEY_MESSAGE;
    if (caught.code === "invalid_password") return "当前登录密码不正确，请重新输入。";
    if (caught.status === 429) return "验证过于频繁，请稍后重试。";
    if (caught.status === 401) return "登录已过期，请重新登录后查看。";
    if (caught.status === 404) return "此 Key 已删除，或当前服务尚不支持查看。";
    if (caught.status === 403) return "当前账号无权查看此 Key。";
  }
  // 不展示任意响应文案，避免异常响应将密码或 Key 回显到错误提示。
  return "查看失败，请稍后重试。";
}

async function copyKey(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // HTTP 部署/剪贴板权限限制时尝试临时 textarea，始终删除它。
  }
  const area = document.createElement("textarea");
  try {
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    return document.execCommand?.("copy") ?? false;
  } catch {
    return false;
  } finally {
    area.value = "";
    area.remove();
  }
}

/** 密码和明文仅留在此组件：取消、超时、换行或卸载均撤销晚到响应。 */
export function RevealKey({ id, preview, canReveal }: {
  id: string;
  preview: string;
  canReveal?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [plainKey, setPlainKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [copyLabel, setCopyLabel] = useState("复制");
  const epoch = useRef(0);
  const inFlight = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const revoke = useCallback(() => {
    epoch.current += 1;
    inFlight.current?.abort();
    inFlight.current = null;
    clearTimeout(timer.current);
    timer.current = undefined;
  }, []);
  const clearSecrets = useCallback(() => {
    revoke();
    setOpen(false);
    setPassword("");
    setPlainKey("");
    setBusy(false);
    setError("");
    setCopyLabel("复制");
  }, [revoke]);
  const startTimeout = () => {
    clearTimeout(timer.current);
    timer.current = setTimeout(clearSecrets, REVEAL_KEY_TIMEOUT_MS);
  };
  useEffect(() => {
    clearSecrets();
    return revoke;
  }, [id, canReveal, clearSecrets, revoke]);
  const dialogRef = useDialogFocus<HTMLFormElement>(clearSecrets, open);

  const showDialog = () => {
    clearSecrets();
    setOpen(true);
    startTimeout();
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!password || inFlight.current) return;
    const controller = new AbortController();
    inFlight.current = controller;
    const attempt = epoch.current;
    setBusy(true);
    setError("");
    const pending = revealApiKey(id, password, controller.signal);
    setPassword("");
    try {
      const result = await pending;
      if (epoch.current !== attempt || controller.signal.aborted) return;
      if (typeof result?.api_key !== "string" || !result.api_key) {
        setError("查看失败，请稍后重试。");
        return;
      }
      setOpen(false);
      setPlainKey(result.api_key);
      startTimeout();
    } catch (caught) {
      if (epoch.current !== attempt || controller.signal.aborted) return;
      setError(revealError(caught));
    } finally {
      if (epoch.current === attempt) {
        inFlight.current = null;
        setBusy(false);
      }
    }
  };
  const copy = async () => {
    const attempt = epoch.current;
    const copied = await copyKey(plainKey);
    if (epoch.current === attempt) setCopyLabel(copied ? "已复制" : "请手动复制");
  };

  if (canReveal === false) {
    return <span className="inline-grid gap-1">
      <span>{preview}</span>
      <span className="font-sans text-muted-foreground text-xs">{OLD_KEY_MESSAGE}</span>
    </span>;
  }
  return <>
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <code className="max-w-xs break-all select-all" data-testid={`key-preview-${id}`}>
        {plainKey || preview}
      </code>
      {plainKey ? <>
        <Button size="sm" variant="ghost" onClick={copy}>{copyLabel}</Button>
        <Button size="sm" variant="ghost" onClick={clearSecrets} aria-label="隐藏完整 Key">
          <EyeOff className="size-3.5" /> 隐藏
        </Button>
        <span className="font-sans text-xs text-muted-foreground">60 秒后自动隐藏</span>
      </> : <Button size="sm" variant="ghost" onClick={showDialog} aria-label="查看完整 Key">
        <Eye className="size-3.5" /> 查看
      </Button>}
    </span>
    {open && <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
      onClick={clearSecrets} data-testid="reveal-key-backdrop">
      <form ref={dialogRef} role="dialog" aria-modal="true" aria-label="查看 API Key"
        className="w-full max-w-sm rounded-xl border border-border bg-card p-5 shadow-xl font-sans"
        onClick={(event) => event.stopPropagation()} onSubmit={submit}>
        <h2 className="mb-3 text-sm font-semibold">查看 API Key</h2>
        <p className="mb-4 text-xs text-muted-foreground">请输入当前登录账号的密码。完整 Key 显示 60 秒后自动隐藏。</p>
        <Field label="当前登录密码" htmlFor={`reveal-key-password-${id}`}>
          <Input id={`reveal-key-password-${id}`} type="password" autoComplete="off"
            value={password} disabled={busy}
            onChange={(event) => setPassword(event.target.value)} />
        </Field>
        {error && <p role="alert" className="mt-3 text-xs text-destructive">{error}</p>}
        <div className="mt-4 flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={clearSecrets}>取消</Button>
          <Button type="submit" variant="primary" disabled={busy || !password}>
            {busy ? "验证中…" : "验证并查看"}
          </Button>
        </div>
      </form>
    </div>}
  </>;
}
