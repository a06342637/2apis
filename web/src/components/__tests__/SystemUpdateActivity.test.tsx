import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UpdateJob } from "../../api/systemUpdates";
import { SystemUpdateActivity } from "../SystemUpdateActivity";

function job(values: Partial<UpdateJob> = {}): UpdateJob {
  return { id: "one", status: "running", phase: "building", progress: 25, error: "",
    started_at: 100, finished_at: null, step: "正在构建镜像", step_started_at: 150,
    heartbeat_at: 198, logs: [{ time: 198, message: "等待当前步骤完成" }], ...values };
}
const props = { serverTime: 200, syncedAt: 10000, reconnecting: false };
describe("SystemUpdateActivity", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(10000); });
  afterEach(() => { cleanup(); vi.useRealTimers(); });
  it("主机和浏览器时间不同也正确递增耗时，并明确百分比是阶段进度", () => {
    render(<SystemUpdateActivity job={job()} {...props} />);
    expect(screen.getByTestId("update-elapsed")).toHaveTextContent("已运行 1 分 40 秒");
    expect(screen.getByTestId("update-step-elapsed")).toHaveTextContent("50 秒");
    expect(screen.getByTestId("update-heartbeat")).toHaveTextContent("2 秒前");
    expect(screen.getByText(/百分比表示更新阶段/)).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(5000));
    expect(screen.getByTestId("update-step-elapsed")).toHaveTextContent("55 秒");
    expect(screen.getByTestId("update-heartbeat")).toHaveTextContent("7 秒前");
    expect(screen.getByText("主机状态 5 秒前已同步")).toBeInTheDocument();
    expect(screen.getByLabelText("更新日志")).toHaveTextContent("08:03:18");
  });
  it("长时间没有活动报告时提示等待，不把同步成功当构建已推进", () => {
    render(<SystemUpdateActivity job={job({ heartbeat_at: 100 })} {...props} />);
    expect(screen.getByTestId("update-heartbeat")).toHaveTextContent("请勿重复启动");
    expect(screen.getByTestId("update-heartbeat")).toHaveClass("text-warn-ink");
  });
  it("兼容旧助手缺少活动字段和空日志，网络断开显示重连", () => {
    render(<SystemUpdateActivity job={job({ step: null, heartbeat_at: null,
      step_started_at: null, logs: [] })} {...props} reconnecting />);
    expect(screen.getByTestId("update-current-step")).toHaveTextContent("获取源码并构建镜像");
    expect(screen.getByLabelText("更新日志")).toHaveTextContent("等待主机记录第一条日志");
    expect(screen.getByText("正在重新连接主机")).toBeInTheDocument();
    expect(screen.getByTestId("update-heartbeat")).toHaveTextContent("等待步骤日志");
  });
  it("任务结束后停止计时，以主机完成时间固定总耗时", () => {
    render(<SystemUpdateActivity job={job({ status: "success", phase: "done", finished_at: 160 })} {...props} />);
    const before = screen.getByTestId("update-elapsed").textContent;
    act(() => vi.advanceTimersByTime(30000));
    expect(screen.getByTestId("update-elapsed").textContent).toBe(before);
    expect(screen.queryByTestId("update-step-elapsed")).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
  });
  it("新增日志跟随底部，手动查看旧日志时保持位置，可一键恢复跟随", () => {
    const view = render(<SystemUpdateActivity job={job()} {...props} />);
    const log = screen.getByLabelText("更新日志");
    Object.defineProperties(log, { scrollHeight: { configurable: true, value: 600 },
      clientHeight: { configurable: true, value: 100 }, scrollTop: { configurable: true, writable: true, value: 0 } });
    fireEvent.scroll(log);
    view.rerender(<SystemUpdateActivity job={job({ logs: [{ time: 200, message: "新的活动报告" }] })} {...props} />);
    expect(log.scrollTop).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "查看最新日志" }));
    expect(log.scrollTop).toBe(600);
    expect(screen.queryByRole("button", { name: "查看最新日志" })).not.toBeInTheDocument();
  });
});
