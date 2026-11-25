import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, makeCredential, renderPage, userEvent } from "@/test-utils";
import { CredentialsPage } from "../CredentialsPage";
import type { CredentialGroup } from "@/api/types";

const ACCOUNTS = [
  makeCredential({ id: "mi1", provider: "miora", nickname: "主号" }),
  makeCredential({ id: "mi2", provider: "miora", nickname: "暂停号", enabled: 0 }),
  makeCredential({ id: "ca1", provider: "codearts", nickname: "其他渠道" }),
];

function setup(write = true, fails = false) {
  let groups: CredentialGroup[] = [{ id: "g1", provider: "miora", name: "生产组", created_at: 1,
    updated_at: 1, member_count: 1, credential_ids: write ? ["mi1"] : undefined }];
  const spy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.startsWith("/api/credential-groups")) {
      if (fails) throw new Error("offline");
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body));
        const group = { ...body, id: "created", created_at: 1, updated_at: 1, member_count: body.credential_ids.length };
        groups = [...groups, group];
        return jsonResponse(group);
      }
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        groups = groups.map((group) => group.id === "created" ? { ...group, ...body, member_count: body.credential_ids.length } : group);
        return jsonResponse(groups[1]);
      }
      if (init?.method === "DELETE") {
        groups = groups.filter((group) => group.id !== "created");
        return jsonResponse({ ok: true });
      }
      return jsonResponse({ groups, is_operator: write });
    }
    if (url.startsWith("/api/credentials")) return jsonResponse({ credentials: ACCOUNTS,
      is_operator: write, expiry_window_seconds: 129600, expiry_secondary_window_seconds: 604800,
      token_expiry_warning_seconds: 3600 });
    if (url === "/api/playground/models") return jsonResponse({ data: [] });
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", spy);
  renderPage(<CredentialsPage />, write ? { username: "root", is_admin: true } : { username: "guest", is_admin: false });
  return spy;
}

describe("凭证页分组入口", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("每渠道组头最右分组按钮，点击不触发折叠，全量成员不受页面搜索过滤", async () => {
    const spy = setup();
    const action = await screen.findByTestId("credential-groups-miora");
    await waitFor(() => expect(action).toHaveTextContent("账号分组 1"));
    expect(screen.getByTestId("credential-groups-codearts")).toHaveTextContent("账号分组 0");
    const toggle = screen.getByTestId("group-toggle-miora");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(action.closest("button")).toBe(action);
    expect(toggle).not.toContainElement(action);
    await userEvent.click(action);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "关闭" }));
    fireEvent.change(screen.getByTestId("credential-search"), { target: { value: "主号" } });
    await userEvent.click(screen.getByTestId("credential-groups-miora"));
    await userEvent.click(screen.getByTestId("edit-group-g1"));
    expect(screen.getByTestId("group-edit-credential-mi2")).toBeInTheDocument();
    expect(screen.getByText("暂停号 · Miora")).toBeInTheDocument();
    expect(spy.mock.calls.filter(([url, init]) => String(url) === "/api/credential-groups" && !init?.method)).toHaveLength(1);
  });

  it("CRUD成功后刷新组头计数和名称，成员保存供Key动态绑定", async () => {
    setup();
    await screen.findByTestId("credential-groups-miora");
    await userEvent.click(screen.getByTestId("credential-groups-miora"));
    await userEvent.click(await screen.findByRole("button", { name: "新建分组" }));
    await userEvent.type(screen.getByLabelText("分组名称"), "新组");
    await userEvent.click(screen.getByTestId("group-edit-credential-mi2"));
    await userEvent.click(screen.getByRole("button", { name: "保存分组" }));
    await waitFor(() => expect(screen.getByTestId("credential-groups-miora")).toHaveTextContent("账号分组 2"));
    await userEvent.click(screen.getByTestId("edit-group-created"));
    expect(screen.getByTestId("group-edit-credential-mi2")).toBeChecked();
    fireEvent.change(screen.getByLabelText("分组名称"), { target: { value: "改名组" } });
    await userEvent.click(screen.getByRole("button", { name: "保存分组" }));
    await screen.findByText("改名组");
    await userEvent.click(screen.getByTestId("delete-group-created"));
    await userEvent.click(screen.getByRole("button", { name: "确认删除分组" }));
    await waitFor(() => expect(screen.getByTestId("credential-groups-miora")).toHaveTextContent("账号分组 1"));
    expect(screen.queryByText("改名组")).not.toBeInTheDocument();
  });

  it("readonly可以查看每渠道分组数量，不显示成员编辑与创建", async () => {
    setup(false);
    await screen.findByTestId("credential-groups-miora");
    await userEvent.click(screen.getByTestId("credential-groups-miora"));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog).toHaveTextContent("生产组"));
    expect(within(dialog).queryByRole("button", { name: "新建分组" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("分组列表失败保留凭证与其他入口，弹窗只提示局部错误和重试", async () => {
    setup(true, true);
    const button = await screen.findByTestId("credential-groups-miora");
    expect(button).toHaveTextContent("账号分组 —");
    expect(screen.getByTestId("open-add-dialog")).toBeInTheDocument();
    await userEvent.click(button);
    await screen.findByText("账号分组加载失败。");
    expect(screen.getByRole("button", { name: "重试分组列表" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建分组" })).not.toBeInTheDocument();
  });
});
