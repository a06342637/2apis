import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import type { CredentialGroup } from "@/api/types";
import { jsonResponse, makeCredential, mockCredentialsFetch, renderPage, settle, userEvent } from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;
const VIEWER = { username: "viewer", is_admin: false } as const;
const ACCOUNTS = [makeCredential({ id: "mi-a", provider: "miora", nickname: "主号" }),
  makeCredential({ id: "mi-b", provider: "miora", nickname: "备用号" })];
function success(ids: string[]) {
  return jsonResponse({ action: "delete", total: ids.length, succeeded: ids.length, failed: 0,
    results: ids.map((id) => ({ id, ok: true })) });
}
function setup(response: () => Response | Promise<Response>, {
  write = true, groups = [],
}: { write?: boolean; groups?: CredentialGroup[] } = {}, session: typeof ADMIN | typeof VIEWER = ADMIN) {
  const spy = mockCredentialsFetch({ "/api/credentials/batch": response,
    "/api/credentials": { credentials: ACCOUNTS, is_operator: write },
    "/api/credential-groups": { groups, is_operator: write } });
  renderPage(<CredentialsPage />, session);
  return spy;
}
function requests(spy: ReturnType<typeof setup>) {
  return spy.mock.calls.filter(([url]) => String(url) === "/api/credentials/batch")
    .map(([, init]) => JSON.parse(String(init?.body)));
}
async function confirm() {
  await userEvent.click(screen.getByTestId("batch-delete"));
  await userEvent.click(screen.getByTestId("batch-delete-confirm"));
}

