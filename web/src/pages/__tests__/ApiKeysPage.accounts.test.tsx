import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, makeCredential, renderPage, userEvent } from "@/test-utils";
import { ApiKeysPage } from "../ApiKeysPage";

const KEY = {
  id: "key_1", username: "root", name: "限定账号", preview: "sk-…Ab3d",
  created_at: 1_700_000_000, last_used_at: null, provider_binding: "miora",
  allowed_ips: "", allowed_models: "", expires_at: null,
};
const ACCOUNTS = [
  makeCredential({ id: "mi1", provider: "miora", nickname: "主号" }),
  makeCredential({ id: "mi2", provider: "miora", nickname: "暂停号", enabled: 0 }),
  makeCredential({ id: "mi3", provider: "miora", nickname: "禁用号", disabled: 1 }),
  makeCredential({ id: "mi4", provider: "miora", nickname: "过期号", token_expires_at: 1 }),
  makeCredential({ id: "ca1", provider: "codearts", nickname: "CodeArts 主号" }),
];

function setup(credentialIds: string[] = [], credentials: unknown = ACCOUNTS) {
  const writes: { method: string; body: Record<string, unknown> }[] = [];
  const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/credentials")) {
      if (credentials instanceof Error) throw credentials;
      return jsonResponse({ credentials });
    }
    if (url.startsWith("/api/api-keys")) {
      if (init?.method === "POST" || init?.method === "PUT") {
        writes.push({ method: init.method, body: JSON.parse(String(init.body)) });
        return jsonResponse({ ...KEY, credential_ids: credentialIds, api_key: "sk-local-test" });
      }
      return jsonResponse({ api_keys: [{ ...KEY, credential_ids: credentialIds }], total: 1 });
    }
    if (url === "/v1/models") return jsonResponse({ data: [] });
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchSpy);
  renderPage(<ApiKeysPage />);
  return { writes, fetchSpy };
}

async function createDialog() {
  await screen.findByTestId("keys-table");
  await userEvent.click(screen.getByTestId("open-create-key-dialog"));
  return screen.findByTestId("create-key-dialog");
}

