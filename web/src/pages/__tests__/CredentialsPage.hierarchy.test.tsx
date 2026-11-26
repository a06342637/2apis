import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import { makeCredential, mockCredentialsFetch, renderPage, settle, userEvent } from "@/test-utils";
import type { CredentialGroup } from "@/api/types";
import { vi } from "vitest";

const accountGroup = (id: string, provider: CredentialGroup["provider"], name: string, members: string[]): CredentialGroup =>
  ({ id, provider, name, credential_ids: members, member_count: members.length, created_at: 1, updated_at: 1 });

function setup(groups: CredentialGroup[], accounts = [
  makeCredential({ id: "mi-b", provider: "miora", nickname: "暂停号", enabled: 0 }),
  makeCredential({ id: "mi-a", provider: "miora", nickname: "主号" }),
  makeCredential({ id: "mi-free", provider: "miora", nickname: "未归组号" }),
  makeCredential({ id: "ca-a", provider: "codearts", nickname: "CodeArts 账号" }),
], write = true) {
  const spy = mockCredentialsFetch({
    "/api/credential-groups": { groups, is_operator: write },
    "/api/credentials/batch": { action: "toggle", total: 2, succeeded: 2, failed: 0, results: [] },
    "/api/credentials": { credentials: accounts, is_operator: write,
      expiry_window_seconds: 129600, expiry_secondary_window_seconds: 604800, token_expiry_warning_seconds: 3600 },
  });
  renderPage(<CredentialsPage />, write ? { username: "root", is_admin: true } : { username: "viewer", is_admin: false });
  return spy;
}

