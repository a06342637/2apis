import { afterEach, describe, expect, it, vi } from "vitest";
import { api, REQUEST_TIMEOUT_MS } from "../client";
import { setUnauthorizedHandler } from "../unauthorized";

afterEach(() => {
  setUnauthorizedHandler(null);
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("批量凭证导出客户端", () => {
  it("POST携带当前IDs、CSRF头和no-store，文件原样保留可重新导入数组", async () => {
    const entries = [{ provider: "miora", nickname: "synthetic", tags: ["test"], credential: { authToken: "fake" } }];
    const fetch = vi.fn(async () => new Response(JSON.stringify(entries), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    const { blob } = await api.exportCredentials(["mi-a", "mi-b"]);
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/credentials/export", expect.objectContaining({
      method: "POST", body: JSON.stringify({ ids: ["mi-a", "mi-b"] }),
      credentials: "same-origin", cache: "no-store", signal: expect.any(AbortSignal),
      headers: { "X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json" },
    }));
    expect(JSON.parse(await blob.text())).toEqual(entries);
  });

  it("保留服务端错误状态、code及message", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: {
      code: "export_too_large", message: "too many selected credentials",
    } }), { status: 413 })));
    await expect(api.exportCredentials(["a"])).rejects.toMatchObject({
      status: 413, code: "export_too_large", message: "too many selected credentials",
    });
  });

  it("反代非JSON错误保留HTTP状态而非解析异常", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>bad gateway</html>", { status: 502 })));
    await expect(api.exportCredentials(["a"])).rejects.toMatchObject({
      status: 502, code: "export_failed", message: "导出失败（HTTP 502）",
    });
  });

  it("会话过期通知登录层并保留失败", async () => {
    const expired = vi.fn();
    setUnauthorizedHandler(expired);
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
    await expect(api.exportCredentials(["a"])).rejects.toMatchObject({ status: 401 });
    expect(expired).toHaveBeenCalledOnce();
  });

  it("读取文件体仍受超时限制", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async (_path, init: RequestInit) => ({ ok: true,
      blob: () => new Promise((_resolve, reject) => init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")))),
    })));
    const result = api.exportCredentials(["a"]);
    const assertion = expect(result).rejects.toMatchObject({ status: 0, code: "timeout" });
    await vi.advanceTimersByTimeAsync(REQUEST_TIMEOUT_MS);
    await assertion;
    expect(vi.getTimerCount()).toBe(0);
  });

  it("普通网络失败原样抛出并清理计时器", async () => {
    vi.useFakeTimers();
    const error = new TypeError("Failed to fetch");
    vi.stubGlobal("fetch", vi.fn(async () => { throw error; }));
    await expect(api.exportCredentials(["a"])).rejects.toBe(error);
    expect(vi.getTimerCount()).toBe(0);
  });
});
