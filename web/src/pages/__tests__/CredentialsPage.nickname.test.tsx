import { QueryClient } from "@tanstack/react-query";
import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { expandCredentialGroups, jsonResponse, makeCredential, mockCredentialsFetch,
  renderPage, settle, userEvent } from "@/test-utils";
import { CredentialsPage } from "../CredentialsPage";

const ADMIN = { username: "root", is_admin: true } as const;
const OPERATOR = { username: "op", is_admin: false, role: "operator" } as const;
const VIEWER = { username: "viewer", is_admin: false } as const;

const id = "cred-edit-me";
const url = `/api/credentials/${id}/nickname`;

async function editNickname() {
  await userEvent.click(await screen.findByTestId(`actions-${id}`));
  await userEvent.click(screen.getByRole("menuitem", { name: "修改昵称" }));
  const dialog = await screen.findByRole("dialog", { name: "修改昵称" });
  return { dialog, input: within(dialog).getByRole("textbox", { name: "凭证昵称" }),
    save: within(dialog).getByRole("button", { name: "保存昵称" }),
    cancel: within(dialog).getByRole("button", { name: "取消" }) };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("凭证昵称", () => {
  it.each([
    { provider: "miora", session: ADMIN },
    { provider: "trae", session: OPERATOR },
    { provider: "zen", session: ADMIN },
  ])("$provider 行菜单改名后回显，清空时回落到凭证 ID", async ({ provider, session }) => {
    let nickname = "原昵称";
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    const fetch = mockCredentialsFetch({
      [url]: () => jsonResponse({ nickname: "" }),
      "/api/credentials": () => jsonResponse({ is_operator: true,
        credentials: [makeCredential({ id, provider, nickname }),
          makeCredential({ id: "other", provider, nickname: "不要动" })] }),
    });
    // 保留真实 API 客户端的请求体；模拟保存后列表读到持久化的名称。
    const dispatch = fetch.getMockImplementation()!;
    fetch.mockImplementation(async (input, init) => {
      if (String(input) === url && init?.method === "PUT") {
        ({ nickname } = JSON.parse(String(init.body)));
        return jsonResponse({ nickname });
      }
      return dispatch(input, init);
    });
    renderPage(<CredentialsPage />, session);
    await settle();
    await expandCredentialGroups();
    const first = await editNickname();
    expect(first.input).toHaveValue("原昵称");
    fireEvent.change(first.input, { target: { value: "  备用 🚀  " } });
    await userEvent.click(first.save);
    await waitFor(() => expect(within(screen.getByTestId(`row-${id}`))
      .getByText("备用 🚀")).toBeInTheDocument());
    expect(screen.getByTestId("row-other")).toHaveTextContent("不要动");
    expect(screen.queryByRole("dialog", { name: "修改昵称" })).not.toBeInTheDocument();
    expect(screen.getByText("凭证昵称已保存。")).toBeInTheDocument();
    for (const key of ["credentials", "stats-credentials", "stats-events"]) {
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["admin", session.username, key] });
    }

    const second = await editNickname();
    expect(second.input).toHaveValue("备用 🚀");
    await userEvent.clear(second.input);
    await userEvent.type(second.input, "{Enter}");
    await waitFor(() => expect(within(screen.getByTestId(`row-${id}`))
      .getByText(id.slice(0, 12))).toBeInTheDocument());
    expect(fetch.mock.calls.filter(([input]) => input === url).map(([, options]) => ({
      method: options?.method, body: JSON.parse(String(options?.body)),
    }))).toEqual([
      { method: "PUT", body: { nickname: "备用 🚀" } },
      { method: "PUT", body: { nickname: "" } },
    ]);
  });

  it("超长输入不发送，服务端拒绝后保留编辑，保存期间不能重复提交或关闭", async () => {
    let respond!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { respond = resolve; });
    const fetch = mockCredentialsFetch({
      [url]: () => jsonResponse({}),
      "/api/credentials": { is_operator: true,
        credentials: [makeCredential({ id, provider: "qoder" })] },
    });
    const dispatch = fetch.getMockImplementation()!;
    fetch.mockImplementation((input, init) => String(input) === url ? pending : dispatch(input, init));
    renderPage(<CredentialsPage />, ADMIN);
    await settle();
    await expandCredentialGroups();
    const { dialog, input, save, cancel } = await editNickname();
    const changed = (nickname: string) => fireEvent.change(input, { target: { value: nickname } });
    changed("🚀".repeat(129));
    await userEvent.click(save);
    expect(within(dialog).getByRole("alert")).toHaveTextContent("昵称最长 128 个字符");
    expect(fetch.mock.calls.filter(([input]) => input === url)).toHaveLength(0);
    const nickname = "🚀".repeat(128);
    changed(nickname);
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    await userEvent.click(save);
    expect(save).toBeDisabled();
    expect(input).toBeDisabled();
    expect(cancel).toBeDisabled();
    fireEvent.submit(save.closest("form")!);
    fireEvent.click(dialog.parentElement!);
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(dialog).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([input]) => input === url)).toHaveLength(1);
    await act(async () => respond(jsonResponse({ error: { message: "凭证不存在", code: "invalid_request" } }, 400)));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("凭证不存在");
    expect(input).toHaveValue(nickname);
    expect(save).toBeEnabled();
    await userEvent.click(cancel);
    expect(dialog).not.toBeInTheDocument();
  });

  it("只读用户没有昵称编辑入口", async () => {
    mockCredentialsFetch({ "/api/credentials": { is_operator: false,
      credentials: [makeCredential({ id, provider: "miora" })] } });
    renderPage(<CredentialsPage />, VIEWER);
    await settle();
    await expandCredentialGroups();
    expect(screen.getByTestId(`row-${id}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`actions-${id}`)).not.toBeInTheDocument();
  });
});
