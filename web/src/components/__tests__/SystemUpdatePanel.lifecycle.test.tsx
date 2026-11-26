import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";
import { systemUpdates, type SystemUpdateStatus } from "../../api/systemUpdates";
import { SystemUpdatePanel } from "../SystemUpdatePanel";

function status(source: string): SystemUpdateStatus {
  return { enabled: true, source, dirty: false,
    current: { version: "0.1.2", commit: "a".repeat(40) }, latest: null,
    update_available: false, job: null };
}

describe("SystemUpdatePanel 页面生命周期", () => {
  afterEach(() => vi.restoreAllMocks());

  it("退出页面后返回的新状态不能被旧检查请求的迟到结果覆盖", async () => {
    vi.spyOn(systemUpdates, "status").mockResolvedValue(status("first/repo"));
    let finishCheck!: (value: SystemUpdateStatus) => void;
    vi.spyOn(systemUpdates, "check").mockImplementation(() => new Promise((resolve) => { finishCheck = resolve; }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = render(<QueryClientProvider client={client}><SystemUpdatePanel /></QueryClientProvider>);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("GitHub 更新仓库")).toHaveValue("first/repo"));
    await user.click(screen.getByRole("button", { name: "检查更新" }));
    view.rerender(<QueryClientProvider client={client}>{null}</QueryClientProvider>);
    client.clear();
    vi.mocked(systemUpdates.status).mockResolvedValue(status("current/repo"));
    view.rerender(<QueryClientProvider client={client}><SystemUpdatePanel /></QueryClientProvider>);
    await waitFor(() => expect(screen.getByLabelText("GitHub 更新仓库")).toHaveValue("current/repo"));
    vi.mocked(systemUpdates.status).mockResolvedValue(status("fresh/repo"));
    await act(async () => { finishCheck(status("stale/repo")); });
    await waitFor(() => expect(screen.getByLabelText("GitHub 更新仓库")).toHaveValue("fresh/repo"));
    expect(client.getQueryData<SystemUpdateStatus>(["system-updates"])?.source).toBe("fresh/repo");
    client.clear();
  });

  it("离开后成功的操作使缓存失效，下次打开重新读取已完成的服务端状态", async () => {
    vi.spyOn(systemUpdates, "status").mockResolvedValue(status("first/repo"));
    let finishCheck!: (value: SystemUpdateStatus) => void;
    vi.spyOn(systemUpdates, "check").mockImplementation(() => new Promise((resolve) => { finishCheck = resolve; }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const view = render(<QueryClientProvider client={client}><SystemUpdatePanel /></QueryClientProvider>);
    const user = userEvent.setup();
    await waitFor(() => expect(screen.getByLabelText("GitHub 更新仓库")).toHaveValue("first/repo"));
    await user.click(screen.getByRole("button", { name: "检查更新" }));
    view.rerender(<QueryClientProvider client={client}>{null}</QueryClientProvider>);
    vi.mocked(systemUpdates.status).mockResolvedValue(status("current/repo"));
    await act(async () => { finishCheck(status("stale/repo")); });
    view.rerender(<QueryClientProvider client={client}><SystemUpdatePanel /></QueryClientProvider>);
    await waitFor(() => expect(systemUpdates.status).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.getByLabelText("GitHub 更新仓库")).toHaveValue("current/repo"));
    expect(screen.getByLabelText("GitHub 更新仓库")).toHaveValue("current/repo");
    expect(client.getQueryData<SystemUpdateStatus>(["system-updates"])?.source).toBe("current/repo");
    client.clear();
  });
});
