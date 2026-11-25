import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, makeCredential, renderPage, userEvent } from "@/test-utils";
import { ApiKeysPage } from "../ApiKeysPage";
import { accountScopeIssue, accountScopeMode } from "../apikeys/AccountScopeSelector";
import type { Credential, CredentialGroup } from "@/api/types";

const groups: CredentialGroup[] = [
  { id: "g1", provider: "miora", name: "生产组", created_at: 1, updated_at: 1, member_count: 2 },
  { id: "g2", provider: "miora", name: "备用组", created_at: 1, updated_at: 1, member_count: 1 },
  { id: "empty", provider: "miora", name: "空组", created_at: 1, updated_at: 1, member_count: 0 },
  { id: "ca", provider: "codearts", name: "CodeArts组", created_at: 1, updated_at: 1, member_count: 1 },
];
const accounts = [makeCredential({ id: "mi1", provider: "miora" })] as Credential[];
const KEY = { id: "key1", username: "root", name: "Key", preview: "sk-…test", created_at: 1,
  last_used_at: null, provider_binding: "miora", allowed_ips: "", allowed_models: "", expires_at: null };

function setup(groupIds: string[] = [], failed = false) {
  const writes: Record<string, unknown>[] = [];
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/credential-groups")) {
      if (failed) throw new Error("offline");
      return jsonResponse({ groups, is_operator: false });
    }
    if (url.startsWith("/api/credentials")) return jsonResponse({ credentials: accounts });
    if (url.startsWith("/api/api-keys")) {
      if (init?.method === "POST" || init?.method === "PUT") {
        writes.push(JSON.parse(String(init.body)));
        return jsonResponse({ ...KEY, api_key: "sk-created" });
      }
      return jsonResponse({ api_keys: [{ ...KEY, group_ids: groupIds }], total: 1 });
    }
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", spy);
  renderPage(<ApiKeysPage />);
  return { writes, spy };
}

