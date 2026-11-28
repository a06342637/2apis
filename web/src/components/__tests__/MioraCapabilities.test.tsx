import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ModelInfo } from "../../api/types";
import type { ProviderGroup } from "../../lib/credentialList";
import { CredentialGroupHeader } from "../../pages/credentials/groupHeader";
import { ModelPicker, groupModelsByProvider, modelValue, pickDefaultModel } from "../ModelPicker";
import { stubPopupGeometry } from "@/test-utils/popupGeometry";

const models: ModelInfo[] = [
  { id: "hy4flash", name: "Hy4 Flash 标准路由", object: "model", owned_by: "2apis", providers: ["miora"] },
  { id: "hy4flash-free", name: "Hy4 Flash 免费路由", object: "model", owned_by: "2apis", providers: ["miora"], credit_rate: 0 },
  { id: "miora-pro", name: "进阶模式", object: "model", owned_by: "2apis", providers: ["miora"] },
];

describe("Miora 模型用途", () => {
  afterEach(() => vi.restoreAllMocks());
  it("凭证池弹层每项都有用途，并列出已接出的生成接口", async () => {
    stubPopupGeometry("group-miora-model-count");
    const group: ProviderGroup = {
      key: "miora", provider: "miora", label: "Miora", ledger: "credits",
      items: [], states: {}, remaining: 75, total: 100, expiring: null,
      expiringSecondary: null, checkin: { done: 0, failed: 0, pending: 0, lastAt: null },
    };
    render(<table><tbody><CredentialGroupHeader
      group={group} isCollapsed canWrite={false} allSelected={false}
      groupIds={[]} groupModels={groupModelsByProvider(models).get("miora") ?? []}
      rateText={null} quotaPct={75} expiryWindow={3600} expirySecondaryWindow={7200}
      onToggleGroup={vi.fn()} onToggleGroupSelected={vi.fn()}
    /></tbody></table>);
    await userEvent.hover(screen.getByTestId("group-miora-model-count"));
    const popover = await screen.findByTestId("group-miora-models");
    expect(popover).toHaveTextContent("3 个模型");
    const rows = within(popover).getAllByTestId("group-miora-model");
    expect(rows).toHaveLength(models.length);
    for (const row of rows) {
      expect(within(row).getByTestId("model-purpose")).toHaveTextContent("文本/代码");
      expect(row).toHaveTextContent("客户端工具（JSON模拟）");
    }
    expect(rows.some(row => row.textContent?.includes("标准路由"))).toBe(true);
    expect(rows.some(row => row.textContent?.includes("免费路由"))).toBe(true);
    expect(rows.some(row => row.textContent?.includes("网页模式 · 进阶"))).toBe(true);
    expect(popover).toHaveTextContent("生图接口：/v1/images/generations");
    expect(popover).toHaveTextContent("视频接口：/v1/videos");
    expect(popover).toHaveTextContent("原始 ID：hy4flash-free");
    const media = within(popover).getByTestId("miora-official-media");
    expect(media).not.toHaveAttribute("open");
    await userEvent.click(within(media).getByText("官网其他创作入口（未接入 API）"));
    expect(media).toHaveAttribute("open");
    expect(screen.getByTestId("group-miora-models")).toBeInTheDocument();
    expect(media).toHaveTextContent("H3D3.1");
    expect(media).toHaveTextContent("音频：当前 API 未接入");
    expect(within(popover).getAllByTestId("group-miora-model")).toHaveLength(models.length);
  });

  it("真实媒体条目按端点分组，目录中的未接入条目不误标可调用", async () => {
    stubPopupGeometry("group-miora-model-count");
    const extended: ModelInfo[] = [...models,
      { id: "gem-3.1", name: "Nano Banana 2", object: "model", owned_by: "2apis", providers: ["miora"],
        gateway_endpoints: ["/v1/images/generations"], output_modalities: ["image"] },
      { id: "vidu-q2-pro-video", name: "Vidu Q2 Pro", object: "model", owned_by: "2apis", providers: ["miora"],
        gateway_endpoints: ["/v1/videos"], output_modalities: ["video"] },
      { id: "3d-reference", object: "model", owned_by: "2apis", providers: ["miora"], gateway_endpoints: [] },
    ];
    const group: ProviderGroup = {
      key: "miora", provider: "miora", label: "Miora", ledger: "credits", items: [], states: {}, remaining: 75,
      total: 100, expiring: null, expiringSecondary: null, checkin: { done: 0, failed: 0, pending: 0, lastAt: null },
    };
    render(<table><tbody><CredentialGroupHeader group={group} isCollapsed canWrite={false} allSelected={false}
      groupIds={[]} groupModels={groupModelsByProvider(extended).get("miora") ?? []} rateText={null}
      quotaPct={75} expiryWindow={3600} expirySecondaryWindow={7200} onToggleGroup={vi.fn()}
      onToggleGroupSelected={vi.fn()} /></tbody></table>);
    await userEvent.hover(screen.getByTestId("group-miora-model-count"));
    const popover = await screen.findByTestId("group-miora-models");
    expect(within(popover).getAllByTestId("miora-model-section").map(node => node.textContent)).toEqual([
      "文本/代码", "媒体专用（图片）", "媒体专用（视频）", "目录模型",
    ]);
    const rows = within(popover).getAllByTestId("group-miora-model");
    expect(rows).toHaveLength(6);
    expect(rows.find(row => row.textContent?.includes("Nano Banana 2"))).toHaveTextContent("/v1/images/generations");
    expect(rows.find(row => row.textContent?.includes("Vidu Q2 Pro"))).toHaveTextContent("/v1/videos");
    for (const name of ["Nano Banana 2", "Vidu Q2 Pro"]) {
      expect(within(rows.find(row => row.textContent?.includes(name))!).getByText("媒体专用"))
        .toBeInTheDocument();
    }
    expect(rows.find(row => row.textContent?.includes("3d-reference"))).toHaveTextContent("未接入调用接口");
  });

  it("媒体模型不进入聊天 Picker，也不以低倍率抢占默认选择", () => {
    const extended: ModelInfo[] = [
      { id: "gem-3.1", object: "model", owned_by: "2apis", providers: ["miora"], credit_rate: 0,
        gateway_endpoints: ["/v1/images/generations"], output_modalities: ["image"] }, ...models,
    ];
    const picked = extended.map(item => ({ ...item, value: modelValue(item) }));
    expect(pickDefaultModel(picked)).toBe("hy4flash-free@miora");
    render(<ModelPicker models={picked} value="hy4flash-free@miora" onChange={vi.fn()} />);
    expect(screen.getAllByRole("option")).toHaveLength(3);
    expect(screen.queryByTestId("model-option-gem-3.1@miora")).not.toBeInTheDocument();
  });

  it("Picker 保留全部原有条目并逐项标注用途和标准/免费路由", () => {
    const picked = models.map(item => ({ ...item, value: modelValue(item) }));
    render(<ModelPicker models={picked} value="hy4flash-free@miora" onChange={vi.fn()} />);
    expect(screen.getAllByRole("option")).toHaveLength(models.length);
    for (const option of screen.getAllByRole("option")) {
      expect(within(option).getByTestId("model-use")).toHaveTextContent("文本/代码");
    }
    expect(screen.getByTestId("model-option-hy4flash@miora")).toHaveTextContent("标准路由");
    expect(screen.getByTestId("model-option-hy4flash-free@miora")).toHaveTextContent("免费路由");
  });
});
