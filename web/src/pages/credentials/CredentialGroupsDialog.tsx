import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { createCredentialGroup, deleteCredentialGroup, updateCredentialGroup } from "../../api/credentialGroups";
import type { Credential, CredentialGroup, Provider } from "../../api/types";
import { useDialogFocus } from "../../hooks/useDialogFocus";
import { errorText } from "../../lib/errors";
import { Button, Field, Input, Notice } from "../../ui";
import { credentialSelectionIssue, CredentialSelector } from "../apikeys/CredentialSelector";

export function CredentialGroupsDialog({ provider, label, credentials, groups, loading, canWrite,
  onRetry, onChanged, onClose }: {
  provider: Provider; label: string; credentials: Credential[];
  groups: CredentialGroup[] | undefined; loading: boolean; canWrite: boolean;
  onRetry: () => Promise<CredentialGroup[] | undefined>;
  onChanged: () => Promise<unknown>;
  onClose: () => void;
}) {
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<CredentialGroup | null>(null);
  const [name, setName] = useState("");
  const [members, setMembers] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const editingGeneration = useRef(0);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; editingGeneration.current += 1; };
  }, []);
  const close = () => { if (!working.current) { editingGeneration.current += 1; onClose(); } };
  const dialogRef = useDialogFocus<HTMLDivElement>(close);
  const visible = groups?.filter((group) => group.provider === provider);
  const groupNames = new Map<string, string[]>();
  for (const group of visible ?? []) {
    for (const id of group.credential_ids ?? []) {
      const names = groupNames.get(id) ?? [];
      names.push(group.name);
      groupNames.set(id, names);
    }
  }
  const issue = credentialSelectionIssue(members, credentials, provider);

  const openEditor = (group: CredentialGroup | null) => {
    editingGeneration.current += 1;
    if (group && !Array.isArray(group.credential_ids)) {
      setError("无法读取组成员，请重试分组列表后再编辑。");
      return;
    }
    setEditing(group); setName(group?.name ?? ""); setMembers([...(group?.credential_ids ?? [])]);
    setEditorOpen(true); setError(""); setConflict(false); setConfirmDelete(null);
  };
  const perform = async (action: () => Promise<unknown>) => {
    if (working.current) return;
    working.current = true; setBusy(true); setError(""); setConflict(false);
    try {
      await action();
      await onChanged();
    } catch (caught) {
      const stale = caught instanceof ApiError && caught.status === 409;
      setConflict(stale && editing !== null);
      setError(stale ? "分组已被修改或名称已存在，请检查后重新载入再保存。"
        : errorText(caught, "分组操作失败，请稍后重试。"));
    } finally {
      working.current = false; setBusy(false);
    }
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!canWrite || !name.trim() || issue) return;
    await perform(async () => {
      if (editing) await updateCredentialGroup(editing.id, {
        name: name.trim(), credential_ids: members, expected_updated_at: editing.updated_at,
      });
      else await createCredentialGroup({ provider, name: name.trim(), credential_ids: members });
      setEditorOpen(false); setEditing(null); setMembers([]); setName("");
    });
  };
  const reloadEditor = async () => {
    const generation = editingGeneration.current;
    try {
      const latest = await onRetry();
      if (!mounted.current || editingGeneration.current !== generation) return;
      const group = latest?.find((item) => item.id === editing?.id);
      if (group) openEditor(group);
      else setError("此分组已删除或暂时无法读取，请关闭编辑后重试。");
    } catch (caught) {
      if (mounted.current && editingGeneration.current === generation) {
        setError(errorText(caught, "重新载入失败，请稍后重试。"));
      }
    }
  };
  return <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4 backdrop-blur-sm" onClick={close}>
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-label={`${label}账号分组`}
      data-testid="credential-groups-dialog" onClick={(event) => event.stopPropagation()}
      className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-xl border border-border bg-card p-5 shadow-xl">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold">{label} · 账号分组</h2>
        <Button size="sm" variant="ghost" disabled={busy} onClick={close}>关闭</Button>
      </div>
      <p className="mb-3 text-xs text-muted-foreground">分组属于固定渠道，可重命名和调整成员。API Key 按组绑定后，新请求随成员变化动态生效。</p>
      {!groups ? <div className="space-y-2">
        <p role={loading ? undefined : "alert"}>{loading ? "正在加载账号分组…" : "账号分组加载失败。"}</p>
        {!loading && <Button size="sm" onClick={() => void onRetry()}>重试分组列表</Button>}
      </div> : <>
        <ul className="space-y-2" data-testid="credential-group-list">
          {visible?.map((group) => <li key={group.id} className="flex flex-wrap items-center gap-2 rounded-md border border-border p-2">
            <span className="min-w-0 flex-1 break-all text-sm">{group.name} <span className="text-xs text-muted-foreground">{group.member_count} 个账号</span></span>
            {canWrite && (confirmDelete === group.id ? <>
              <Button size="sm" variant="danger" disabled={busy} onClick={() => void perform(async () => {
                await deleteCredentialGroup(group.id); setConfirmDelete(null);
                if (editing?.id === group.id) setEditorOpen(false);
              })}>确认删除分组</Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirmDelete(null)}>取消删除</Button>
            </> : <>
              <Button size="sm" disabled={busy} onClick={() => openEditor(group)} data-testid={`edit-group-${group.id}`}>编辑</Button>
              <Button size="sm" variant="ghost" disabled={busy} onClick={() => setConfirmDelete(group.id)} data-testid={`delete-group-${group.id}`}>删除</Button>
            </>)}
          </li>)}
          {visible?.length === 0 && <li className="text-xs text-muted-foreground">此渠道还没有账号分组。</li>}
        </ul>
        {!canWrite && <p className="mt-3 text-xs text-muted-foreground">当前账号只能查看分组名称与成员数量。</p>}
        {canWrite && !editorOpen && <Button size="sm" className="mt-3" disabled={busy} onClick={() => openEditor(null)}>新建分组</Button>}
      </>}
      {confirmDelete && <p className="mt-3 text-xs text-warn-ink">删除后，绑定此组的 API Key 需要重新调整范围才能使用。</p>}
      {editorOpen && canWrite && <form className="mt-4 space-y-3 border-t border-border pt-4" onSubmit={save}>
        <Field label="分组名称" htmlFor="credential-group-name">
          <Input id="credential-group-name" value={name} maxLength={128} disabled={busy} onChange={(event) => setName(event.target.value)} />
        </Field>
        <fieldset disabled={busy}><CredentialSelector legend="组成员" credentials={credentials} selected={members} binding={provider}
          onChange={setMembers} testidPrefix="group-edit" emptySummary="当前分组无成员"
          groupNames={groupNames}
          selectionHint="账号后显示已加入的分组，同一账号可加入多个分组。暂停、禁用或过期账号仍可加入；不勾选会创建空组。"
          clearText="清空组成员" /></fieldset>
        <div className="flex justify-end gap-2">
          <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => {
            editingGeneration.current += 1; setEditorOpen(false); setEditing(null); setError(""); setConflict(false);
          }}>取消编辑</Button>
          <Button type="submit" size="sm" variant="primary" disabled={busy || !name.trim() || Boolean(issue)}>保存分组</Button>
        </div>
      </form>}
      {error && <div className="mt-3"><Notice tone="danger">{error}</Notice></div>}
      {conflict && <Button size="sm" className="mt-2" onClick={() => void reloadEditor()}>重新载入此组</Button>}
    </div>
  </div>;
}