describe("API Key 按账号分组绑定", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("按组展示名称/渠道/数量，可多选，提交稳定ID与空账号数组", async () => {
    const { writes } = setup();
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("open-create-key-dialog"));
    await userEvent.selectOptions(screen.getByTestId("key-binding"), "miora");
    await userEvent.click(screen.getByTestId("key-scope-groups"));
    await screen.findByTestId("key-group-g1");
    expect(screen.getByText(/生产组 · Miora · 2 个账号/)).toBeInTheDocument();
    expect(screen.queryByTestId("key-group-ca")).not.toBeInTheDocument();
    expect(screen.getByTestId("create-key-submit")).toBeDisabled();
    await userEvent.click(screen.getByTestId("key-group-g1"));
    await userEvent.click(screen.getByTestId("key-group-g2"));
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0]).toMatchObject({ group_ids: ["g1", "g2"], credential_ids: [], provider_binding: "miora" });
  });

  it("三种模式切换显式清另一个选择，按账号/按组空选必须明确改全部", async () => {
    const { writes } = setup();
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("open-create-key-dialog"));
    await userEvent.click(screen.getByTestId("key-credential-mi1"));
    expect(screen.getByTestId("key-scope-accounts")).toBeChecked();
    await userEvent.click(screen.getByTestId("key-scope-groups"));
    await screen.findByTestId("key-group-g1");
    await userEvent.click(screen.getByTestId("key-group-g1"));
    await userEvent.click(screen.getByTestId("key-scope-accounts"));
    expect(screen.getByTestId("key-credential-mi1")).not.toBeChecked();
    expect(screen.getByTestId("create-key-submit")).toBeDisabled();
    fireEvent.submit(screen.getByTestId("create-key-submit").closest("form")!);
    expect(writes).toEqual([]);
    await userEvent.click(screen.getByTestId("key-scope-all"));
    expect(screen.getByTestId("create-key-submit")).toBeEnabled();
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].credential_ids).toEqual([]);
    expect(writes[0].group_ids).toBeUndefined();
  });

  it("编辑保留组ID，转按账号提交group_ids[]明确清除旧组", async () => {
    const { writes } = setup(["g1"]);
    await waitFor(() => expect(screen.getByTestId("key-credentials-key1")).toHaveTextContent("生产组"));
    await userEvent.click(screen.getByTestId("edit-key-key1"));
    expect(screen.getByTestId("edit-key-scope-groups")).toBeChecked();
    expect(screen.getByTestId("edit-key-group-g1")).toBeChecked();
    await userEvent.click(screen.getByTestId("edit-key-scope-accounts"));
    await userEvent.click(screen.getByTestId("edit-key-credential-mi1"));
    await userEvent.click(screen.getByTestId("edit-key-save"));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({ credential_ids: ["mi1"], group_ids: [] });
  });

  it("旧组缺失不能变成全池，移除最后一组后仍需明确选择全部", async () => {
    const { writes } = setup(["gone"]);
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("edit-key-key1"));
    await screen.findByTestId("edit-key-group-gone");
    expect(screen.getByTestId("edit-key-group-gone")).toBeChecked();
    expect(screen.getByTestId("edit-key-save")).toBeDisabled();
    await userEvent.click(screen.getByTestId("edit-key-group-gone"));
    expect(screen.getByTestId("edit-key-save")).toBeDisabled();
    await userEvent.click(screen.getByTestId("edit-key-scope-all"));
    await userEvent.click(screen.getByTestId("edit-key-save"));
    await waitFor(() => expect(writes).toHaveLength(1));
    expect(writes[0]).toMatchObject({ credential_ids: [], group_ids: [] });
  });

  it("空组可绑定但明确提示无成员当前不可用", async () => {
    const { writes } = setup();
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("open-create-key-dialog"));
    await userEvent.click(screen.getByTestId("key-scope-groups"));
    await screen.findByTestId("key-group-empty");
    await userEvent.click(screen.getByTestId("key-group-empty"));
    expect(screen.getByText(/所选分组当前没有成员/)).toBeInTheDocument();
    expect(screen.getByTestId("create-key-submit")).toBeEnabled();
    await userEvent.click(screen.getByTestId("create-key-submit"));
    await screen.findByTestId("new-key-plaintext");
    expect(writes[0].group_ids).toEqual(["empty"]);
  });

  it("目录失败保留原组ID并阻止保存，不退回全池", async () => {
    const { writes } = setup(["g1"], true);
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("edit-key-key1"));
    await screen.findByText("账号分组加载失败，请重试。");
    expect(screen.getByTestId("edit-key-group-g1")).toBeChecked();
    expect(screen.getByTestId("edit-key-save")).toBeDisabled();
    expect(screen.getByRole("button", { name: "重试分组列表" })).toBeInTheDocument();
    expect(writes).toEqual([]);
  });

  it("跨渠道组保留但挡保存，调回绑定渠道恢复", async () => {
    setup(["g1"]);
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByTestId("edit-key-key1"));
    await screen.findByTestId("edit-key-group-g1");
    await userEvent.selectOptions(screen.getByTestId("edit-key-binding"), "codearts");
    expect(screen.getByTestId("edit-key-group-g1")).toBeChecked();
    expect(screen.getByTestId("edit-key-save")).toBeDisabled();
    await userEvent.selectOptions(screen.getByTestId("edit-key-binding"), "miora");
    expect(screen.getByTestId("edit-key-save")).toBeEnabled();
  });

  it("互斥与旧DTO模式判断保持稳定，不把缺失成员解析为全部账号", () => {
    expect(accountScopeMode([], [])).toBe("all");
    expect(accountScopeMode(["mi1"], [])).toBe("accounts");
    expect(accountScopeMode([], ["g1"])).toBe("groups");
    expect(accountScopeIssue("groups", ["mi1"], ["g1"], accounts, groups, "miora")).toContain("不能同时");
    expect(accountScopeIssue("groups", [], ["g1"], undefined, groups, "miora")).toBeNull();
  });
});