describe("凭证批量删除确认", () => {
  it("viewer没有选择、批量导出或批量删除入口", async () => {
    const spy = setup(() => success([]), { write: false }, VIEWER);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    expect(screen.queryByTestId("select-mi-a")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-delete")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-export")).not.toBeInTheDocument();
    expect(requests(spy)).toEqual([]);
  });
  it("批量栏明确显示两入口，删除需确认数量，取消不调用API并保留选择", async () => {
    const spy = setup(() => success(["mi-a", "mi-b"]));
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    expect(screen.getByTestId("batch-export")).toHaveTextContent("批量导出凭证");
    expect(screen.getByTestId("batch-delete")).toHaveTextContent("批量删除");
    await userEvent.click(screen.getByTestId("batch-delete"));
    expect(screen.getByTestId("batch-delete-question")).toHaveTextContent("确认删除选中的 2 个凭证");
    expect(screen.getByTestId("batch-delete-confirm")).toHaveTextContent("确认删除 2 个凭证");
    expect(requests(spy)).toEqual([]);
    await userEvent.click(screen.getByTestId("batch-delete-cancel"));
    expect(screen.queryByTestId("batch-delete-confirm")).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
    expect(requests(spy)).toEqual([]);
  });
  it.each(["account", "channel"])("%s 选择确认后只删除选中项，成功清理选择", async (selection) => {
    const ids = selection === "account" ? ["mi-a"] : ["mi-a", "mi-b"];
    const spy = setup(() => success(ids));
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId(selection === "account" ? "select-mi-a" : "group-select-miora"));
    await userEvent.click(screen.getByTestId("batch-delete"));
    expect(screen.getByTestId("batch-delete-confirm")).toHaveTextContent(`确认删除 ${ids.length} 个凭证`);
    await userEvent.click(screen.getByTestId("batch-delete-confirm"));
    await screen.findByTestId("credentials-batch-result");
    expect(requests(spy)).toEqual([{ action: "delete", ids }]);
    expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument();
  });
  it("跨组重复账号去重，按选中账号而非分组调用删除", async () => {
    const group = (id: string, credential_ids: string[]): CredentialGroup => ({ id, name: id,
      provider: "miora", credential_ids, member_count: credential_ids.length, created_at: 1, updated_at: 1 });
    const spy = setup(() => success(["mi-a", "mi-b"]), {
      groups: [group("g-a", ["mi-a"]), group("g-b", ["mi-a", "mi-b"])] });
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("account-group-select-g-a"));
    await userEvent.click(screen.getByTestId("account-group-select-g-b"));
    await confirm();
    await screen.findByTestId("credentials-batch-result");
    expect(requests(spy)).toEqual([{ action: "delete", ids: ["mi-a", "mi-b"] }]);
  });
  it("部分成功只清掉成功项，失败原因可见且重试只提交失败项", async () => {
    let calls = 0;
    const spy = setup(() => ++calls === 1 ? jsonResponse({ action: "delete", total: 2, succeeded: 1,
      failed: 1, results: [{ id: "mi-a", ok: true }, { id: "mi-b", ok: false, error: "action_failed" }] })
      : success(["mi-b"]));
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await confirm();
    expect(await screen.findByTestId("credentials-batch-result")).toHaveTextContent("成功 1 / 2，失败 1");
    expect(screen.getByTestId("batch-failure")).toHaveTextContent("备用号：执行失败");
    await waitFor(() => expect(screen.getByTestId("batch-delete")).toBeEnabled());
    expect(screen.getByTestId("select-mi-a")).not.toBeChecked();
    expect(screen.getByTestId("select-mi-b")).toBeChecked();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 1 个");
    await confirm();
    await waitFor(() => expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument());
    expect(requests(spy)).toEqual([{ action: "delete", ids: ["mi-a", "mi-b"] }, { action: "delete", ids: ["mi-b"] }]);
  });
  it("请求失败保留全部选择及确认重试，不能显示删除成功", async () => {
    let calls = 0;
    const spy = setup(() => ++calls === 1 ? jsonResponse({ error: { message: "最多删除 200 个凭证" } }, 400)
      : success(["mi-a", "mi-b"]));
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await confirm();
    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("最多删除 200 个凭证");
    expect(screen.queryByTestId("credentials-batch-result")).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
    expect(screen.getByTestId("batch-delete-confirm")).toBeEnabled();
    await userEvent.click(screen.getByTestId("batch-delete-confirm"));
    await screen.findByTestId("credentials-batch-result");
    expect(requests(spy)).toHaveLength(2);
  });
  it("筛选隐藏的选中项不会删除，也不会被成功结果清掉选择", async () => {
    const spy = setup(() => success(["mi-a"]));
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    fireEvent.change(screen.getByTestId("credential-search"), { target: { value: "主号" } });
    await confirm();
    await screen.findByTestId("credentials-batch-result");
    expect(requests(spy)).toEqual([{ action: "delete", ids: ["mi-a"] }]);
    fireEvent.change(screen.getByTestId("credential-search"), { target: { value: "" } });
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 1 个");
  });
  it("确认后更改选择或筛选撤销旧确认，避免删除范围悄然改变", async () => {
    const spy = setup(() => success([]));
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await userEvent.click(screen.getByTestId("batch-delete"));
    await userEvent.click(screen.getByTestId("select-mi-b"));
    expect(screen.queryByTestId("batch-delete-confirm")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("batch-delete"));
    fireEvent.change(screen.getByTestId("credential-search"), { target: { value: "主号" } });
    expect(screen.queryByTestId("batch-delete-confirm")).not.toBeInTheDocument();
    expect(requests(spy)).toEqual([]);
  });
  it("删除期间所有批量按钮禁用，重复确认不提交第二次", async () => {
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    const spy = setup(() => pending);
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await confirm();
    for (const button of within(screen.getByTestId("credentials-batch-bar")).getAllByRole("button")) {
      expect(button).toBeDisabled();
    }
    await userEvent.click(screen.getByTestId("batch-delete-confirm"));
    expect(requests(spy)).toHaveLength(1);
    finish(success(["mi-a", "mi-b"]));
    await screen.findByTestId("credentials-batch-result");
    expect(screen.queryByTestId("credentials-batch-bar")).not.toBeInTheDocument();
  });
});
