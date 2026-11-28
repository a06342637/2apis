import { useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { CloudUpload } from "lucide-react";
import { adminKey } from "../api/hooks";
import { DESTINATIONS, remoteBackups, type Destination, type RemoteConfig } from "../api/remoteBackups";
import { useSessionContext } from "../Layout";
import { errorText } from "../lib/errors";
import { Button, Card, Field, Input, Notice, Select } from "../ui";

const targets = Object.keys(DESTINATIONS) as Destination[];

export function useRemoteBackups() {
  const { username } = useSessionContext();
  const key = adminKey(username, "remote-backups");
  const client = useQueryClient();
  const query = useQuery({ queryKey: key, queryFn: remoteBackups.status,
    retry: false, refetchInterval: query => query.state.data?.running ? 2000 : 15000 });
  return { ...query, key, client };
}

export function RemoteBackupPanel() {
  const query = useRemoteBackups();
  const [draft, setDraft] = useState<RemoteConfig | null>(null);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string }>();
  const status = query.data;
  if (!status) return query.isError
    ? <Notice tone="danger">无法读取远端备份配置。 <Button size="sm" onClick={() => void query.refetch()}>重试</Button></Notice>
    : <p role="status">正在读取远端备份配置…</p>;
  const config = draft ?? status.config;
  const disabled = busy || status.running;
  const anyTarget = targets.some(target => status.config[target].enabled);
  const change = (values: Partial<RemoteConfig>) => setDraft(previous => ({ ...(previous ?? config), ...values }));
  const targetChange = <T extends Destination>(target: T, values: Partial<RemoteConfig[T]>) => {
    setDraft(previous => {
      const base = previous ?? config;
      return { ...base, [target]: { ...base[target], ...values } };
    });
  };
  const run = async (operation: () => Promise<unknown>, text: string) => {
    if (working.current) return;
    working.current = true; setBusy(true); setFeedback(undefined);
    try {
      await operation(); setFeedback({ ok: true, text });
    } catch (caught) {
      setFeedback({ ok: false, text: errorText(caught, "远端备份操作失败，请重试。") });
    } finally { working.current = false; setBusy(false); }
  };
  const fmt = (value: number | null) => value === null ? "—" : new Intl.DateTimeFormat("zh-CN", {
    timeZone: status.config.timezone, dateStyle: "medium", timeStyle: "short",
  }).format(value * 1000);
  function text<T extends Destination, K extends keyof RemoteConfig[T] & string>(
    target: T, key: K, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {},
  ) {
    const id = `remote-${target}-${key}`;
    return <Field label={label} htmlFor={id}>
      <Input id={id} value={String(config[target][key] ?? "")} {...props}
        onChange={event => targetChange(target, {
          [key]: props.type === "number" ? Number(event.target.value) : event.target.value,
        } as Partial<RemoteConfig[T]>)} />
    </Field>;
  }
  return <div className="space-y-4" data-testid="remote-backup-panel">
    <Card className="gap-3 p-4">
      <h2 className="font-semibold">远端自动备份</h2>
      <p className="text-sm text-muted-foreground">定时将通过完整性校验的 SQLite 快照上传到私有 OSS 或 SFTP。换机恢复凭证须另外保管原部署的 APP_SECRET。</p>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={config.enabled} disabled={disabled}
        onChange={event => change({ enabled: event.target.checked })} /> 启用远端自动备份</label>
      <div className="flex flex-wrap items-center justify-between gap-3 text-sm">
        <div>下次备份：{fmt(status.next_run)}　上次备份：{fmt(status.last_run?.finished_at ?? null)}</div>
        <Button size="sm" disabled={disabled || !anyTarget || !!draft} onClick={() => void run(async () => {
          await remoteBackups.run();
          query.client.setQueryData(query.key, { ...status, running: true });
          await query.refetch();
        }, "已开始远端备份，可在这里或服务端日志查看进度。")}><CloudUpload className="size-4" />{status.running ? "正在备份…" : "立即远端备份"}</Button>
      </div>
      {status.last_run && <div className="space-y-1 text-xs" data-testid="remote-last-run">
        <p>{status.last_run.scheduled ? "定时" : "手动"}备份 · {status.last_run.name}</p>
        {status.last_run.error && <Notice tone="danger">{status.last_run.error}</Notice>}
        {status.last_run.targets?.map(target => <p key={target.target}>{DESTINATIONS[target.target]}：{target.message}
          {target.removed !== undefined && `，清理 ${target.removed} 份旧备份`}</p>)}
      </div>}
      <Link to="/logs?category=backup" className="w-fit text-sm text-primary underline">查看备份日志</Link>
    </Card>
    <fieldset className="space-y-4" disabled={disabled}>
      <Card className="gap-3 p-4">
        <h3 className="text-sm font-semibold">备份计划</h3>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="每隔几天" htmlFor="remote-days"><Input id="remote-days" type="number" min={1} max={365}
            value={config.days} onChange={e => change({ days: Number(e.target.value) })} /></Field>
          <Field label="执行时刻" htmlFor="remote-time"><Input id="remote-time" type="time" value={config.time}
            onChange={e => change({ time: e.target.value })} /></Field>
          <Field label="计划时区" htmlFor="remote-zone"><Input id="remote-zone" value={config.timezone} placeholder="Asia/Shanghai"
            onChange={e => change({ timezone: e.target.value })} /></Field>
        </div>
        <p className="text-xs text-muted-foreground">停机错过的计划在恢复后补跑最近一次；手动备份不改变定时计划。</p>
      </Card>
      {targets.map(target => <Card key={target} className="gap-3 p-4">
        <label className="flex items-center gap-2 text-sm font-semibold"><input type="checkbox" checked={config[target].enabled}
          onChange={event => targetChange(target, { enabled: event.target.checked })} />{DESTINATIONS[target]}</label>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {target === "oss" ? <>
            {text("oss", "region", "地域")}{text("oss", "bucket", "Bucket 名称")}
            {text("oss", "key_id", "AccessKey ID", { autoComplete: "off" })}
            {text("oss", "key_secret", "AccessKey Secret", { type: "password", autoComplete: "new-password", placeholder: config.oss.has_key_secret ? "已保存，留空不修改" : "未设置" })}
            {text("oss", "endpoint", "自定义 OSS Endpoint", { placeholder: "oss-cn-hongkong.aliyuncs.com" })}
            <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={config.oss.internal}
              onChange={event => targetChange("oss", { internal: event.target.checked })} /> 使用内网 Endpoint</label>
          </> : <>
            {text("sftp", "host", "服务器 IP 或域名")}{text("sftp", "port", "SSH 端口", { type: "number", min: 1, max: 65535 })}
            {text("sftp", "username", "SFTP 用户名", { autoComplete: "off" })}
            <Field label="登录方式" htmlFor="remote-auth"><Select id="remote-auth" value={config.sftp.auth}
              onChange={event => targetChange("sftp", { auth: event.target.value })}>
              <option value="password">密码</option><option value="key">私钥</option>
            </Select></Field>
            {config.sftp.auth === "password" ? text("sftp", "password", "SFTP 密码", { type: "password", autoComplete: "new-password", placeholder: config.sftp.has_password ? "已保存，留空不修改" : "未设置" }) : <>
              <Field label="SFTP 私钥" htmlFor="remote-sftp-private-key"><textarea id="remote-sftp-private-key" rows={3}
                className="w-full rounded-md border border-border bg-background p-2 font-mono text-xs"
                autoComplete="off" placeholder={config.sftp.has_private_key ? "已保存，留空不修改" : "粘贴私钥"}
                value={config.sftp.private_key ?? ""} onChange={event => targetChange("sftp", { private_key: event.target.value })} /></Field>
              {text("sftp", "passphrase", "私钥口令", { type: "password", autoComplete: "new-password", placeholder: config.sftp.has_passphrase ? "已保存，留空不修改" : "可选" })}
            </>}
            {text("sftp", "fingerprint", "主机公钥 SHA256 指纹", { placeholder: "SHA256:…" })}
          </>}
          {text(target, "directory", "保存目录", { placeholder: target === "oss" ? "留空使用 Bucket 根目录" : "留空使用用户目录" })}
          {text(target, "keep", "保留份数", { type: "number", min: 1, max: 100 })}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" variant="default" disabled={!!draft || !status.config[target].enabled}
            onClick={() => void run(() => remoteBackups.test(target), `${DESTINATIONS[target]} 连接成功。`)}>测试{DESTINATIONS[target]}连接</Button>
          <Link to={`/backups?remote=${target}`} className="text-sm text-primary underline">浏览远端备份</Link>
        </div>
      </Card>)}
    </fieldset>
    {feedback && <Notice tone={feedback.ok ? "ok" : "danger"}>{feedback.text}</Notice>}
    <div className="flex items-center gap-3">
      <Button variant="primary" disabled={disabled || !draft} onClick={() => void run(async () => {
        const result = await remoteBackups.save(config);
        query.client.setQueryData(query.key, result); setDraft(null);
      }, "远端备份设置已保存。")}>保存远端备份设置</Button>
      <Button variant="ghost" disabled={disabled || !draft} onClick={() => setDraft(null)}>放弃修改</Button>
      {draft && <span className="text-xs text-muted-foreground">保存后可测试连接或开始备份。</span>}
    </div>
  </div>;
}
