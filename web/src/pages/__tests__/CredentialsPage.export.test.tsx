import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialsPage } from "../CredentialsPage";
import type { CredentialGroup } from "@/api/types";
import { copyText } from "@/lib/clipboard";
import { jsonResponse, makeCredential, mockCredentialsFetch, renderPage, settle, userEvent } from "@/test-utils";

const ADMIN = { username: "root", is_admin: true } as const;
const OPERATOR = { username: "operator", is_admin: false, role: "operator" } as const;
const VIEWER = { username: "viewer", is_admin: false } as const;
const ENTRIES = [{ provider: "miora", nickname: "主号", tags: ["备用"],
  credential: { authToken: "synthetic", refreshToken: "synthetic-refresh" } }];
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
const accounts = [
  makeCredential({ id: "mi-a", provider: "miora", nickname: "主号" }),
  makeCredential({ id: "mi-b", provider: "miora", nickname: "备用号" }),
];
const createURL = vi.fn((_blob: Blob) => "blob:credential-export");
const revokeURL = vi.fn();
let downloads: { name: string; href: string; attached: boolean }[];

function setup({ write = true, groups = [], response = () => jsonResponse(ENTRIES),
  singleResponse = () => jsonResponse(ENTRIES[0]) }: {
  write?: boolean;
  groups?: CredentialGroup[];
  response?: () => Response | Promise<Response>;
  singleResponse?: () => Response | Promise<Response>;
} = {}, session: typeof ADMIN | typeof OPERATOR | typeof VIEWER = ADMIN) {
  const spy = mockCredentialsFetch({
    "/api/credentials/export": response,
    "/api/credentials/mi-a/export": singleResponse,
    "/api/credentials": { credentials: accounts, is_operator: write,
      expiry_window_seconds: 129600, expiry_secondary_window_seconds: 604800, token_expiry_warning_seconds: 3600 },
    "/api/credential-groups": { groups, is_operator: write },
  });
  renderPage(<CredentialsPage />, session);
  return spy;
}

async function chooseBatchExport(label = "下载 JSON") {
  await userEvent.click(screen.getByTestId("batch-export"));
  await userEvent.click(await screen.findByRole("menuitem", { name: label }));
}

async function openRowExport() {
  await userEvent.click(screen.getByTestId("actions-mi-a"));
  await userEvent.click(screen.getByRole("menuitem", { name: "导出凭证" }));
  await screen.findByRole("menuitem", { name: "下载 JSON" });
}

function exportCalls(spy: ReturnType<typeof mockCredentialsFetch>) {
  return spy.mock.calls.filter(([url]) => String(url) === "/api/credentials/export")
    .map(([, init]) => JSON.parse(String(init?.body)));
}

const group = (id: string, members: string[]): CredentialGroup => ({ id, name: id, provider: "miora",
  credential_ids: members, member_count: members.length, created_at: 1, updated_at: 1 });

