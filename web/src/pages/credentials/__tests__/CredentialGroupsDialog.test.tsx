import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, makeCredential } from "@/test-utils";
import type { Credential, CredentialGroup } from "@/api/types";
import { CredentialGroupsDialog } from "../CredentialGroupsDialog";

const GROUP: CredentialGroup = { id: "g1", provider: "miora", name: "生产组", created_at: 1,
  updated_at: 10, member_count: 1, credential_ids: ["mi1"] };
const OTHER: CredentialGroup = { ...GROUP, id: "g2", provider: "codearts", name: "其他渠道" };
const accounts = [
  makeCredential({ id: "mi1", provider: "miora", nickname: "主号" }),
  makeCredential({ id: "mi2", provider: "miora", nickname: "暂停号", enabled: 0 }),
  makeCredential({ id: "mi3", provider: "miora", nickname: "禁用号", disabled: 1 }),
  makeCredential({ id: "ca1", provider: "codearts", nickname: "跨渠道号" }),
] as Credential[];

function setup(overrides: Partial<React.ComponentProps<typeof CredentialGroupsDialog>> = {}) {
  const changed = vi.fn(async () => undefined);
  const close = vi.fn();
  const retry = vi.fn(async () => [GROUP]);
  const view = render(<CredentialGroupsDialog provider="miora" label="Miora" credentials={accounts}
    groups={[GROUP, OTHER]} loading={false} canWrite onRetry={retry} onChanged={changed} onClose={close}
    {...overrides} />);
  return { ...view, changed, close, retry };
}

async function click(name: string) {
  await act(async () => fireEvent.click(screen.getByRole("button", { name })));
}

