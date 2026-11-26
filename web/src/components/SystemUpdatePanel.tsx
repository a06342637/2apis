import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { systemUpdates, type SystemUpdateStatus } from "../api/systemUpdates";
import { errorText } from "../lib/errors";
import { Button, Field, Input, Notice, Panel } from "../ui";

const KEY = ["system-updates"];
const LABELS = { running: "更新中", success: "更新成功", failed: "更新失败",
  rolled_back: "已回滚", recovery_failed: "回滚待处理" };

export function SystemUpdatePanel() {
  const client = useQueryClient();
  const [source, setSource] = useState("");
  const [force, setForce] = useState(false);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const mutation = useMutation({
    mutationFn: (work: () => Promise<SystemUpdateStatus>) => work(),
    onMutate: () => client.cancelQueries({ queryKey: KEY }),
    onSuccess: (data) => {
      if (mounted.current) client.setQueryData(KEY, data);
      // 页面切走后操作仍可能完成；读取当前主机状态，避免旧响应覆盖新页面。
      else void client.invalidateQueries({ queryKey: KEY });
    },
  });
  const query = useQuery({ queryKey: KEY,
    queryFn: ({ signal }) => systemUpdates.status(signal),
    enabled: !mutation.isPending,
    retry: 0,
    refetchInterval: (current) => current.state.data?.enabled === false ? false : 2000,
  });
  const state = query.data;
  useEffect(() => { setSource(state?.source ?? ""); }, [state?.source]);
  const job = state?.job;
  const busy = mutation.isPending || job?.status === "running" || job?.status === "recovery_failed";
  const latest = state?.latest;
  const canStart = latest?.ci === "success" && (state?.update_available || (state?.dirty && force));
  const run = (work: () => Promise<SystemUpdateStatus>) => mutation.mutate(work);

  return <Panel title="更新系统" data-testid="system-update-panel">
    <div className="space-y-4">
      {query.isPending && <p>正在读取更新状态…</p>}
      {query.isError && <Notice tone="warn">{state?.job?.status === "running"
        ? "服务正在切换，正在重新连接；更新进度由主机保存。"
        : errorText(query.error, "读取更新状态失败，正在重新连接。")}</Notice>}
      {mutation.isError && <Notice tone="danger">{errorText(mutation.error, "更新操作失败，请重试。")}</Notice>}
      {state?.enabled === false && <p>此部署尚未启用主机更新助手。</p>}
      {state?.enabled && <>
        <div className="flex flex-wrap items-end gap-3">
          <Field label="GitHub 更新仓库" htmlFor="system-update-source">
            <Input id="system-update-source" value={source} disabled={busy}
              placeholder="a06342637/2apis" onChange={(event) => setSource(event.target.value)} />
          </Field>
          <Button disabled={busy || !source.trim() || source.trim() === state.source}
            onClick={() => run(() => systemUpdates.source(source.trim()))}>保存仓库</Button>
          <Button disabled={busy} onClick={() => run(systemUpdates.check)}>检查更新</Button>
        </div>
        <p className="text-sm">当前版本 {state.current?.version ?? "未知"}
          {" · "}<code>{state.current?.commit.slice(0, 12) || "提交未知"}</code></p>
        {latest && <p className="text-sm">main 最新版本 {latest.version}{" · "}
          <code>{latest.commit.slice(0, 12)}</code>{" · "}
          {latest.ci === "success" ? "CI 全部通过" : latest.ci === "pending" ? "等待 CI 通过" : "CI 未通过"}
          {state.update_available ? " · 有可用更新" : " · 已是最新提交"}</p>}
        {state.dirty && <Notice tone="warn">检测到本地源码修改，更新前会保存备份。</Notice>}
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={force} disabled={busy}
            onChange={(event) => setForce(event.target.checked)} />覆盖本地源码修改
        </label>
        <p className="text-xs text-muted-foreground">构建期间服务继续运行，切换时会短暂中断。
          原账号、API Key 和部署配置保留，失败自动回滚；覆盖源码也必须通过 CI。</p>
        <Button variant="primary" disabled={busy || !canStart || (state.dirty && !force)}
          onClick={() => { if (latest) run(() => systemUpdates.start(latest.commit, force)); }}>
          {job?.status === "running" ? "更新进行中…" : "更新并重启"}</Button>
      </>}
      {job && <div className="space-y-2" aria-live="polite">
        <p>{LABELS[job.status]} · {job.progress}%</p>
        <progress aria-label="更新进度" value={job.progress} max={100} className="w-full" />
        {job.error && <Notice tone="danger">{job.error}</Notice>}
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap text-xs" aria-label="更新日志">
          {job.logs.map((log) => `${new Date(log.time * 1000).toLocaleTimeString()} ${log.message}`).join("\n")}
        </pre>
      </div>}
    </div>
  </Panel>;
}