describe("凭证批量导出", () => {
  beforeEach(() => {
    downloads = [];
    createURL.mockClear();
    revokeURL.mockClear();
    vi.mocked(copyText).mockReset().mockResolvedValue(true);
    const OriginalURL = globalThis.URL;
    class DownloadURL extends OriginalURL {
      static createObjectURL = createURL;
      static revokeObjectURL = revokeURL;
    }
    vi.stubGlobal("URL", DownloadURL);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push({ name: this.download, href: this.href, attached: document.body.contains(this) });
    });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("未选择时没有导出按钮，viewer没有选择及导出入口", async () => {
    setup({ write: false }, VIEWER);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    expect(screen.queryByTestId("select-mi-a")).not.toBeInTheDocument();
    expect(screen.queryByTestId("batch-export")).not.toBeInTheDocument();
    expect(screen.queryByTestId("actions-mi-a")).not.toBeInTheDocument();
  });

  it.each([ADMIN, OPERATOR])("$username 单选下载一次JSON且保留选择，不刷新列表", async (session) => {
    const spy = setup({}, session);
    await settle();
    expect(screen.queryByTestId("batch-export")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("select-mi-a"));
    const buttons = within(screen.getByTestId("credentials-batch-bar")).getAllByRole("button");
    expect(buttons.at(-3)).toBe(screen.getByTestId("batch-export"));
    expect(buttons.at(-2)).toBe(screen.getByTestId("batch-delete"));
    expect(screen.getByTestId("batch-export")).toHaveTextContent("批量导出凭证");
    expect(buttons.at(-1)).toBe(screen.getByTestId("batch-clear"));
    expect(screen.getByTestId("batch-export").querySelector("svg")).not.toBeNull();
    const listReads = spy.mock.calls.filter(([url]) => String(url).startsWith("/api/credentials?")).length;
    await chooseBatchExport();
    await screen.findByTestId("credentials-notice");
    expect(exportCalls(spy)).toEqual([{ ids: ["mi-a"] }]);
    expect(downloads).toEqual([{ name: expect.stringMatching(/^2apis-credentials-\d{8}-\d{6}\.json$/),
      href: "blob:credential-export", attached: true }]);
    expect(createURL).toHaveBeenCalledOnce();
    expect(createURL.mock.calls[0][0]).toBeInstanceOf(Blob);
    expect(JSON.parse(await createURL.mock.calls[0][0].text())).toEqual(ENTRIES);
    expect(revokeURL).toHaveBeenCalledExactlyOnceWith("blob:credential-export");
    expect(document.querySelector('a[download^="2apis-credentials-"]')).toBeNull();
    expect(screen.getByTestId("select-mi-a")).toBeChecked();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 1 个");
    expect(screen.getByTestId("batch-export")).toBeEnabled();
    expect(spy.mock.calls.filter(([url]) => String(url).startsWith("/api/credentials?")).length).toBe(listReads);
  });

  it("渠道全选下载全部账号到一个文件", async () => {
    const spy = setup();
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await chooseBatchExport();
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(exportCalls(spy)).toEqual([{ ids: ["mi-a", "mi-b"] }]);
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
  });

  it("跨组重复成员只导出一次，重复行的勾选状态保留", async () => {
    const spy = setup({ groups: [group("g-a", ["mi-a"]), group("g-b", ["mi-a", "mi-b"])] });
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("account-group-select-g-a"));
    await userEvent.click(screen.getByTestId("account-group-select-g-b"));
    expect(screen.getAllByTestId("select-mi-a")).toHaveLength(2);
    await chooseBatchExport();
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(exportCalls(spy)).toEqual([{ ids: ["mi-a", "mi-b"] }]);
    for (const checkbox of screen.getAllByTestId("select-mi-a")) expect(checkbox).toBeChecked();
    expect(screen.getByTestId("select-mi-b")).toBeChecked();
  });

  it("筛选变化仅导出当前可见勾选子集，清除筛选后原选择仍保留", async () => {
    const spy = setup();
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    fireEvent.change(screen.getByTestId("credential-search"), { target: { value: "主号" } });
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 1 个");
    await chooseBatchExport();
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(exportCalls(spy)).toEqual([{ ids: ["mi-a"] }]);
    fireEvent.change(screen.getByTestId("credential-search"), { target: { value: "" } });
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
  });

  it("导出期间禁止重复点击及其他批量动作，完成后恢复", async () => {
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    const spy = setup({ response: () => pending });
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await chooseBatchExport();
    for (const button of within(screen.getByTestId("credentials-batch-bar")).getAllByRole("button")) expect(button).toBeDisabled();
    await userEvent.click(screen.getByTestId("batch-export"));
    expect(exportCalls(spy)).toHaveLength(1);
    finish(jsonResponse(ENTRIES));
    await waitFor(() => expect(screen.getByTestId("batch-export")).toBeEnabled());
    expect(downloads).toHaveLength(1);
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
  });

  it("失败保留上游错误与选择，解除busy后可原样重试", async () => {
    let attempt = 0;
    const spy = setup({ response: () => ++attempt === 1
      ? jsonResponse({ error: { code: "export_too_large", message: "导出内容超过 16 MiB，请减少勾选" } }, 413)
      : jsonResponse(ENTRIES) });
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await chooseBatchExport();
    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("导出内容超过 16 MiB，请减少勾选");
    expect(createURL).not.toHaveBeenCalled();
    expect(screen.getByTestId("batch-export")).toBeEnabled();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
    await chooseBatchExport();
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(exportCalls(spy)).toEqual([{ ids: ["mi-a", "mi-b"] }, { ids: ["mi-a", "mi-b"] }]);
  });

  it("点击批量导出只打开二级选择，按 Escape 可取消且不读取凭证", async () => {
    const spy = setup();
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await userEvent.click(screen.getByTestId("batch-export"));
    expect(screen.getByRole("menuitem", { name: "下载 JSON" })).toBeVisible();
    expect(screen.getByRole("menuitem", { name: "复制到剪贴板" })).toBeVisible();
    expect(exportCalls(spy)).toEqual([]);
    expect(copyText).not.toHaveBeenCalled();
    expect(downloads).toHaveLength(0);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("menuitem", { name: "下载 JSON" })).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
  });

  it.each([ADMIN, OPERATOR])("$username 批量复制同一JSON且保留选择，不创建下载", async (session) => {
    const spy = setup({}, session);
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await chooseBatchExport("复制到剪贴板");
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("已复制到剪贴板");
    expect(exportCalls(spy)).toEqual([{ ids: ["mi-a", "mi-b"] }]);
    expect(copyText).toHaveBeenCalledOnce();
    expect(JSON.parse(vi.mocked(copyText).mock.calls[0][0])).toEqual(ENTRIES);
    expect(createURL).not.toHaveBeenCalled();
    expect(downloads).toHaveLength(0);
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
  });

  it("剪贴板失败明确提示且不假报成功，保留选择可重试下载", async () => {
    vi.mocked(copyText).mockResolvedValue(false);
    const spy = setup();
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await chooseBatchExport("复制到剪贴板");
    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("复制到剪贴板失败");
    expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
    expect(createURL).not.toHaveBeenCalled();
    expect(screen.getByTestId("batch-export")).toBeEnabled();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
    await chooseBatchExport();
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(JSON.parse(await createURL.mock.calls[0][0].text())).toEqual(ENTRIES);
    expect(exportCalls(spy)).toHaveLength(2);
  });

  it("复制遇到服务端拒绝不会复制错误体或创建文件", async () => {
    setup({ response: () => jsonResponse({ error: { message: "导出权限不足" } }, 403) });
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await chooseBatchExport("复制到剪贴板");
    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("导出权限不足");
    expect(copyText).not.toHaveBeenCalled();
    expect(createURL).not.toHaveBeenCalled();
    expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("batch-selected-count")).toHaveTextContent("已选 2 个");
  });

  it.each([ADMIN, OPERATOR])("$username 单行导出子菜单复制原始条目并提示成功", async (session) => {
    const spy = setup({}, session);
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await openRowExport();
    expect(spy.mock.calls.filter(([url]) => String(url).endsWith("/mi-a/export"))).toHaveLength(0);
    expect(copyText).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("menuitem", { name: "复制到剪贴板" }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("已复制到剪贴板");
    expect(spy.mock.calls.filter(([url]) => String(url).endsWith("/mi-a/export"))).toHaveLength(1);
    expect(copyText).toHaveBeenCalledOnce();
    expect(JSON.parse(vi.mocked(copyText).mock.calls[0][0])).toEqual(ENTRIES[0]);
    expect(createURL).not.toHaveBeenCalled();
    expect(screen.getByTestId("actions-mi-a")).toBeEnabled();
  });

  it("单行复制失败后可从同一子菜单下载JSON，下载清理对象URL", async () => {
    vi.mocked(copyText).mockResolvedValue(false);
    setup();
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await openRowExport();
    await userEvent.click(screen.getByRole("menuitem", { name: "复制到剪贴板" }));
    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("复制到剪贴板失败");
    expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("actions-mi-a")).toBeEnabled();
    await openRowExport();
    await userEvent.click(screen.getByRole("menuitem", { name: "下载 JSON" }));
    await waitFor(() => expect(downloads).toHaveLength(1));
    expect(downloads[0].name).toBe("coding2api-credential-miora-mi-a.json");
    expect(JSON.parse(await createURL.mock.calls[0][0].text())).toEqual(ENTRIES[0]);
    expect(revokeURL).toHaveBeenCalledOnce();
  });

  it("单行服务端拒绝复制时不会把错误体写入剪贴板", async () => {
    setup({ singleResponse: () => jsonResponse({ error: { message: "凭证不存在" } }, 400) });
    await settle();
    await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await openRowExport();
    await userEvent.click(screen.getByRole("menuitem", { name: "复制到剪贴板" }));
    expect(await screen.findByTestId("credentials-error")).toHaveTextContent("凭证不存在");
    expect(copyText).not.toHaveBeenCalled();
    expect(createURL).not.toHaveBeenCalled();
    expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
    expect(screen.getByTestId("actions-mi-a")).toBeEnabled();
  });

  it("剪贴板写入完成前保持busy，不能把旧剪贴板内容当成复制成功", async () => {
    let finish!: (value: boolean) => void;
    vi.mocked(copyText).mockReturnValue(new Promise<boolean>((resolve) => { finish = resolve; }));
    setup();
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await chooseBatchExport("复制到剪贴板");
    await waitFor(() => expect(copyText).toHaveBeenCalledOnce());
    expect(screen.getByTestId("batch-export")).toBeDisabled();
    expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
    finish(true);
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("已复制到剪贴板");
    expect(screen.getByTestId("batch-export")).toBeEnabled();
  });
});
