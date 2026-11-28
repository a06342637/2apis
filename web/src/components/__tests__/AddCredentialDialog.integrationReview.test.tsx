import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, renderStandalone } from "@/test-utils";
import { LoginPage } from "@/pages/LoginPage";
import { Input } from "@/ui";
import { AddCredentialDialog } from "../AddCredentialDialog";

function dialog() {
  const callbacks = { onClose: vi.fn(), onImported: vi.fn(), onNotice: vi.fn(), onError: vi.fn() };
  render(<AddCredentialDialog open credentialCount={0} hasZen={false} hasKilo={false} {...callbacks} />);
  return callbacks;
}

describe("凭证入口和登录表单交叉回归", () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it.each([
    ["manual-raccoon-summary", "raccoon-token"],
    ["manual-lingxi-summary", "lingxi-cookie"],
    ["manual-free-summary", "zen-api-key"],
  ])("手工字段 %s 回车不会开始第一个渠道登录或导入凭证", async (summary, field) => {
    const fetch = vi.fn(async () => jsonResponse({}));
    const popup = vi.spyOn(window, "open").mockReturnValue(null);
    vi.stubGlobal("fetch", fetch);
    const callbacks = dialog();
    await userEvent.click(screen.getByTestId(summary));
    await userEvent.type(screen.getByTestId(field), "synthetic-review-value{enter}");
    expect(fetch).not.toHaveBeenCalled();
    expect(popup).not.toHaveBeenCalled();
    expect(callbacks.onImported).not.toHaveBeenCalled();
    expect(screen.getByTestId("start-login-codebuddy")).toBeEnabled();
  });

  it("JSON 导入昵称回车只提交所选 Miora 凭证，不触发渠道登录", async () => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ id: "synthetic-account", merged: false }));
    vi.stubGlobal("fetch", fetch);
    const callbacks = dialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "miora");
    fireEvent.change(screen.getByTestId("import-payload"), { target: {
      value: '{"authToken":"synthetic-auth","refreshToken":"synthetic-refresh"}',
    } });
    await userEvent.type(screen.getByTestId("import-nickname"), "review-account{enter}");
    await waitFor(() => expect(callbacks.onImported).toHaveBeenCalledOnce());
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0][0]).toBe("/api/credentials");
    expect(fetch.mock.calls[0][1]?.method).toBe("POST");
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({
      provider: "miora", nickname: "review-account",
      credential: { authToken: "synthetic-auth", refreshToken: "synthetic-refresh" },
    });
  });

  it.each([
    ["", "导出备注"], ["   ", "导出备注"], [" 手工备注 ", "手工备注"],
  ])("导出凭证重新导入保留标签，输入备注 %j 时使用 %j", async (inputNickname, expectedNickname) => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      jsonResponse({ id: "synthetic-codearts", merged: false }));
    vi.stubGlobal("fetch", fetch);
    const callbacks = dialog();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    const credential = { access_key_id: "synthetic-ak", secret_access_key: "synthetic-sk" };
    fireEvent.change(screen.getByTestId("import-payload"), { target: { value: JSON.stringify({
      provider: "codearts", nickname: "导出备注", tags: ["自己4594", "备用"], credential,
    }) } });
    fireEvent.change(screen.getByTestId("import-nickname"), { target: { value: inputNickname } });
    await userEvent.click(screen.getByTestId("import-submit"));
    await waitFor(() => expect(callbacks.onImported).toHaveBeenCalledOnce());
    expect(fetch).toHaveBeenCalledOnce();
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({
      provider: "codearts", nickname: expectedNickname, tags: ["自己4594", "备用"], credential,
    });
  });

  it("真实剪贴板拒绝并且旧接口失败时，不报告成功或留下临时 textarea", async () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    Object.defineProperty(document, "execCommand", { configurable: true, value: vi.fn(() => false) });
    dialog();
    fireEvent.click(screen.getByTestId("miora-login-summary"));
    fireEvent.click(screen.getByTestId("miora-copy-command"));
    await waitFor(() => expect(screen.getByTestId("miora-copy-command")).toHaveTextContent("复制失败"));
    expect(screen.getByTestId("miora-copy-command")).not.toHaveTextContent("已复制");
    expect(document.querySelector("textarea")).toBeNull();
    expect(screen.getByTestId("miora-copy-command").querySelector("code"))
      .toHaveTextContent('localStorage.getItem("refreshToken")');
  });

  it("真实登录页面保留自动填充语义，搜索框手输 admin 不影响登录提交", async () => {
    const fetch = vi.fn(async () => jsonResponse({ error: { code: "invalid_credentials" } }, 401));
    vi.stubGlobal("fetch", fetch);
    renderStandalone(<><Input aria-label="搜索凭证" /><LoginPage /></>);
    const username = screen.getByTestId("login-username");
    const password = screen.getByTestId("login-password");
    expect(username).toHaveAttribute("name", "username");
    expect(username).toHaveAttribute("autocomplete", "username");
    expect(password).toHaveAttribute("name", "password");
    expect(password).toHaveAttribute("autocomplete", "current-password");
    expect(username).not.toHaveAttribute("data-1p-ignore");
    expect(password).not.toHaveAttribute("data-lpignore");
    const search = screen.getByRole("searchbox", { name: "搜索凭证" });
    await userEvent.type(search, "admin");
    expect(search).toHaveValue("admin");
    expect(search).toHaveAttribute("autocomplete", "off");
    expect(search).toHaveAttribute("data-bwignore", "true");
    expect(fetch).not.toHaveBeenCalled();
    await userEvent.type(username, "operator");
    await userEvent.type(password, "wrong-password{enter}");
    await screen.findByTestId("login-error");
    expect(fetch).toHaveBeenCalledWith("/api/auth/login", expect.objectContaining({
      method: "POST", body: JSON.stringify({ username: "operator", password: "wrong-password" }),
    }));
    expect(search).toHaveValue("admin");
  });
});
