import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../../api/client";
import { systemUpdates, type SystemUpdateStatus } from "../../api/systemUpdates";
import { SystemUpdatePanel } from "../SystemUpdatePanel";

const SHA = "b".repeat(40);
function status(values: Partial<SystemUpdateStatus> = {}): SystemUpdateStatus {
  return { enabled: true, source: "a06342637/2apis", branch: "main", dirty: false,
    current: { version: "0.1.0", commit: "a".repeat(40) },
    latest: { version: "0.1.0", commit: SHA, ci: "success" },
    update_available: true, job: null, ...values };
}
function job(state: "running" | "success" | "recovery_failed" = "running") {
  return { id: "job", status: state, phase: "building", progress: 10,
    logs: [{ time: 1, message: "旧服务继续运行" }], error: "", started_at: 1, finished_at: null };
}
function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const view = render(<QueryClientProvider client={client}><SystemUpdatePanel /></QueryClientProvider>);
  return { ...view, client, user: userEvent.setup({ advanceTimers: vi.advanceTimersByTime }) };
}

describe("SystemUpdatePanel", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.spyOn(systemUpdates, "status").mockResolvedValue(status());
    vi.spyOn(systemUpdates, "check").mockResolvedValue(status());
    vi.spyOn(systemUpdates, "source").mockResolvedValue(status({ source: "other/repo", latest: null }));
    vi.spyOn(systemUpdates, "start").mockImplementation(async () => {
      const running = status({ job: job() });
      vi.mocked(systemUpdates.status).mockResolvedValue(running);
      return running;
    });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

  it("同版本不同commit可更新，提交完整SHA与force，不重复启动", async () => {
    const { user } = setup();
    const button = await screen.findByRole("button", { name: "更新并重启" });
    expect(screen.getByText(/有可用更新/)).toBeInTheDocument();
    await user.click(button);
    expect(systemUpdates.start).toHaveBeenCalledWith(SHA, false);
    expect(await screen.findByRole("button", { name: "更新进行中…" })).toBeDisabled();
    expect(screen.getByRole("progressbar", { name: "更新进度" })).toHaveAttribute("value", "10");
    expect(screen.getByLabelText("更新日志")).toHaveTextContent("旧服务继续运行");
  });

  it.each(["pending", "failed"] as const)("CI %s 时force也不能更新", async (ci) => {
    vi.mocked(systemUpdates.status).mockResolvedValue(status({ latest: { version: "0.1.0", commit: SHA, ci } }));
    const { user } = setup();
    await screen.findByRole("button", { name: "更新并重启" });
    await user.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "更新并重启" })).toBeDisabled();
  });

  it("本地修改须勾选覆盖，随后传force", async () => {
    vi.mocked(systemUpdates.status).mockResolvedValue(status({ dirty: true }));
    const { user } = setup();
    const button = await screen.findByRole("button", { name: "更新并重启" });
    expect(button).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    await user.click(button);
    expect(systemUpdates.start).toHaveBeenCalledWith(SHA, true);
  });

  it("保存合法仓库和检查更新", async () => {
    const { user } = setup();
    const input = await screen.findByLabelText("GitHub 更新仓库");
    await waitFor(() => expect(input).toHaveValue("a06342637/2apis"));
    await user.clear(input);
    await user.type(input, "other/repo");
    await user.click(screen.getByRole("button", { name: "保存仓库" }));
    expect(systemUpdates.source).toHaveBeenCalledWith("other/repo");
    await user.click(screen.getByRole("button", { name: "检查更新" }));
    expect(systemUpdates.check).toHaveBeenCalledOnce();
  });

  it("刷新恢复持久化任务，服务切换失败后轮询重连", async () => {
    vi.mocked(systemUpdates.status).mockResolvedValueOnce(status({ job: job() }))
      .mockRejectedValueOnce(new ApiError(502, "down", "down"))
      .mockResolvedValue(status({ job: { ...job("success"), progress: 100 } }));
    setup();
    await screen.findByText("更新中 · 10%");
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(await screen.findByText(/暂时无法连接主机/)).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(await screen.findByText("更新成功 · 100%")).toBeInTheDocument();
  });

  it("禁用部署不显示更新控件，回滚失败锁定后续操作", async () => {
    vi.mocked(systemUpdates.status).mockResolvedValue({ enabled: false });
    const view = setup();
    expect(await screen.findByText(/尚未启用/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "更新并重启" })).not.toBeInTheDocument();
    view.unmount();
    vi.mocked(systemUpdates.status).mockResolvedValue(status({ job: { ...job("recovery_failed"), error: "需要修复" } }));
    setup();
    expect(await screen.findByText("需要修复")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查更新" })).toBeDisabled();
  });

  it("显示受控操作错误，已是最新提交时不可启动", async () => {
    vi.mocked(systemUpdates.status).mockResolvedValue(status({ update_available: false }));
    vi.mocked(systemUpdates.check).mockRejectedValue(new ApiError(400, "bad", "目标 CI 未通过"));
    const { user } = setup();
    await screen.findByRole("button", { name: "更新并重启" });
    expect(screen.getByRole("button", { name: "更新并重启" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "检查更新" }));
    expect(await screen.findByText("目标 CI 未通过")).toBeInTheDocument();
  });
});