describe("渠道账号分组", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(GROUP))));
  afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("仅显示当前渠道，readonly只显示名称和数量", () => {
    setup({ canWrite: false, groups: [{ ...GROUP, credential_ids: undefined }, OTHER] });
    expect(screen.getByRole("dialog")).toHaveTextContent("生产组");
    expect(screen.getByRole("dialog")).toHaveTextContent("1 个账号");
    expect(screen.queryByText("其他渠道")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "新建分组" })).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("新建组只发同渠道成员，暂停/禁用可选，组provider固定", async () => {
    const { changed } = setup();
    await click("新建分组");
    expect(screen.getByRole("button", { name: "保存分组" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("分组名称"), { target: { value: " 测试组 " } });
    expect(screen.queryByTestId("group-edit-credential-ca1")).not.toBeInTheDocument();
    expect(screen.getByText("已暂停")).toBeInTheDocument();
    expect(screen.getByText("已禁用")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("group-edit-credential-mi2"));
    fireEvent.click(screen.getByTestId("group-edit-credential-mi3"));
    await click("保存分组");
    expect(fetch).toHaveBeenCalledWith("/api/credential-groups", expect.objectContaining({
      method: "POST", body: JSON.stringify({ provider: "miora", name: "测试组", credential_ids: ["mi2", "mi3"] }),
      credentials: "same-origin", headers: expect.objectContaining({ "X-Requested-With": "XMLHttpRequest" }),
    }));
    expect(changed).toHaveBeenCalledOnce();
    expect(screen.queryByLabelText("分组名称")).not.toBeInTheDocument();
  });

  it("空组可创建，编辑清空成员不会代表全部账号", async () => {
    setup();
    await click("新建分组");
    fireEvent.change(screen.getByLabelText("分组名称"), { target: { value: "空组" } });
    expect(screen.getByText("当前分组无成员")).toBeInTheDocument();
    await click("保存分组");
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body))).toMatchObject({ credential_ids: [] });
    fireEvent.click(screen.getByTestId("edit-group-g1"));
    await click("清空组成员");
    await click("保存分组");
    const [url, init] = vi.mocked(fetch).mock.calls[1];
    expect(url).toBe("/api/credential-groups/g1");
    expect(JSON.parse(String(init?.body))).toEqual({ name: "生产组", credential_ids: [], expected_updated_at: 10 });
  });

  it("重命名沿用稳定ID，乐观锁冲突留草稿，显式载入后用新版本保存", async () => {
    const spy = vi.fn().mockResolvedValueOnce(jsonResponse({ error: { code: "conflict", message: "changed" } }, 409))
      .mockResolvedValueOnce(jsonResponse(GROUP));
    vi.stubGlobal("fetch", spy);
    const latest = { ...GROUP, name: "他人名称", updated_at: 11, credential_ids: ["mi2"] };
    const retry = vi.fn(async () => [latest]);
    const { changed } = setup({ onRetry: retry });
    fireEvent.click(screen.getByTestId("edit-group-g1"));
    fireEvent.change(screen.getByLabelText("分组名称"), { target: { value: "本地草稿" } });
    await click("保存分组");
    expect(screen.getByRole("alert")).toHaveTextContent("分组已被修改");
    expect(screen.getByLabelText("分组名称")).toHaveValue("本地草稿");
    expect(changed).not.toHaveBeenCalled();
    await click("重新载入此组");
    expect(screen.getByLabelText("分组名称")).toHaveValue("他人名称");
    expect(screen.getByTestId("group-edit-credential-mi2")).toBeChecked();
    fireEvent.change(screen.getByLabelText("分组名称"), { target: { value: "重命名后" } });
    await click("保存分组");
    expect(spy.mock.calls[1][0]).toBe("/api/credential-groups/g1");
    expect(JSON.parse(spy.mock.calls[1][1].body)).toEqual({ name: "重命名后", credential_ids: ["mi2"], expected_updated_at: 11 });
  });

  it("删除需要确认，失败只在弹窗提示，可取消不影响分组", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { code: "forbidden", message: "无权删除分组" } }, 403)));
    const { changed } = setup();
    fireEvent.click(screen.getByTestId("delete-group-g1"));
    expect(screen.getByText(/绑定此组的 API Key 需要重新调整范围/)).toBeInTheDocument();
    await click("取消删除");
    expect(fetch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("delete-group-g1"));
    await click("确认删除分组");
    expect(screen.getByRole("alert")).toHaveTextContent("无权删除分组");
    expect(changed).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledWith("/api/credential-groups/g1", expect.objectContaining({ method: "DELETE" }));
  });

  it("删除成功刷新目录，Esc/关闭不写后端", async () => {
    const { changed, close } = setup();
    fireEvent.click(screen.getByTestId("delete-group-g1"));
    await click("确认删除分组");
    expect(changed).toHaveBeenCalledOnce();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(close).toHaveBeenCalledOnce();
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("组成员缺失时禁止悄悄覆盖成空组，列表失败有局部重试", async () => {
    const view = setup({ groups: [{ ...GROUP, credential_ids: undefined }] });
    fireEvent.click(screen.getByTestId("edit-group-g1"));
    expect(screen.getByRole("alert")).toHaveTextContent("无法读取组成员");
    expect(screen.queryByLabelText("分组名称")).not.toBeInTheDocument();
    view.unmount();
    const { retry } = setup({ groups: undefined });
    expect(screen.getByRole("alert")).toHaveTextContent("账号分组加载失败");
    await click("重试分组列表");
    expect(retry).toHaveBeenCalledOnce();
    expect(fetch).not.toHaveBeenCalled();
  });

  it("有已删除或跨渠道成员时阻止保存，必须明确移除", async () => {
    setup({ groups: [{ ...GROUP, credential_ids: ["gone", "ca1"], member_count: 2 }] });
    fireEvent.click(screen.getByTestId("edit-group-g1"));
    expect(screen.getByTestId("group-edit-credential-gone")).toBeChecked();
    expect(screen.getByTestId("group-edit-credential-ca1")).toBeChecked();
    expect(screen.getByRole("button", { name: "保存分组" })).toBeDisabled();
    fireEvent.click(screen.getByTestId("group-edit-credential-gone"));
    fireEvent.click(screen.getByTestId("group-edit-credential-ca1"));
    expect(screen.getByRole("button", { name: "保存分组" })).toBeEnabled();
    await click("保存分组");
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[0][1]?.body)).credential_ids).toEqual([]);
  });

  it("冲突重新载入的晚到结果不能重新打开已取消的编辑", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { code: "conflict", message: "changed" } }, 409)));
    let finish!: (groups: CredentialGroup[]) => void;
    const pending = new Promise<CredentialGroup[]>((done) => { finish = done; });
    setup({ onRetry: () => pending });
    fireEvent.click(screen.getByTestId("edit-group-g1"));
    await click("保存分组");
    fireEvent.click(screen.getByRole("button", { name: "重新载入此组" }));
    fireEvent.click(screen.getByRole("button", { name: "取消编辑" }));
    await act(async () => finish([{ ...GROUP, name: "Latest" }]));
    expect(screen.queryByLabelText("分组名称")).not.toBeInTheDocument();
  });
});