describe("渠道下的二级账号组", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("展开渠道显示自建组、空组和未分组，保留跨组会员", async () => {
    setup([accountGroup("production", "miora", "生产组", ["mi-a", "mi-b"]),
      accountGroup("shared", "miora", "共享组", ["mi-a"]), accountGroup("empty", "miora", "空组", [])]);
    await settle();
    expect(screen.queryByTestId("account-group-production")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    expect(await screen.findByTestId("account-group-production")).toHaveTextContent("生产组");
    expect(screen.getByTestId("account-group-empty")).toHaveTextContent("0 个账号");
    expect(screen.getByTestId("account-group-ungrouped-miora")).toHaveTextContent("未分组");
    expect(screen.getAllByTestId("row-mi-a")).toHaveLength(2);
    expect(screen.getByTestId("row-mi-free")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("account-group-toggle-production"));
    expect(screen.getByTestId("account-group-toggle-production")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getAllByTestId("row-mi-a")).toHaveLength(1);
    expect(screen.queryByTestId("row-mi-b")).not.toBeInTheDocument();
  });

  it("没有账号的渠道仍能显示已创建的空组", async () => {
    setup([accountGroup("office-empty", "officeace", "OfficeAce 空组", [])]);
    await settle();
    const toggle = await screen.findByTestId("group-toggle-officeace");
    await userEvent.click(toggle);
    expect(screen.getByTestId("account-group-office-empty")).toHaveTextContent("OfficeAce 空组");
    expect(screen.getByTestId("account-group-office-empty")).toHaveTextContent("0 个账号");
  });

  it("只读账号显示组名和数量，未下发成员时不把渠道账号误称未分组", async () => {
    const visible = accountGroup("readonly", "miora", "只读组", ["mi-a"]);
    delete visible.credential_ids;
    setup([visible], undefined, false);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    expect(await screen.findByTestId("account-group-readonly")).toHaveTextContent("只读组");
    expect(screen.getByTestId("account-group-readonly")).toHaveTextContent("1 个账号");
    expect(screen.getByTestId("row-mi-a")).toBeInTheDocument();
    expect(screen.queryByText("未分组")).not.toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    await waitFor(() => expect(screen.getByText("当前账号只能查看组名与成员数量。")).toBeInTheDocument());
  });

  it("跨组重复账号选择同步且批量提交只发送一次ID", async () => {
    const spy = setup([accountGroup("production", "miora", "生产组", ["mi-a", "mi-b"]),
      accountGroup("shared", "miora", "共享组", ["mi-a"])]);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("account-group-select-production"));
    expect(screen.getByTestId("account-group-select-shared")).toBeChecked();
    expect(screen.getAllByTestId("select-mi-a")).toHaveLength(2);
    for (const checkbox of screen.getAllByTestId("select-mi-a")) expect(checkbox).toBeChecked();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
    await userEvent.click(screen.getByTestId("batch-pause"));
    await waitFor(() => expect(spy.mock.calls.some(([url, init]) => String(url) === "/api/credentials/batch" && init?.method === "POST")).toBe(true));
    const batch = spy.mock.calls.find(([url, init]) => String(url) === "/api/credentials/batch" && init?.method === "POST")!;
    expect(JSON.parse(String(batch[1]?.body))).toMatchObject({ ids: ["mi-b", "mi-a"] });
  });

  it("二级组选择框靠前，只有真正归组的账号选择框缩进，未分组保持旧位置", async () => {
    setup([accountGroup("production", "miora", "生产组", ["mi-a"])]);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    const groupCheckbox = screen.getByTestId("account-group-select-production");
    expect(groupCheckbox.parentElement).not.toHaveClass("pl-4");
    expect(groupCheckbox.closest("td")).toHaveClass("px-2");
    expect(screen.getByTestId("select-mi-a").closest("td")).toHaveClass("pl-7");
    expect(screen.getByTestId("select-mi-free").closest("td")).not.toHaveClass("pl-7");
    expect(screen.getByTestId("select-mi-b").closest("td")).not.toHaveClass("pl-7");
  });

  it("筛选展开原先折叠的匹配二级组，并只选择当前匹配成员", async () => {
    setup([accountGroup("production", "miora", "生产组", ["mi-a", "mi-b"])]);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("account-group-toggle-production"));
    expect(screen.queryByTestId("row-mi-b")).not.toBeInTheDocument();
    fireEvent.change(screen.getByTestId("credential-search"), { target: { value: "暂停号" } });
    await waitFor(() => expect(screen.getByTestId("account-group-toggle-production")).toHaveAttribute("aria-expanded", "true"));
    expect(screen.getByTestId("row-mi-b")).toBeInTheDocument();
    expect(screen.queryByTestId("row-mi-a")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("account-group-select-production"));
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 1 个");
  });

  it("整站没有凭证时仍可展开保存的空渠道组，支持按渠道筛选", async () => {
    setup([accountGroup("office-empty", "officeace", "OfficeAce 空组", [])], []);
    await settle();
    expect(screen.queryByTestId("no-credentials")).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByTestId("credential-provider-filter"), "officeace");
    expect(await screen.findByTestId("account-group-office-empty")).toBeInTheDocument();
    expect(screen.getByTestId("account-group-empty-office-empty")).toHaveTextContent("此分组暂无账号");
  });

  it("Miora账号逐行展示真实套餐标识，免费仍保留非零积分余额", async () => {
    const now = Math.floor(Date.now() / 1000);
    const detail = (tier: string | null, status: string, vip: boolean | null, expires_at: number | null) =>
      ({ miora_subscription: { tier, status, vip, expires_at, probed_at: now } });
    setup([], [
      makeCredential({ id: "mi-free", provider: "miora", nickname: "免费账号", quota_remaining: 62, quota_total: 100,
        quota_detail: detail("free", "free", false, null) }),
      makeCredential({ id: "mi-vip", provider: "miora", nickname: "VIP账号", quota_detail: detail("standard", "active", true, now + 1000) }),
      makeCredential({ id: "mi-unknown", provider: "miora", nickname: "未获取账号", quota_detail: detail(null, "unknown", null, null) }),
      makeCredential({ id: "mi-expired", provider: "miora", nickname: "到期账号", quota_detail: detail("pro", "active", true, now - 1) }),
    ]);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    expect(screen.getByTestId("miora-plan-mi-free")).toHaveTextContent("免费");
    expect(screen.getByTestId("miora-plan-mi-free")).toHaveAttribute("title", expect.stringContaining("扣赠送积分"));
    expect(screen.getByTestId("row-mi-free")).toHaveTextContent("62 / 100");
    expect(screen.getByTestId("miora-plan-mi-vip")).toHaveTextContent("VIP · Standard");
    expect(screen.getByTestId("miora-plan-mi-unknown")).toHaveTextContent("套餐未获取");
    expect(screen.getByTestId("miora-plan-mi-expired")).toHaveTextContent("套餐已到期");
  });
});
