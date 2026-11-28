import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { createCredentialGroup, deleteCredentialGroup, listCredentialGroups, updateCredentialGroup } from "../api/credentialGroups";
import { ApiError } from "../api/client";
import type { BatchImportEntry, BatchImportResult, CredentialGroup, Provider } from "../api/types";
import { providerLabel } from "../api/providers";
import { errorText } from "../lib/errors";
import { Button, Input, Notice } from "../ui";

type Target = { id: string; name: string };
type Assignment = { provider: Provider; ids: string[]; target: Target };
const NEW_GROUP = "new";

/** 分组失败只重试成员分配，不重新导入凭证。 */
export function useBatchImportGrouping(providers: Provider[], busy: boolean,
  working: MutableRefObject<boolean>, onChanged: () => void) {
  const [groups, setGroups] = useState<CredentialGroup[] | undefined>();
  const [targets, setTargets] = useState<Partial<Record<Provider, Target>>>({});
  const [loading, setLoading] = useState(true);
  const [managing, setManaging] = useState(false);
  const [error, setError] = useState("");
  const [waiting, setWaiting] = useState<Assignment[]>([]);
  const [editing, setEditing] = useState<Provider | null>(null);
  const [editName, setEditName] = useState("");
  const [deleting, setDeleting] = useState<Provider | null>(null);
  const mounted = useRef(false);
  const generation = useRef(0);

  const reload = async (signal?: AbortSignal) => {
    const current = ++generation.current;
    setLoading(true);
    try {
      const result = await listCredentialGroups(signal);
      if (!result.is_operator) throw new ApiError(403, "forbidden", "当前账号无权管理分组");
      if (mounted.current && current === generation.current) { setGroups(result.groups); setError(""); }
      return result.groups;
    } catch (caught) {
      if (mounted.current && !signal?.aborted && current === generation.current) {
        setError(errorText(caught, "分组列表加载失败，请重试；仍可不分组导入。"));
      }
      return undefined;
    } finally {
      if (mounted.current && current === generation.current) setLoading(false);
    }
  };
  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void reload(controller.signal);
    return () => { mounted.current = false; generation.current += 1; controller.abort(); };
  }, []);

  const selectedGroup = (provider: Provider) => groups?.find(group =>
    group.id === targets[provider]?.id && group.provider === provider);
  const selectionIssue = providers.some(provider => {
    const target = targets[provider];
    return target?.id && (loading || !groups || (target.id === NEW_GROUP
      ? !target.name.trim() || groups.some(group => group.provider === provider && group.name === target.name.trim())
      : !Array.isArray(selectedGroup(provider)?.credential_ids)));
  }) ? "请填写不重复的分组名称，或重新选择有效分组。" : null;

  const manage = async (action: () => Promise<unknown>) => {
    if (working.current) return;
    working.current = true; setManaging(true); setError("");
    try { await action(); onChanged(); await reload(); }
    catch (caught) { setError(errorText(caught, "分组操作失败，请重试。")); }
    finally { working.current = false; setManaging(false); }
  };

  const apply = async (assignments: Assignment[]) => {
    const remaining: Assignment[] = [];
    const messages: string[] = [];
    for (const assignment of assignments) {
      try {
        let saved: CredentialGroup;
        if (assignment.target.id === NEW_GROUP) {
          saved = await createCredentialGroup({ provider: assignment.provider,
            name: assignment.target.name.trim(), credential_ids: assignment.ids });
          setTargets(previous => ({ ...previous, [assignment.provider]: { id: saved.id, name: saved.name } }));
        } else {
          // 使用最新成员和乐观锁，不能用导入前的列表覆盖别人刚添加的账号。
          const current = (await listCredentialGroups()).groups.find(group =>
            group.id === assignment.target.id && group.provider === assignment.provider);
          if (!current || !Array.isArray(current.credential_ids)) throw new ApiError(409, "group_unavailable", "分组已删除或无法读取成员");
          saved = await updateCredentialGroup(current.id, {
            credential_ids: [...new Set([...current.credential_ids, ...assignment.ids])],
            expected_updated_at: current.updated_at,
          });
        }
        setGroups(previous => [...(previous ?? []).filter(group => group.id !== saved.id), saved]);
        onChanged();
      } catch (caught) {
        remaining.push(assignment);
        messages.push(`${providerLabel(assignment.provider)}：${errorText(caught, "加入分组失败")}`);
      }
    }
    setWaiting(remaining);
    setError(messages.length ? `账号已导入，分组分配未完成。${messages.join("；")}` : "");
  };
  const assignImported = async (response: BatchImportResult, entries: BatchImportEntry[]) => {
    const assignments = providers.flatMap(provider => {
      const target = targets[provider];
      const ids = response.created.filter(item => entries[item.index]?.provider === provider).map(item => item.id);
      return target?.id && ids.length ? [{ provider, ids: [...new Set(ids)], target: { ...target } }] : [];
    });
    await apply(assignments);
  };
  const retryAssignments = async () => {
    if (working.current) return;
    working.current = true; setManaging(true);
    try { await apply(waiting); }
    finally { working.current = false; setManaging(false); }
  };

  const disabled = busy || managing || waiting.length > 0;
  const controls = <div className="space-y-2 rounded-md border border-border p-3" data-testid="batch-grouping">
    <p className="text-xs text-muted-foreground">导入到分组（可选）。不同渠道分别选择；保留已有成员和其他分组，失败项和跳过项不分配。</p>
    {providers.map(provider => {
      const target = targets[provider];
      const group = selectedGroup(provider);
      return <div key={provider} className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-xs">{providerLabel(provider)} 分组
            <select aria-label={`${providerLabel(provider)} 导入分组`} value={target?.id ?? ""}
              disabled={disabled || loading || !groups} data-testid={`batch-group-${provider}`}
              className="rounded border border-border bg-background px-2 py-1"
              onChange={event => {
                const id = event.target.value;
                setTargets(previous => ({ ...previous, [provider]: { id, name: "" } }));
                setEditing(null); setDeleting(null);
              }}>
              <option value="">不分配分组</option>
              {groups?.filter(item => item.provider === provider).map(item =>
                <option key={item.id} value={item.id}>{item.name}（{item.member_count} 个账号）</option>)}
              {target?.id && target.id !== NEW_GROUP && !group && <option value={target.id}>分组已删除，请重新选择</option>}
              <option value={NEW_GROUP}>新建分组</option>
            </select>
          </label>
          {group && <>
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => {
              setEditing(provider); setEditName(group.name); setDeleting(null);
            }}>修改名称</Button>
            <Button size="sm" variant="ghost" disabled={disabled} onClick={() => {
              setDeleting(provider); setEditing(null);
            }}>删除分组</Button>
          </>}
        </div>
        {target?.id === NEW_GROUP && <div className="flex items-center gap-2">
          <Input aria-label={`${providerLabel(provider)} 新分组名称`} value={target.name} maxLength={128}
            placeholder="分组名称（导入成功后创建）" disabled={disabled}
            onChange={event => setTargets(previous => ({ ...previous, [provider]: { ...target, name: event.target.value } }))} />
          <Button size="sm" disabled={disabled || !target.name.trim() || Boolean(selectionIssue)}
            onClick={() => void manage(async () => {
              const saved = await createCredentialGroup({ provider, name: target.name.trim(), credential_ids: [] });
              setTargets(previous => ({ ...previous, [provider]: { id: saved.id, name: saved.name } }));
            })}>立即创建</Button>
        </div>}
        {editing === provider && group && <div className="flex items-center gap-2">
          <Input aria-label={`${providerLabel(provider)} 分组名称`} value={editName} maxLength={128} disabled={disabled}
            onChange={event => setEditName(event.target.value)} />
          <Button size="sm" disabled={disabled || !editName.trim()} onClick={() => void manage(async () => {
            await updateCredentialGroup(group.id, { name: editName.trim(), expected_updated_at: group.updated_at });
            setEditing(null);
          })}>保存名称</Button>
          <Button size="sm" variant="ghost" disabled={disabled} onClick={() => setEditing(null)}>取消修改</Button>
        </div>}
        {deleting === provider && group && <div className="flex flex-wrap items-center gap-2 text-xs">
          <span>仅删除分组，账号保留；绑定此组的 API Key 需重新调整范围。</span>
          <Button size="sm" variant="danger" disabled={disabled} onClick={() => void manage(async () => {
            await deleteCredentialGroup(group.id);
            setTargets(previous => ({ ...previous, [provider]: { id: "", name: "" } })); setDeleting(null);
          })}>确认删除分组</Button>
          <Button size="sm" variant="ghost" disabled={disabled} onClick={() => setDeleting(null)}>取消删除</Button>
        </div>}
      </div>;
    })}
    {loading && <p className="text-xs text-muted-foreground">正在加载分组…</p>}
    {selectionIssue && <p className="text-xs text-warn-ink">{selectionIssue}</p>}
    {error && <div data-testid="batch-group-error"><Notice tone="warn">{error}</Notice></div>}
    <div className="flex flex-wrap gap-2">
      <Button size="sm" variant="ghost" disabled={busy || managing || loading} onClick={() => void reload()}>刷新分组</Button>
      {waiting.length > 0 && <>
        <Button size="sm" disabled={busy || managing} onClick={() => void retryAssignments()}>重试分组分配</Button>
        <Button size="sm" variant="ghost" disabled={busy || managing} onClick={() => { setWaiting([]); setError(""); }}>保留账号，放弃分组分配</Button>
      </>}
    </div>
  </div>;
  return { controls, selectionIssue, assignImported, managing, hasWaiting: waiting.length > 0 };
}
