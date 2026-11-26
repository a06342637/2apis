import { QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, makeClient } from "@/test-utils";
import { useDisplayModels } from "../modelDisplay";

describe("管理台完整模型目录缓存", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("显式请求展示目录且按用户隔离，不污染聊天模型缓存", async () => {
    const client = makeClient();
    const fetch = vi.fn(async () => jsonResponse({ object: "list", data: [] }));
    vi.stubGlobal("fetch", fetch);
    const wrapper = ({ children }: { children: ReactNode }) =>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const view = renderHook(({ username }) => useDisplayModels(username), {
      initialProps: { username: "one" }, wrapper,
    });
    await waitFor(() => expect(view.result.current.isSuccess).toBe(true));
    expect(fetch).toHaveBeenCalledWith("/api/playground/models?include_unavailable=true",
      expect.objectContaining({ credentials: "same-origin" }));
    expect(client.getQueryData(["model-display", "one"])).toEqual({ object: "list", data: [] });
    expect(client.getQueryData(["playground-models"])).toBeUndefined();
    view.rerender({ username: "two" });
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(view.result.current.isSuccess).toBe(true));
    expect(client.getQueryData(["model-display", "two"])).toEqual({ object: "list", data: [] });
  });
});
