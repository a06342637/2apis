import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../../api/client";
import * as groupApi from "../../api/credentialGroups";
import type { CredentialGroup } from "../../api/types";
import { BatchImportDialog } from "../BatchImportDialog";

const GROUP: CredentialGroup = { id: "g1", provider: "miora", name: "生产组", created_at: 1,
  updated_at: 10, member_count: 1, credential_ids: ["old"] };
const CA_GROUP: CredentialGroup = { ...GROUP, id: "g2", provider: "codearts", name: "华为组", credential_ids: ["ca-old"] };
let groups: CredentialGroup[];

async function setup(text = '{"authToken":"synthetic-one"}') {
  const changed = vi.fn();
  const imported = vi.fn();
  render(<BatchImportDialog onClose={vi.fn()} onNotice={vi.fn()} onImported={imported} onGroupsChanged={changed} />);
  await waitFor(() => expect(screen.getByTestId("batch-group-miora")).toBeEnabled());
  fireEvent.change(screen.getByTestId("batch-text"), { target: { value: text } });
  return { changed, imported };
}

describe("批量导入分组", () => {
  beforeEach(() => {
    groups = [GROUP, CA_GROUP];
    vi.spyOn(groupApi, "listCredentialGroups").mockImplementation(async () => ({ groups, is_operator: true }));
    vi.spyOn(groupApi, "createCredentialGroup").mockImplementation(async payload => {
      const group = { ...payload, id: "new-group", created_at: 1, updated_at: 20, member_count: payload.credential_ids.length };
      groups = [...groups, group]; return group;
    });
    vi.spyOn(groupApi, "updateCredentialGroup").mockImplementation(async (id, payload) => {
      const group = { ...groups.find(item => item.id === id)!, ...payload, updated_at: 30 };
      group.member_count = group.credential_ids!.length;
      groups = groups.map(item => item.id === id ? group : item); return group;
    });
    vi.spyOn(groupApi, "deleteCredentialGroup").mockImplementation(async id => {
      groups = groups.filter(item => item.id !== id); return { ok: true };
    });
    vi.spyOn(api, "batchImportCredentials").mockResolvedValue({
      created: [{ index: 0, id: "new-account", tos: { level: "green", reason: "", basis: "" } }],
      skipped: [], failed: [],
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("默认不分组，选择已有组时追加最新成员并保留其他渠道", async () => {
    const { changed } = await setup();
    expect(screen.getByTestId("batch-group-miora")).toHaveValue("");
    expect(screen.getByTestId("batch-group-miora")).toHaveTextContent("生产组（1 个账号）");
    expect(screen.getByTestId("batch-group-miora")).not.toHaveTextContent("华为组");
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "g1");
    groups = [{ ...GROUP, updated_at: 15, credential_ids: ["old", "concurrent-member"] }, CA_GROUP];
    await userEvent.click(screen.getByTestId("batch-submit"));
    await waitFor(() => expect(groupApi.updateCredentialGroup).toHaveBeenCalledOnce());
    expect(groupApi.updateCredentialGroup).toHaveBeenCalledWith("g1", {
      credential_ids: ["old", "concurrent-member", "new-account"], expected_updated_at: 15,
    });
    expect(groups[1]).toEqual(CA_GROUP);
    expect(changed).toHaveBeenCalledOnce();
  });

  it("导入成功后新建分组，只包含成功项；同批失败和跳过不加入", async () => {
    vi.mocked(api.batchImportCredentials).mockResolvedValue({
      created: [{ index: 0, id: "new-account", tos: { level: "green", reason: "", basis: "" } }],
      skipped: [{ index: 1, reason: "duplicate" }], failed: [{ index: 2, reason: "invalid" }],
    });
    await setup(['synthetic-one', 'synthetic-two', 'synthetic-three'].map(authToken => JSON.stringify({ authToken })).join('\n'));
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "new");
    expect(screen.getByTestId("batch-submit")).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Miora 新分组名称"), { target: { value: " 新批次 " } });
    await userEvent.click(screen.getByTestId("batch-submit"));
    await waitFor(() => expect(groupApi.createCredentialGroup).toHaveBeenCalledWith({
      provider: "miora", name: "新批次", credential_ids: ["new-account"],
    }));
    expect((screen.getByTestId("batch-text") as HTMLTextAreaElement).value).toContain("synthetic-three");
  });

  it("可立即创建空组、查看、改名和确认删除，取消删除不发送请求", async () => {
    await setup();
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "new");
    fireEvent.change(screen.getByLabelText("Miora 新分组名称"), { target: { value: "新组" } });
    await userEvent.click(screen.getByRole("button", { name: "立即创建" }));
    await waitFor(() => expect(screen.getByTestId("batch-group-miora")).toHaveValue("new-group"));
    expect(groupApi.createCredentialGroup).toHaveBeenCalledWith({ provider: "miora", name: "新组", credential_ids: [] });
    await userEvent.click(screen.getByRole("button", { name: "修改名称" }));
    fireEvent.change(screen.getByLabelText("Miora 分组名称"), { target: { value: "改名组" } });
    await userEvent.click(screen.getByRole("button", { name: "保存名称" }));
    await waitFor(() => expect(screen.getByTestId("batch-group-miora")).toHaveTextContent("改名组（0 个账号）"));
    expect(groupApi.updateCredentialGroup).toHaveBeenCalledWith("new-group", { name: "改名组", expected_updated_at: 20 });
    await userEvent.click(screen.getByRole("button", { name: "删除分组" }));
    await userEvent.click(screen.getByRole("button", { name: "取消删除" }));
    expect(groupApi.deleteCredentialGroup).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "删除分组" }));
    await userEvent.click(screen.getByRole("button", { name: "确认删除分组" }));
    await waitFor(() => expect(screen.getByTestId("batch-group-miora")).toHaveValue(""));
    expect(groupApi.deleteCredentialGroup).toHaveBeenCalledWith("new-group");
    expect(groupApi.updateCredentialGroup).toHaveBeenCalledOnce();
  });

  it("分组冲突可只重试分配，凭证不重复导入，也不覆盖其他成员", async () => {
    vi.mocked(groupApi.updateCredentialGroup).mockRejectedValueOnce(new ApiError(409, "conflict", "分组已修改"));
    await setup();
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "g1");
    await userEvent.click(screen.getByTestId("batch-submit"));
    await screen.findByRole("button", { name: "重试分组分配" });
    expect(screen.getByTestId("batch-group-error")).toHaveTextContent("账号已导入，分组分配未完成");
    expect(screen.getByTestId("batch-submit")).toBeDisabled();
    groups = [{ ...GROUP, updated_at: 18, credential_ids: ["old", "another"] }, CA_GROUP];
    await userEvent.click(screen.getByRole("button", { name: "重试分组分配" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "重试分组分配" })).not.toBeInTheDocument());
    expect(api.batchImportCredentials).toHaveBeenCalledOnce();
    expect(groupApi.updateCredentialGroup).toHaveBeenLastCalledWith("g1", {
      credential_ids: ["old", "another", "new-account"], expected_updated_at: 18,
    });
  });

  it("混合渠道分别分配，成功组不随失败组重试", async () => {
    vi.mocked(api.batchImportCredentials).mockResolvedValue({ created: [
      { index: 0, id: "mi-new", tos: { level: "green", reason: "", basis: "" } },
      { index: 1, id: "ca-new", tos: { level: "green", reason: "", basis: "" } },
    ], skipped: [], failed: [] });
    vi.mocked(groupApi.createCredentialGroup).mockRejectedValueOnce(new Error("创建失败"));
    await setup('{"authToken":"synthetic-mi"}\n{"provider":"codearts","credential":{"access_key_id":"synthetic-ak","secret_access_key":"synthetic-sk"}}');
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "g1");
    await userEvent.selectOptions(screen.getByTestId("batch-group-codearts"), "new");
    fireEvent.change(screen.getByLabelText("CodeArts 新分组名称"), { target: { value: "华为新组" } });
    await userEvent.click(screen.getByTestId("batch-submit"));
    await screen.findByRole("button", { name: "重试分组分配" });
    expect(groupApi.updateCredentialGroup).toHaveBeenCalledWith("g1", { credential_ids: ["old", "mi-new"], expected_updated_at: 10 });
    await userEvent.click(screen.getByRole("button", { name: "重试分组分配" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "重试分组分配" })).not.toBeInTheDocument());
    expect(groupApi.updateCredentialGroup).toHaveBeenCalledOnce();
    expect(groupApi.createCredentialGroup).toHaveBeenLastCalledWith({ provider: "codearts", name: "华为新组", credential_ids: ["ca-new"] });
  });

  it("列表读取失败仍可不分组导入，刷新恢复；同名新组阻止提交", async () => {
    vi.mocked(groupApi.listCredentialGroups).mockRejectedValueOnce(new Error("offline"));
    render(<BatchImportDialog onClose={vi.fn()} onImported={vi.fn()} onNotice={vi.fn()} />);
    await screen.findByTestId("batch-group-error");
    fireEvent.change(screen.getByTestId("batch-text"), { target: { value: '{"authToken":"synthetic-one"}' } });
    expect(screen.getByTestId("batch-submit")).toBeEnabled();
    await userEvent.click(screen.getByRole("button", { name: "刷新分组" }));
    await waitFor(() => expect(screen.getByTestId("batch-group-miora")).toBeEnabled());
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "new");
    fireEvent.change(screen.getByLabelText("Miora 新分组名称"), { target: { value: "生产组" } });
    expect(screen.getByTestId("batch-submit")).toBeDisabled();
    expect(groupApi.createCredentialGroup).not.toHaveBeenCalled();
  });

  it("分组删除后分配失败可放弃，账号保留且不发删除账号请求", async () => {
    await setup();
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "g1");
    groups = [CA_GROUP];
    await userEvent.click(screen.getByTestId("batch-submit"));
    await screen.findByRole("button", { name: "重试分组分配" });
    expect(screen.getByTestId("batch-group-error")).toHaveTextContent("分组已删除或无法读取成员");
    await userEvent.click(screen.getByRole("button", { name: "保留账号，放弃分组分配" }));
    expect(screen.queryByRole("button", { name: "重试分组分配" })).not.toBeInTheDocument();
    expect(groupApi.deleteCredentialGroup).not.toHaveBeenCalled();
    expect(groupApi.updateCredentialGroup).not.toHaveBeenCalled();
    expect(api.batchImportCredentials).toHaveBeenCalledOnce();
  });

  it("没有成功条目不创建新组，改名失败保留草稿并可取消", async () => {
    vi.mocked(api.batchImportCredentials).mockResolvedValue({ created: [], skipped: [{ index: 0, reason: "duplicate" }], failed: [] });
    await setup();
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "new");
    fireEvent.change(screen.getByLabelText("Miora 新分组名称"), { target: { value: "空批次" } });
    await userEvent.click(screen.getByTestId("batch-submit"));
    await waitFor(() => expect(api.batchImportCredentials).toHaveBeenCalledOnce());
    expect(groupApi.createCredentialGroup).not.toHaveBeenCalled();
    await userEvent.selectOptions(screen.getByTestId("batch-group-miora"), "g1");
    await userEvent.click(screen.getByRole("button", { name: "修改名称" }));
    fireEvent.change(screen.getByLabelText("Miora 分组名称"), { target: { value: "修改草稿" } });
    vi.mocked(groupApi.updateCredentialGroup).mockRejectedValueOnce(new ApiError(409, "conflict", "名称已被修改"));
    await userEvent.click(screen.getByRole("button", { name: "保存名称" }));
    await screen.findByTestId("batch-group-error");
    expect(screen.getByLabelText("Miora 分组名称")).toHaveValue("修改草稿");
    await userEvent.click(screen.getByRole("button", { name: "取消修改" }));
    expect(screen.queryByLabelText("Miora 分组名称")).not.toBeInTheDocument();
  });
});
