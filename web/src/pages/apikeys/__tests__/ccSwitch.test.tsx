import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "@/test-utils";
import { CcSwitchImportButtons } from "../ccSwitch";

const catalogue = (id: string) => ({ data: [{
  id, object: "model", owned_by: "2apis", providers: ["miora"],
}] });
const modelInLink = () => new URLSearchParams(screen.getByTestId("ccswitch-claude")
  .getAttribute("href")!.split("?")[1]).get("model");

describe("CC Switch Key 模型范围", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("空 Key 不请求模型，且导入按钮禁用", () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    render(<CcSwitchImportButtons apiKey="" />);
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(screen.getByTestId("ccswitch-claude")).toBeDisabled();
    expect(screen.getAllByRole("option")).toHaveLength(1);
  });

  it("仅展示此 Key 返回的模型，并将选择写入深链", async () => {
    const fetchSpy = vi.fn(async () => jsonResponse(catalogue("miora-pro")));
    vi.stubGlobal("fetch", fetchSpy);
    render(<CcSwitchImportButtons apiKey="sk-key-a" />);
    await screen.findByRole("option", { name: "miora-pro" });
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith("/v1/models", expect.objectContaining({
      headers: expect.objectContaining({ Authorization: "Bearer sk-key-a" }),
    }));
    expect(screen.getAllByRole("option")).toHaveLength(2);
    await userEvent.selectOptions(screen.getByTestId("ccswitch-model"), "miora-pro@miora");
    expect(modelInLink()).toBe("miora-pro@miora");
  });

  it("读取失败时显示受控文案，仍可留空模型导入", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      error: { code: "unavailable", message: "绑定账号当前不可用" },
    }, 503)));
    render(<CcSwitchImportButtons apiKey="sk-key-a" />);
    expect(await screen.findByText(/绑定账号当前不可用/)).toBeInTheDocument();
    expect(screen.getByTestId("ccswitch-claude")).toHaveAttribute("href");
    expect(modelInLink()).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(1);
  });

  it("Key 目录中的媒体模型不能成为聊天客户端的默认模型", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ data: [
      ...catalogue("miora-pro").data,
      { id: "gem-3.1", object: "model", owned_by: "2apis", providers: ["miora"],
        gateway_endpoints: ["/v1/images/generations"], output_modalities: ["image"] },
    ] })));
    render(<CcSwitchImportButtons apiKey="sk-key-a" />);
    await screen.findByRole("option", { name: "miora-pro" });
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.queryByRole("option", { name: "gem-3.1" })).not.toBeInTheDocument();
  });

  it("清空后重新粘贴同一 Key 不复活之前的模型选择", async () => {
    const fetchSpy = vi.fn(async () => jsonResponse(catalogue("miora-pro")));
    vi.stubGlobal("fetch", fetchSpy);
    const view = render(<CcSwitchImportButtons apiKey="sk-key-a" />);
    await screen.findByRole("option", { name: "miora-pro" });
    await userEvent.selectOptions(screen.getByTestId("ccswitch-model"), "miora-pro@miora");
    view.rerender(<CcSwitchImportButtons apiKey="" />);
    expect(screen.getAllByRole("option")).toHaveLength(1);
    view.rerender(<CcSwitchImportButtons apiKey="sk-key-a" />);
    expect(modelInLink()).toBeNull();
    expect(screen.getAllByRole("option")).toHaveLength(1);
    await screen.findByRole("option", { name: "miora-pro" });
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    expect(modelInLink()).toBeNull();
  });

  it("更换 Key 立即清除旧模型选择，旧响应不能覆盖新范围", async () => {
    let resolveOld: (response: Response) => void = () => undefined;
    let oldSignal: AbortSignal | undefined;
    const fetchSpy = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const auth = (init?.headers as Record<string, string>).Authorization;
      if (auth === "Bearer sk-key-old") {
        oldSignal = init?.signal as AbortSignal;
        return new Promise<Response>((resolve) => { resolveOld = resolve; });
      }
      return jsonResponse(catalogue(auth === "Bearer sk-key-a" ? "miora-pro" : "miora-max"));
    });
    vi.stubGlobal("fetch", fetchSpy);
    const view = render(<CcSwitchImportButtons apiKey="sk-key-a" />);
    await screen.findByRole("option", { name: "miora-pro" });
    await userEvent.selectOptions(screen.getByTestId("ccswitch-model"), "miora-pro@miora");
    view.rerender(<CcSwitchImportButtons apiKey="sk-key-old" />);
    expect(modelInLink()).toBeNull();
    expect(screen.queryByRole("option", { name: "miora-pro" })).not.toBeInTheDocument();
    await waitFor(() => expect(oldSignal).toBeDefined());
    view.rerender(<CcSwitchImportButtons apiKey="sk-key-b" />);
    expect(oldSignal?.aborted).toBe(true);
    await screen.findByRole("option", { name: "miora-max" });
    await act(async () => resolveOld(jsonResponse(catalogue("stale-model"))));
    expect(screen.queryByText("stale-model")).not.toBeInTheDocument();
    expect(screen.getByRole("option", { name: "miora-max" })).toBeInTheDocument();
  });

  it("关闭组件会取消仍在进行的模型请求", async () => {
    let signal: AbortSignal | undefined;
    vi.stubGlobal("fetch", vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      signal = init?.signal as AbortSignal;
      return new Promise<Response>((_resolve, reject) => {
        signal!.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
      });
    }));
    const view = render(<CcSwitchImportButtons apiKey="sk-key-a" />);
    await waitFor(() => expect(signal).toBeDefined());
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });
});