describe("API Key 账号范围", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("默认不勾选账号，明确提交 [] 并显示全部账号", async () => {
    const { writes } = setup();
    const dialog = await createDialog();
    expect(screen.getByTestId("key-credentials-key_1")).toHaveTextContent("全部账号");
    expect(within(dialog).getAllByRole("checkbox").every((box) => !(box as HTMLInputElement).checked)).toBe(true);
    expect(screen.getByTestId("key-credential-summary")).toHaveTextContent("含以后添加的账号");
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].body.credential_ids).toEqual([]);
  });

  it("绑定多个 Miora 账号，只提交选中的 IDs 并按渠道过滤", async () => {
    const { writes } = setup();
    await createDialog();
    await userEvent.selectOptions(screen.getByTestId("key-binding"), "miora");
    expect(screen.queryByTestId("key-credential-ca1")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("key-credential-mi1"));
    await userEvent.click(screen.getByTestId("key-credential-mi2"));
    expect(screen.getByTestId("key-credential-summary")).toHaveTextContent("已指定 2 个账号");
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].body).toMatchObject({ provider_binding: "miora", credential_ids: ["mi1", "mi2"] });
    expect(Object.keys(writes[0].body)).not.toContain("credentials");
  });

  it("暂停、禁用和过期状态可见，仍允许选择供以后恢复", async () => {
    const { writes } = setup();
    const dialog = await createDialog();
    expect(within(dialog).getByText("已暂停")).toBeInTheDocument();
    expect(within(dialog).getByText("已禁用")).toBeInTheDocument();
    expect(within(dialog).getByText("凭证已过期")).toBeInTheDocument();
    for (const id of ["mi2", "mi3", "mi4"]) await userEvent.click(screen.getByTestId(`key-credential-${id}`));
    expect(screen.getByTestId("create-key-submit")).toBeEnabled();
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].body.credential_ids).toEqual(["mi2", "mi3", "mi4"]);
  });

  it("切换渠道保留不兼容选择并阻止提交，改回渠道可恢复", async () => {
    const { writes } = setup();
    await createDialog();
    await userEvent.click(screen.getByTestId("key-credential-mi1"));
    await userEvent.selectOptions(screen.getByTestId("key-binding"), "codearts");
    expect(screen.getByTestId("key-credential-mi1")).toBeChecked();
    expect(screen.getByText("跨渠道")).toBeInTheDocument();
    expect(screen.getByTestId("create-key-submit")).toBeDisabled();
    // 回车提交同样不能越过验证。
    fireEvent.submit(screen.getByTestId("create-key-submit").closest("form")!);
    expect(writes).toEqual([]);
    await userEvent.selectOptions(screen.getByTestId("key-binding"), "miora");
    expect(screen.getByTestId("key-credential-mi1")).toBeChecked();
    expect(screen.getByTestId("create-key-submit")).toBeEnabled();
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].body.credential_ids).toEqual(["mi1"]);
  });

  it("明确取消跨渠道账号后可以改绑，并维持新选择", async () => {
    const { writes } = setup();
    await createDialog();
    await userEvent.click(screen.getByTestId("key-credential-mi1"));
    await userEvent.selectOptions(screen.getByTestId("key-binding"), "codearts");
    await userEvent.click(screen.getByTestId("key-credential-ca1"));
    await userEvent.click(screen.getByTestId("key-credential-mi1"));
    expect(screen.getByTestId("create-key-submit")).toBeEnabled();
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].body.credential_ids).toEqual(["ca1"]);
  });

  it("编辑回显原选择，删除账号保持选中直到明确移除", async () => {
    const { writes } = setup(["mi1", "deleted"]);
    await screen.findByTestId("keys-table");
    expect(screen.getByTestId("key-credentials-key_1")).toHaveTextContent("指定 2 个账号");
    await userEvent.click(screen.getByTestId("edit-key-key_1"));
    expect(screen.getByTestId("edit-key-credential-mi1")).toBeChecked();
    expect(screen.getByTestId("edit-key-credential-deleted")).toBeChecked();
    expect(screen.getByText("已删除 / 无权访问")).toBeInTheDocument();
    expect(screen.getByTestId("edit-key-save")).toBeDisabled();
    fireEvent.submit(screen.getByTestId("edit-key-save").closest("form")!);
    expect(writes).toEqual([]);
    await userEvent.click(screen.getByTestId("edit-key-credential-deleted"));
    await userEvent.click(screen.getByTestId("edit-key-save"));
    await waitFor(() => expect(screen.queryByTestId("edit-key-dialog")).not.toBeInTheDocument());
    expect(writes[0].body.credential_ids).toEqual(["mi1"]);
  });

  it("全部账号已删除时只有明确清空才能变为不限账号", async () => {
    const { writes } = setup(["deleted"], []);
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("edit-key-key_1"));
    expect(screen.getByTestId("edit-key-credential-deleted")).toBeChecked();
    expect(screen.getByTestId("edit-key-save")).toBeDisabled();
    await userEvent.click(screen.getByTestId("edit-key-clear-credentials"));
    expect(screen.getByTestId("edit-key-credential-summary")).toHaveTextContent("全部账号");
    await userEvent.click(screen.getByTestId("edit-key-save"));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0].body.credential_ids).toEqual([]);
  });

  it("账号列表失败时不把已有选择变成全部账号，重试后仍保留", async () => {
    const { writes, fetchSpy } = setup(["mi1"], new Error("offline"));
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("edit-key-key_1"));
    await screen.findByText("账号列表加载失败；仍可选择不限定账号，或重试后指定。");
    expect(screen.getByTestId("edit-key-credential-mi1")).toBeChecked();
    expect(screen.getByTestId("edit-key-save")).toBeDisabled();
    expect(writes).toEqual([]);
    fetchSpy.mockImplementation(async (input) => String(input).startsWith("/api/credentials")
      ? jsonResponse({ credentials: ACCOUNTS }) : jsonResponse({}));
    await userEvent.click(screen.getByRole("button", { name: "重试账号列表" }));
    await waitFor(() => expect(screen.getByTestId("edit-key-save")).toBeEnabled());
    expect(screen.getByTestId("edit-key-credential-mi1")).toBeChecked();
  });

  it("账号列表失败也可显式创建不限账号的 Key", async () => {
    const { writes } = setup([], new Error("offline"));
    await createDialog();
    expect(screen.getByTestId("create-key-submit")).toBeEnabled();
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].body.credential_ids).toEqual([]);
  });
});
