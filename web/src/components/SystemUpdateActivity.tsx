import { LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { UpdateJob } from "../api/systemUpdates";
import { Button } from "../ui";

const PHASES: Record<string, string> = {
  queued: "准备更新", building: "获取源码并构建镜像", stopping: "排空在途请求",
  backup: "备份并校验数据", switching: "切换版本", starting: "启动与健康检查",
  rollback: "恢复原版本", done: "任务结束",
};

function duration(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  if (total >= 3600) return `${Math.floor(total / 3600)} 小时 ${Math.floor(total % 3600 / 60)} 分 ${total % 60} 秒`;
  if (total >= 60) return `${Math.floor(total / 60)} 分 ${total % 60} 秒`;
  return `${total} 秒`;
}

export function SystemUpdateActivity({ job, serverTime, syncedAt, reconnecting }: {
  job: UpdateJob; serverTime?: number; syncedAt: number; reconnecting: boolean;
}) {
  const [now, setNow] = useState(Date.now);
  const [following, setFollowing] = useState(true);
  const logRef = useRef<HTMLPreElement>(null);
  const running = job.status === "running";
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running, job.id]);
  useEffect(() => { setFollowing(true); }, [job.id]);
  const last = job.logs.at(-1);
  useEffect(() => {
    if (following && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [following, job.id, job.logs.length, last?.time, last?.message]);
  // 使用主机时间 + 本次同步后的浏览器耗时，避免两端时钟差导致虚假的停滞提示。
  const syncAge = Math.max(0, Math.floor((now - syncedAt) / 1000));
  const clock = serverTime === undefined ? Math.floor(now / 1000) : serverTime + syncAge;
  const end = job.finished_at ?? clock;
  const elapsed = duration(end - job.started_at);
  const stepElapsed = duration(clock - (job.step_started_at ?? job.started_at));
  const heartbeatAge = job.heartbeat_at === undefined || job.heartbeat_at === null
    ? null : Math.max(0, clock - job.heartbeat_at);
  return <div className="space-y-2" data-testid="system-update-activity">
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground" aria-live="polite">
      {running && <LoaderCircle aria-hidden="true" className="size-3.5 animate-spin" />}
      <span data-testid="update-elapsed">{running ? "已运行" : "总耗时"} {elapsed}</span>
      {running && <span data-testid="update-step-elapsed">当前步骤已持续 {stepElapsed}</span>}
      <span>{reconnecting ? "正在重新连接主机" : `主机状态 ${syncAge} 秒前已同步`}</span>
    </div>
    <p className="text-sm" data-testid="update-current-step">当前步骤：{job.step || PHASES[job.phase] || "更新任务"}</p>
    {running && job.phase === "building" && <p className="text-xs text-muted-foreground">
      镜像构建可能需要数分钟，旧服务继续运行。百分比表示更新阶段；当前步骤完成后才会推进。
    </p>}
    {running && <p className={`text-xs ${heartbeatAge !== null && heartbeatAge > 45 ? "text-warn-ink" : "text-muted-foreground"}`}
      data-testid="update-heartbeat">
      {heartbeatAge === null ? "正在等待步骤日志；主机会持续保存更新状态。"
        : `最近活动报告：${heartbeatAge} 秒前${heartbeatAge > 45 ? "，正在等待新的报告，请勿重复启动更新。" : "；长步骤每 15 秒报告一次等待状态。"}`}
    </p>}
    <div className="flex items-center justify-between gap-2 text-xs">
      <span>活动日志（{job.logs.length} 条）</span>
      {!following && <Button size="sm" variant="ghost" onClick={() => setFollowing(true)}>查看最新日志</Button>}
    </div>
    <pre ref={logRef} className="max-h-72 overflow-auto whitespace-pre-wrap rounded border border-border bg-muted/30 p-3 text-xs"
      aria-label="更新日志" tabIndex={0} onScroll={(event) => {
        const element = event.currentTarget;
        setFollowing(element.scrollHeight - element.clientHeight - element.scrollTop < 24);
      }}>
      {job.logs.length ? job.logs.map(log => `${new Date(log.time * 1000).toLocaleTimeString("zh-CN", {
        timeZone: "Asia/Shanghai", hour12: false,
      })} ${log.message}`).join("\n") : "等待主机记录第一条日志…"}
    </pre>
  </div>;
}
