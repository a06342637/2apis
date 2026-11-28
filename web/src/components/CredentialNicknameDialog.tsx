import { useState, type FormEvent } from "react";
import { api } from "../api/client";
import type { Credential } from "../api/types";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { errorText } from "../lib/errors";
import { Button, Field, Input } from "../ui";

// 与后端按 Unicode 码点计数的上限一致（JS 字符串 length 会把部分字符算两次）。
const MAX_NICKNAME_LENGTH = 128;

export function CredentialNicknameDialog({ credential, onClose, onSaved }: {
  credential: Credential;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [raw, setRaw] = useState(credential.nickname);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useDialogFocus<HTMLDivElement>(busy ? undefined : onClose);

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (busy) return;
    const nickname = raw.trim();
    if ([...nickname].length > MAX_NICKNAME_LENGTH) {
      setError(`昵称最长 ${MAX_NICKNAME_LENGTH} 个字符。`);
      return;
    }
    setBusy(true);
    setError("");
    try {
      await api.setCredentialNickname(credential.id, nickname);
      onSaved();
    } catch (caught) {
      setError(errorText(caught, "昵称保存失败，请重试。"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm"
      onClick={() => { if (!busy) onClose(); }}>
      <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="credential-nickname-title"
        onClick={(event) => event.stopPropagation()}
        className="max-h-[90vh] w-full max-w-md overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl">
        <h2 id="credential-nickname-title" className="mb-3 text-sm font-semibold">修改昵称</h2>
        <form className="space-y-3" onSubmit={save}>
          <Field label="凭证昵称" htmlFor="credential-nickname-input"
            hint={`最多 ${MAX_NICKNAME_LENGTH} 个字符；留空则显示凭证 ID。不影响上游账号或调度。`}>
            <Input id="credential-nickname-input" value={raw} disabled={busy}
              onChange={(event) => { setRaw(event.target.value); setError(""); }} />
          </Field>
          {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
          <div className="flex justify-end gap-2">
            <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={onClose}>取消</Button>
            <Button type="submit" size="sm" variant="primary" disabled={busy}>
              {busy ? "保存中…" : "保存昵称"}
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
