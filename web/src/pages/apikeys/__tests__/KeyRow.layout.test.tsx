import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { formatTime } from "@/api/display";
import type { ApiKey, CredentialGroup } from "@/api/types";
import { KeyRow } from "../KeyRow";

const item: ApiKey = {
  id: "layout-key", username: "root", name: "生产环境客户端访问 Key",
  preview: "sk-…abcd", can_reveal: true, created_at: 1_700_000_000,
  last_used_at: 1_700_000_100, expires_at: 1_800_000_000, provider_binding: "miora",
  allowed_ips: "192.168.0.0/16,2001:db8:1234:5678::/64",
  allowed_models: "miora-claude-*,miora-gemini-*,miora-gpt-*",
  group_ids: ["production", "missing-group"],
};
const group: CredentialGroup = {
  id: "production", name: "负责生产环境与长期运行客户端的 Miora 账号分组",
  provider: "miora", created_at: 1, updated_at: 1, member_count: 10,
};

function setup(overrides: Partial<ApiKey> = {}, confirming = false) {
  const callbacks = { onRemove: vi.fn(), onCancelDelete: vi.fn(), onEdit: vi.fn(),
    onEditQuota: vi.fn(), onImport: vi.fn(), onConfirmDelete: vi.fn() };
  const row = { ...item, ...overrides, quota: JSON.stringify({ daily_requests: 100, rpm: 5 }),
    usage: { requests: 1234, tokens: 15000, cost_usd: 1.25 } };
  render(<table><tbody><KeyRow item={row} accountGroups={[group]} window="7d"
    confirming={confirming} {...callbacks} /></tbody></table>);
  return { row, ...callbacks };
}

describe("API Key 分组布局", () => {
  it("五个单元格保留完整名称、权限、分组和时间元数据", () => {
    setup();
    expect(screen.getAllByRole("cell")).toHaveLength(5);
    expect(screen.getByTestId("key-name-layout-key")).toHaveTextContent(item.name);
    expect(screen.getByTestId("key-preview-layout-key")).toHaveTextContent(item.preview);
    expect(screen.getByTestId("key-binding-layout-key")).toHaveTextContent("Miora");
    expect(screen.getByTestId("key-credentials-layout-key")).toHaveTextContent(
      `按 2 个分组：${group.name}、missing-group`);
    expect(screen.getByTestId("key-ips-layout-key")).toHaveTextContent(item.allowed_ips);
    expect(screen.getByTestId("key-models-layout-key")).toHaveTextContent(item.allowed_models);
    expect(screen.getByTestId("key-quota-layout-key")).toHaveTextContent("100 次/日 · 5 RPM");
    expect(screen.getByTestId("key-usage-layout-key")).toHaveTextContent("1,234 次");
    expect(screen.getByTestId("key-expires-layout-key")).toHaveTextContent(formatTime(item.expires_at!));
    expect(screen.getByTestId("key-last-used-layout-key")).toHaveTextContent(formatTime(item.last_used_at!));
    expect(screen.getByTestId("key-created-layout-key")).toHaveTextContent(formatTime(item.created_at));
    expect(screen.getAllByRole("cell").map((cell) => cell.dataset.label)).toEqual(
      ["Key", "渠道与范围", "权限与配额", "用量 · 近 7 天", "操作"]);
  });

  it("图标按钮有可访问名称、悬停文案，并将当前行传给原有操作", () => {
    const callbacks = setup();
    const actions = within(screen.getByRole("group", { name: "Key 操作" }));
    expect(actions.getAllByRole("button")).toHaveLength(4);
    const edit = actions.getByRole("button", { name: "编辑" });
    expect(edit).toHaveAttribute("title", "编辑 Key");
    fireEvent.click(edit);
    fireEvent.click(actions.getByRole("button", { name: "配额" }));
    fireEvent.click(actions.getByRole("button", { name: "导入 CC Switch" }));
    fireEvent.click(actions.getByRole("button", { name: "删除" }));
    expect(callbacks.onEdit).toHaveBeenCalledWith(callbacks.row);
    expect(callbacks.onEditQuota).toHaveBeenCalledWith(callbacks.row);
    expect(callbacks.onImport).toHaveBeenCalledWith(callbacks.row);
    expect(callbacks.onConfirmDelete).toHaveBeenCalledWith(item.id);
    expect(callbacks.onRemove).not.toHaveBeenCalled();
  });

  it("删除确认仍显示文字按钮，确认与取消分别调用原有回调", () => {
    const callbacks = setup({}, true);
    expect(screen.queryByRole("group", { name: "Key 操作" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认删除" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(callbacks.onRemove).toHaveBeenCalledWith(item.id);
    expect(callbacks.onCancelDelete).toHaveBeenCalledOnce();
  });

  it("空名称和默认策略清楚展示，指定账号数量继续保留", () => {
    setup({ name: "", group_ids: [], credential_ids: ["one", "two"], expires_at: null,
      last_used_at: null, allowed_ips: "", allowed_models: "" });
    expect(screen.getByTestId("key-name-layout-key")).toHaveTextContent("未命名 Key");
    expect(screen.getByTestId("key-credentials-layout-key")).toHaveTextContent("指定 2 个账号");
    expect(screen.getByTestId("key-expires-layout-key")).toHaveTextContent("永不过期");
    expect(screen.getByTestId("key-last-used-layout-key")).toHaveTextContent("从未使用");
    expect(screen.getByTestId("key-ips-layout-key")).toHaveTextContent("不限制");
    expect(screen.getByTestId("key-models-layout-key")).toHaveTextContent("不限制");
  });
});
