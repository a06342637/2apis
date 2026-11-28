import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { remoteBackups, DESTINATIONS, type Destination } from "../api/remoteBackups";
import { formatTime } from "../api/display";
import { errorText } from "../lib/errors";
import { useDialogFocus } from "../hooks/useDialogFocus";
import { Button, Notice, Panel, Select, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui";
import { useRemoteBackups } from "./RemoteBackupPanel";

export function RemoteBackupFiles({ initial, onRestored, formatBytes }: {
  initial?: Destination; onRestored: () => Promise<unknown>; formatBytes: (size: number) => string;
}) {
  const status = useRemoteBackups();
  const [target, setTarget] = useState<Destination | "">(initial ?? "");
  const [confirm, setConfirm] = useState<{ action: "restore" | "delete"; name: string; target: Destination } | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string }>();
  const [busy, setBusy] = useState(false);
  const dialog = useDialogFocus<HTMLDivElement>(busy ? undefined : () => setConfirm(null), !!confirm);
  const selected = target && status.data?.config[target].enabled ? target : "";
  const run = useQuery({ queryKey: [...status.key, "files", selected],
    queryFn: () => remoteBackups.files(selected as Destination), enabled: !!selected, retry: false });
  const perform = async () => {
    if (!confirm || busy) return;
    setBusy(true); setMessage(undefined);
    try {
      if (confirm.action === "restore") {
        await remoteBackups.restore(confirm.target, confirm.name);
        setMessage({ ok: true, text: "已校验并暂存远端备份，重启服务后恢复。" });
        void onRestored();
      } else {
        await remoteBackups.delete(confirm.target, confirm.name);
        setMessage({ ok: true, text: "已删除远端备份。" });
        void run.refetch();
      }
      setConfirm(null);
    } catch (error) {
      setMessage({ ok: false, text: errorText(error, "远端备份操作失败，请重试。") });
      setConfirm(null);
    } finally { setBusy(false); }
  };
  return <Panel title="远端备份文件" action={<Link to="/settings?tab=remote-backup" className="text-sm text-primary underline">远端备份设置</Link>}>
    <div className="space-y-3" data-testid="remote-backup-files">
      <div className="flex flex-wrap items-center gap-2">
        <Select className="w-auto" aria-label="远端备份目标" value={selected} disabled={busy}
          onChange={e => { setTarget(e.target.value as Destination | ""); setMessage(undefined); }}>
          <option value="">选择远端目标</option>
          {(["oss", "sftp"] as const).filter(name => status.data?.config[name].enabled).map(name =>
            <option key={name} value={name}>{DESTINATIONS[name]}</option>)}
        </Select>
        <Button size="sm" disabled={!selected || busy || run.isFetching} onClick={() => void run.refetch()}>刷新远端列表</Button>
      </div>
      {status.isError && <Notice tone="danger">无法读取远端备份设置。</Notice>}
      {!selected && !status.isError && <p className="text-sm text-muted-foreground">启用远端目标后，可在这里查看、恢复或删除本实例的备份。</p>}
      {selected && run.isPending && <p role="status">正在读取远端备份文件…</p>}
      {selected && run.isError && <Notice tone="danger">{errorText(run.error, "远端列表读取失败，请重试。")}</Notice>}
      {selected && run.data && (run.data.backups.length ? <Table>
        <TableHeader><TableRow><TableHead>文件名</TableHead><TableHead>大小</TableHead><TableHead>时间</TableHead><TableHead>操作</TableHead></TableRow></TableHeader>
        <TableBody>{run.data.backups.map(row => <TableRow key={row.name}>
          <TableCell className="break-all font-mono text-xs">{row.name}</TableCell>
          <TableCell>{formatBytes(row.size)}</TableCell><TableCell>{formatTime(row.created_at)}</TableCell>
          <TableCell><div className="flex flex-wrap items-center gap-1">
            <a href={remoteBackups.downloadUrl(selected, row.name)} download className="px-2 text-xs underline">下载</a>
            <Button size="sm" variant="ghost" disabled={busy || status.data?.running} onClick={() => setConfirm({ action: "restore", name: row.name, target: selected })}>恢复远端备份</Button>
            <Button size="sm" variant="ghost" disabled={busy || status.data?.running} onClick={() => setConfirm({ action: "delete", name: row.name, target: selected })}>删除远端备份</Button>
          </div></TableCell>
        </TableRow>)}</TableBody>
      </Table> : <p className="text-sm text-muted-foreground">该目标还没有本实例的备份。</p>)}
      {message && <Notice tone={message.ok ? "ok" : "danger"}>{message.text}</Notice>}
      {confirm && <div className="fixed inset-0 z-50 grid place-items-center bg-black/50 p-4">
        <div ref={dialog} role="dialog" aria-modal="true" aria-label="确认远端备份操作" className="w-full max-w-md space-y-3 rounded-xl border bg-card p-5 shadow-xl">
          <p>{confirm.action === "restore" ? "下载并校验后暂存，重启时恢复：" : "从远端永久删除："}<br /><code className="break-all">{confirm.name}</code></p>
          <div className="flex justify-end gap-2"><Button disabled={busy} variant="ghost" onClick={() => setConfirm(null)}>取消</Button>
            <Button disabled={busy} variant={confirm.action === "restore" ? "primary" : "danger"} onClick={() => void perform()}>{busy ? "处理中…" : "确认"}</Button></div>
        </div>
      </div>}
    </div>
  </Panel>;
}
