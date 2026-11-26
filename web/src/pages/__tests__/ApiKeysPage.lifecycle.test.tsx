import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fullSession, jsonResponse, makeClient, renderPage } from "@/test-utils";
import { ApiKeysPage } from "../ApiKeysPage";
import { Layout } from "@/Layout";

const KEY = { id: "one", name: "One", username: "root", preview: "sk-…one", created_at: 1,
  last_used_at: null, provider_binding: "", credential_ids: [], allowed_ips: "", allowed_models: "", expires_at: null };
const SECOND = { ...KEY, id: "two", name: "Two", preview: "sk-…two" };

function pendingResponse() {
  let resolve!: (value: Response) => void;
  const promise = new Promise<Response>((done) => { resolve = done; });
  return { promise, resolve };
}
async function createDialog() {
  fireEvent.click(screen.getByTestId("open-create-key-dialog"));
  await screen.findByTestId("create-key-dialog");
}

describe("API Key 弹窗异步生命周期", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("创建请求期间关闭后，晚到明文不能出现在接入示例或重新打开的草稿中", async () => {
    const pending = pendingResponse();
    let saved = false;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith("/api/api-keys")) {
        if (init?.method === "POST") return pending.promise;
        return jsonResponse({ api_keys: saved ? [KEY] : [], total: saved ? 1 : 0 });
      }
      return jsonResponse({});
    }));
    renderPage(<ApiKeysPage />);
    await screen.findByTestId("no-keys");
    await createDialog();
    await act(async () => fireEvent.click(screen.getByTestId("create-key-submit")));
    fireEvent.click(screen.getByTestId("create-key-close"));
    await createDialog();
    fireEvent.change(screen.getByTestId("key-name"), { target: { value: "New unsaved draft" } });
    saved = true;
    await act(async () => pending.resolve(jsonResponse({ ...KEY, api_key: "sk-late-private-value" })));
    expect(document.body.textContent).not.toContain("sk-late-private-value");
    expect(screen.getByTestId("key-name")).toHaveValue("New unsaved draft");
    await waitFor(() => expect(screen.getByTestId("keys-table")).toHaveTextContent("One"));
  });

  it("旧 Key 的复制晚到反馈不能把下一把新 Key 标成已复制", async () => {
    let finishCopy!: () => void;
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn(() => new Promise<void>((done) => { finishCopy = done; })) } });
    let created = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith("/api/api-keys")) {
        if (init?.method === "POST") return jsonResponse({ ...KEY, api_key: `sk-key-${++created}` });
        return jsonResponse({ api_keys: [], total: 0 });
      }
      return jsonResponse({});
    }));
    renderPage(<ApiKeysPage />);
    await screen.findByTestId("no-keys");
    await createDialog();
    await act(async () => fireEvent.click(screen.getByTestId("create-key-submit")));
    fireEvent.click(within(screen.getByTestId("create-key-dialog")).getByRole("button", { name: "复制" }));
    fireEvent.click(screen.getByTestId("create-key-close"));
    await createDialog();
    await act(async () => fireEvent.click(screen.getByTestId("create-key-submit")));
    await act(async () => finishCopy());
    expect(screen.getByTestId("new-key-plaintext")).toHaveTextContent("sk-key-2");
    expect(within(screen.getByTestId("create-key-dialog")).queryByRole("button", { name: "已复制" })).not.toBeInTheDocument();
  });

  it("保存第一把 Key 配额后不能关闭后来打开的第二把 Key 配额草稿", async () => {
    const pending = pendingResponse();
    const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/quota") && init?.method === "PUT") return pending.promise;
      if (String(input).startsWith("/api/api-keys")) return jsonResponse({ api_keys: [KEY, SECOND], total: 2 });
      return jsonResponse({});
    });
    vi.stubGlobal("fetch", spy);
    renderPage(<ApiKeysPage />);
    await screen.findByTestId("keys-table");
    fireEvent.click(screen.getByTestId("edit-quota-one"));
    await act(async () => fireEvent.click(screen.getByTestId("key-quota-save")));
    fireEvent.click(screen.getByTestId("key-quota-close"));
    fireEvent.click(screen.getByTestId("edit-quota-two"));
    fireEvent.change(screen.getByTestId("key-quota-edit-rpm"), { target: { value: "7" } });
    await act(async () => pending.resolve(jsonResponse({ ok: true })));
    expect(screen.getByTestId("key-quota-dialog")).toHaveTextContent("Two");
    expect(screen.getByTestId("key-quota-edit-rpm")).toHaveValue(7);
  });

  it("配额网络异常显示局部受控文案，不回显原始异常正文", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/quota") && init?.method === "PUT") throw new Error("network implementation details");
      if (String(input).startsWith("/api/api-keys")) return jsonResponse({ api_keys: [KEY], total: 1 });
      return jsonResponse({});
    }));
    renderPage(<ApiKeysPage />);
    await screen.findByTestId("keys-table");
    fireEvent.click(screen.getByTestId("edit-quota-one"));
    await act(async () => fireEvent.click(screen.getByTestId("key-quota-save")));
    expect(screen.getByTestId("key-quota-dialog")).toHaveTextContent("保存配额失败");
    expect(document.body.textContent).not.toContain("network implementation details");
  });

  it("配额提交同一帧多次只发一次PUT", async () => {
    const pending = pendingResponse();
    let writes = 0;
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes("/quota") && init?.method === "PUT") { writes += 1; return pending.promise; }
      if (String(input).startsWith("/api/api-keys")) return jsonResponse({ api_keys: [KEY], total: 1 });
      return jsonResponse({});
    }));
    renderPage(<ApiKeysPage />);
    await screen.findByTestId("keys-table");
    fireEvent.click(screen.getByTestId("edit-quota-one"));
    await act(async () => {
      const button = screen.getByTestId("key-quota-save");
      fireEvent.click(button); fireEvent.click(button);
    });
    expect(writes).toBe(1);
    await act(async () => pending.resolve(jsonResponse({ ok: true })));
  });

  it("策略保存的晚到结果不能关闭重新打开的另一把Key草稿", async () => {
    const pending = pendingResponse();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === "/api/api-keys/one" && init?.method === "PUT") return pending.promise;
      if (String(input).startsWith("/api/api-keys")) return jsonResponse({ api_keys: [KEY, SECOND], total: 2 });
      return jsonResponse({});
    }));
    renderPage(<ApiKeysPage />);
    await screen.findByTestId("keys-table");
    fireEvent.click(screen.getByTestId("edit-key-one"));
    await act(async () => fireEvent.click(screen.getByTestId("edit-key-save")));
    fireEvent.click(screen.getByTestId("edit-key-close"));
    fireEvent.click(screen.getByTestId("edit-key-two"));
    fireEvent.change(screen.getByTestId("edit-key-name"), { target: { value: "Preserved Two" } });
    await act(async () => pending.resolve(jsonResponse({ ok: true })));
    expect(screen.getByTestId("edit-key-name")).toHaveValue("Preserved Two");
  });

  it("登录用户变化时Key列表必须使用新用户名缓存，不能显示上个用户的数据", async () => {
    const pending = pendingResponse();
    let user = "alice";
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith("/api/api-keys")) {
        return user === "alice" ? jsonResponse({ api_keys: [{ ...KEY, name: "Alice private key" }], total: 1 }) : pending.promise;
      }
      return jsonResponse({});
    }));
    const client = makeClient();
    const tree = (username: string) => <QueryClientProvider client={client}><MemoryRouter>
      <Routes><Route element={<Layout session={fullSession({ username, is_admin: true })} />}>
        <Route path="*" element={<ApiKeysPage />} />
      </Route></Routes>
    </MemoryRouter></QueryClientProvider>;
    const view = render(tree("alice"));
    await screen.findByText("Alice private key");
    user = "bob";
    view.rerender(tree("bob"));
    expect(screen.queryByText("Alice private key")).not.toBeInTheDocument();
    await act(async () => pending.resolve(jsonResponse({ api_keys: [{ ...SECOND, name: "Bob key" }], total: 1 })));
    expect(await screen.findByText("Bob key")).toBeInTheDocument();
  });

  it("登录用户变化会撤销旧弹窗，旧用户创建请求晚到不得显示明文", async () => {
    const pending = pendingResponse();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).startsWith("/api/api-keys")) {
        if (init?.method === "POST") return pending.promise;
        return jsonResponse({ api_keys: [], total: 0 });
      }
      return jsonResponse({});
    }));
    const client = makeClient();
    const tree = (username: string) => <QueryClientProvider client={client}><MemoryRouter>
      <Routes><Route element={<Layout session={fullSession({ username, is_admin: true })} />}>
        <Route path="*" element={<ApiKeysPage />} />
      </Route></Routes>
    </MemoryRouter></QueryClientProvider>;
    const view = render(tree("alice"));
    await screen.findByTestId("no-keys");
    await createDialog();
    await act(async () => fireEvent.click(screen.getByTestId("create-key-submit")));
    view.rerender(tree("bob"));
    await act(async () => pending.resolve(jsonResponse({ ...KEY, api_key: "sk-alice-only-private" })));
    expect(screen.queryByTestId("create-key-dialog")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("sk-alice-only-private");
  });
});
