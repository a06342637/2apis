import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, makeClient } from "@/test-utils";
import { KeyRow } from "../KeyRow";
import { REVEAL_KEY_TIMEOUT_MS, RevealKey } from "../RevealKey";
import type { ApiKey } from "@/api/types";

const PLAIN = "sk-temporary-private-value";
const PASSWORD = "current-login-password";
const props = { id: "key_1", preview: "sk-…test", canReveal: true };
const originalExecCommand = document.execCommand;

function open() {
  fireEvent.click(screen.getByRole("button", { name: "查看完整 Key" }));
}
async function submit(password = PASSWORD) {
  fireEvent.change(screen.getByLabelText("当前登录密码"), { target: { value: password } });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "验证并查看" }));
  });
}
function deferred() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}

describe("RevealKey", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ api_key: PLAIN })));
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    Object.defineProperty(document, "execCommand", {
      configurable: true, writable: true, value: originalExecCommand,
    });
  });

  it("验证当前密码后仅在该行展示，复制和隐藏可用，不存入查询/浏览器缓存", async () => {
    const client = makeClient();
    const writeText = vi.fn(async () => undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const log = vi.spyOn(console, "log");
    const error = vi.spyOn(console, "error");
    render(<QueryClientProvider client={client}><RevealKey {...props} /></QueryClientProvider>);
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(props.preview);
    open();
    expect(screen.getByLabelText("当前登录密码")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "验证并查看" })).toBeDisabled();
    await submit();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(PLAIN);
    const [url, init] = vi.mocked(fetch).mock.calls[0];
    expect(url).toBe("/api/api-keys/key_1/reveal");
    expect(init).toMatchObject({ method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify({ password: PASSWORD }) });
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "复制" })); });
    expect(writeText).toHaveBeenCalledWith(PLAIN);
    expect(screen.getByRole("button", { name: "已复制" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "隐藏完整 Key" }));
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(props.preview);
    open();
    expect(screen.getByLabelText("当前登录密码")).toHaveValue("");
    expect(client.getQueryCache().getAll()).toEqual([]);
    expect(client.getMutationCache().getAll()).toEqual([]);
    expect(storage).not.toHaveBeenCalled();
    expect(log).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });

  it("旧 Key 明确不可恢复，无密码请求", () => {
    render(<RevealKey {...props} canReveal={false} />);
    expect(screen.getByText(/旧 Key 未保存可恢复的明文/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看完整 Key" })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("旧部署缺 can_reveal 字段时允许尝试，后端旧 Key 错误不误报密码错误", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: {
      code: "key_not_recoverable", message: "not recoverable" } }, 409)));
    render(<RevealKey id={props.id} preview={props.preview} />);
    open();
    await submit();
    expect(screen.getByRole("alert")).toHaveTextContent("旧 Key 未保存可恢复的明文");
    expect(screen.getByLabelText("当前登录密码")).toHaveValue("");
    expect(screen.queryByText(/密码不正确/)).not.toBeInTheDocument();
  });

  it.each([
    [401, "invalid_password", "当前登录密码不正确"],
    [401, "session_expired", "登录已过期"],
    [403, "forbidden", "当前账号无权查看"],
    [404, "not_found", "已删除"],
    [429, "rate_limited", "验证过于频繁"],
    [500, "internal_error", "查看失败"],
  ])("错误 %i/%s 只显示受控的局部文案", async (status, code, expected) => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: {
      code, message: `${PASSWORD} ${PLAIN}` } }, status)));
    render(<RevealKey {...props} />);
    open();
    await submit();
    expect(screen.getByRole("alert")).toHaveTextContent(expected);
    expect(document.body.textContent).not.toContain(PASSWORD);
    expect(document.body.textContent).not.toContain(PLAIN);
    expect(screen.getByLabelText("当前登录密码")).toHaveValue("");
  });

  it.each([null, {}, { api_key: "" }, { api_key: 123 }])("无效成功响应不展示 Key: %j", async (body) => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(body)));
    render(<RevealKey {...props} />);
    open();
    await submit();
    expect(screen.getByRole("alert")).toHaveTextContent("查看失败");
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(props.preview);
  });

  it("网络错误不展示任意错误正文，允许重新验证", async () => {
    const spy = vi.fn().mockRejectedValueOnce(new Error(PASSWORD + PLAIN))
      .mockResolvedValueOnce(jsonResponse({ api_key: PLAIN }));
    vi.stubGlobal("fetch", spy);
    render(<RevealKey {...props} />);
    open();
    await submit();
    expect(screen.getByRole("alert")).toHaveTextContent("查看失败");
    await submit();
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(PLAIN);
  });

  it.each(["cancel", "escape", "backdrop"])("%s 取消会清空密码，再打开无残留", async (action) => {
    render(<RevealKey {...props} />);
    open();
    fireEvent.change(screen.getByLabelText("当前登录密码"), { target: { value: PASSWORD } });
    if (action === "cancel") fireEvent.click(screen.getByRole("button", { name: "取消" }));
    else if (action === "escape") fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    else fireEvent.click(screen.getByTestId("reveal-key-backdrop"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    open();
    expect(screen.getByLabelText("当前登录密码")).toHaveValue("");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("请求取消后晚到响应不显示，重新验证只接受新请求", async () => {
    const first = deferred(), second = deferred();
    const spy = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    vi.stubGlobal("fetch", spy);
    render(<RevealKey {...props} />);
    open();
    await submit();
    expect(screen.getByLabelText("当前登录密码")).toHaveValue("");
    expect(screen.getByRole("button", { name: "验证中…" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(spy.mock.calls[0][1].signal.aborted).toBe(true);
    open();
    await submit("second password");
    await act(async () => { first.resolve(jsonResponse({ api_key: "sk-stale" })); });
    expect(document.body.textContent).not.toContain("sk-stale");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    await act(async () => { second.resolve(jsonResponse({ api_key: PLAIN })); });
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(PLAIN);
  });

  it("60 秒自动清空密码/明文，并中止尚未结束的验证", async () => {
    vi.useFakeTimers();
    const pending = deferred();
    const spy = vi.fn().mockReturnValueOnce(pending.promise).mockResolvedValueOnce(jsonResponse({ api_key: PLAIN }));
    vi.stubGlobal("fetch", spy);
    render(<RevealKey {...props} />);
    open();
    await submit();
    act(() => vi.advanceTimersByTime(REVEAL_KEY_TIMEOUT_MS));
    expect(spy.mock.calls[0][1].signal.aborted).toBe(true);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => pending.resolve(jsonResponse({ api_key: "sk-expired" })));
    expect(document.body.textContent).not.toContain("sk-expired");
    open();
    expect(screen.getByLabelText("当前登录密码")).toHaveValue("");
    await submit();
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(PLAIN);
    act(() => vi.advanceTimersByTime(REVEAL_KEY_TIMEOUT_MS - 1));
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(PLAIN);
    act(() => vi.advanceTimersByTime(1));
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(props.preview);
  });

  it("卸载时中止请求，晚到结果不会留下正文或更新状态", async () => {
    const pending = deferred();
    const spy = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => pending.promise);
    vi.stubGlobal("fetch", spy);
    const view = render(<RevealKey {...props} />);
    open();
    await submit();
    view.unmount();
    expect(spy.mock.calls[0][1]?.signal?.aborted).toBe(true);
    await act(async () => pending.resolve(jsonResponse({ api_key: PLAIN })));
    expect(document.body.textContent).not.toContain(PLAIN);
  });

  it("未提交的密码在60秒后清掉，重复提交不会多发验证请求", async () => {
    vi.useFakeTimers();
    const pending = deferred();
    vi.stubGlobal("fetch", vi.fn(() => pending.promise));
    render(<RevealKey {...props} />);
    open();
    fireEvent.change(screen.getByLabelText("当前登录密码"), { target: { value: PASSWORD } });
    act(() => vi.advanceTimersByTime(REVEAL_KEY_TIMEOUT_MS));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    open();
    expect(screen.getByLabelText("当前登录密码")).toHaveValue("");
    await submit();
    fireEvent.submit(screen.getByRole("dialog"));
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("换行或权限元数据变为不可恢复会清掉正在显示的 Key", async () => {
    const view = render(<RevealKey {...props} />);
    open(); await submit();
    view.rerender(<RevealKey {...props} id="key_2" preview="sk-…next" />);
    expect(document.body.textContent).not.toContain(PLAIN);
    open(); await submit();
    view.rerender(<RevealKey {...props} id="key_2" canReveal={false} />);
    expect(document.body.textContent).not.toContain(PLAIN);
    expect(screen.getByText(/旧 Key 未保存/)).toBeInTheDocument();
  });

  it.each([true, false, "throw"])("非 HTTPS 复制兜底始终删除临时明文 textarea: %s", async (outcome) => {
    vi.stubGlobal("navigator", { clipboard: undefined });
    const execCommand = vi.fn(() => {
      if (outcome === "throw") throw new Error("copy blocked");
      return outcome;
    });
    Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });
    render(<RevealKey {...props} />);
    open(); await submit();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "复制" })));
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.querySelector("textarea")).toBeNull();
    expect(screen.getByRole("button", { name: outcome === true ? "已复制" : "请手动复制" })).toBeInTheDocument();
  });

  it("复制晚到反馈不会在隐藏或新验证期间恢复旧 Key", async () => {
    let finish!: () => void;
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn(() => new Promise<void>((done) => { finish = done; })) } });
    render(<RevealKey {...props} />);
    open(); await submit();
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    fireEvent.click(screen.getByRole("button", { name: "隐藏完整 Key" }));
    await act(async () => finish());
    expect(screen.getByTestId("key-preview-key_1")).toHaveTextContent(props.preview);
    expect(screen.queryByText("已复制")).not.toBeInTheDocument();
  });

  it("剪贴板权限拒绝时允许手动复制，错误和隐藏都不保留临时明文", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn(async () => { throw new Error(PLAIN); }) } });
    Object.defineProperty(document, "execCommand", { configurable: true, value: undefined });
    render(<RevealKey {...props} />);
    open(); await submit();
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "复制" })));
    expect(screen.getByRole("button", { name: "请手动复制" })).toBeInTheDocument();
    expect(document.querySelector("textarea")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "隐藏完整 Key" }));
    expect(document.body.textContent).not.toContain(PLAIN);
  });

  it("KeyRow 使用当前行 DTO，同时保留账号绑定/原操作", () => {
    const item: ApiKey = { ...props, username: "root", name: "laptop", created_at: 1,
      last_used_at: null, provider_binding: "miora", credential_ids: ["a", "b"],
      allowed_ips: "", allowed_models: "", expires_at: null, can_reveal: true };
    render(<table><tbody><KeyRow item={item} window="today" confirming={false}
      onRemove={vi.fn()} onCancelDelete={vi.fn()} onEdit={vi.fn()} onEditQuota={vi.fn()}
      onImport={vi.fn()} onConfirmDelete={vi.fn()} /></tbody></table>);
    expect(screen.getByRole("button", { name: "查看完整 Key" })).toBeInTheDocument();
    expect(screen.getByTestId("key-credentials-key_1")).toHaveTextContent("指定 2 个账号");
    expect(screen.getByTestId("import-ccswitch-key_1")).toBeInTheDocument();
  });
});
