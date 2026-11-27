import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CredentialGroupHeader } from "../groupHeader";
import { ModelListPopup, ModelListPopupClose } from "../ModelListPopup";
import { groupByProvider } from "../../../lib/credentialList";
import type { Credential } from "../../../api/types";
import type { ProviderModel } from "../../../components/ModelPicker";
import { modelAccess } from "../../../lib/modelAccess";
import { makeCredential, userEvent } from "@/test-utils";
import { stubPopupGeometry } from "@/test-utils/popupGeometry";

const defaultModels: ProviderModel[] = [{ id: "glm-5.3@miora", label: "GLM 5.3", rate: 0.5, gatewayGroup: "chat",
  purpose: { route: "/v1/chat/completions", label: "文本对话", description: "完整模型用途" }, rawId: "glm-5.3" }];

function setup(models = defaultModels) {
  const group = groupByProvider([makeCredential({ id: "miora-test", provider: "miora" }) as Credential], Date.now() / 1000)[0];
  const toggle = vi.fn();
  const view = render(<><table><tbody><CredentialGroupHeader group={group} isCollapsed canWrite={false} allSelected={false}
    groupIds={[]} groupModels={models}
    rateText="最低 x0.5" quotaPct={100} expiryWindow={129600} expirySecondaryWindow={604800}
    onToggleGroup={toggle} onToggleGroupSelected={vi.fn()} /></tbody></table><button>外部操作</button></>);
  return { ...view, toggle };
}

describe("账号组模型弹层", () => {
  beforeEach(() => {
    stubPopupGeometry("group-miora-model-count");
  });
  afterEach(() => vi.restoreAllMocks());

  it("点击模型数量后保持清单打开，触屏可以操作详情", async () => {
    const { toggle } = setup();
    fireEvent.click(screen.getByTestId("group-miora-model-count"));
    const popup = await screen.findByRole("dialog", { name: "Miora 模型清单" });
    expect(popup).toHaveTextContent("GLM 5.3");
    expect(popup).toHaveTextContent("完整模型用途");
    expect(popup).toHaveTextContent("原始 ID：glm-5.3");
    await userEvent.click(screen.getByText("官网其他创作入口（未接入 API）"));
    expect(screen.getByTestId("miora-official-media")).toHaveAttribute("open");
    expect(screen.getByRole("link", { name: "查看 Miora 官网" })).toHaveAttribute("href", "https://miora.design/");
    expect(popup).toBeInTheDocument();
    expect(toggle).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "关闭模型清单" }));
    await waitFor(() => expect(popup).not.toBeInTheDocument());
  });

  it("整个内容按可用视口限高，而不只限制模型列表", async () => {
    setup();
    await userEvent.hover(screen.getByTestId("group-miora-model-count"));
    const content = await screen.findByTestId("group-miora-models");
    const popup = content.closest("[data-slot='tooltip-content'], [data-slot='model-popup-content']") as HTMLElement;
    expect(popup.style.maxHeight).toContain("content-available-height");
    expect(popup).toHaveClass("overflow-y-auto");
  });

  it("从触发器跨过间隔进入弹层可继续查看，离开未固定预览后关闭", async () => {
    setup();
    const trigger = screen.getByTestId("group-miora-model-count");
    await userEvent.hover(trigger);
    const popup = await screen.findByRole("dialog", { name: "Miora 模型清单" });
    expect(popup).toHaveAttribute("data-open-mode", "preview");
    await userEvent.unhover(trigger);
    await userEvent.hover(popup);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 200)); });
    expect(popup).toBeInTheDocument();
    await userEvent.unhover(popup);
    await waitFor(() => expect(popup).not.toBeInTheDocument());
  });

  it("悬浮后点击固定，移出鼠标不会关闭，再次点击可以关闭", async () => {
    setup();
    const trigger = screen.getByTestId("group-miora-model-count");
    await userEvent.hover(trigger);
    const popup = await screen.findByRole("dialog", { name: "Miora 模型清单" });
    await userEvent.click(trigger);
    expect(popup).toHaveAttribute("data-open-mode", "pinned");
    await userEvent.unhover(trigger);
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 200)); });
    expect(popup).toBeInTheDocument();
    await userEvent.click(trigger);
    await waitFor(() => expect(popup).not.toBeInTheDocument());
  });

  it("键盘聚焦预览、Enter固定可滚动区域、Escape返回触发器且不自动重开", async () => {
    setup();
    const trigger = screen.getByTestId("group-miora-model-count");
    act(() => trigger.focus());
    const popup = await screen.findByRole("dialog", { name: "Miora 模型清单" });
    expect(trigger).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(popup).toHaveFocus();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(popup).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    await userEvent.tab();
    await userEvent.tab({ shift: true });
    expect(await screen.findByTestId("group-miora-models")).toBeInTheDocument();
  });

  it("未固定预览经Tab离开关闭后，再次Tab回触发器仍可预览", async () => {
    setup();
    const trigger = screen.getByTestId("group-miora-model-count");
    act(() => trigger.focus());
    const popup = await screen.findByRole("dialog", { name: "Miora 模型清单" });
    expect(popup).toHaveAttribute("data-open-mode", "preview");
    await userEvent.tab();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "外部操作" })).toHaveFocus();
    await userEvent.tab({ shift: true });
    expect(trigger).toHaveFocus();
    expect(await screen.findByRole("dialog", { name: "Miora 模型清单" })).toHaveAttribute("data-open-mode", "preview");
  });

  it("点击外部关闭并保留外部操作焦点", async () => {
    setup();
    await userEvent.click(screen.getByTestId("group-miora-model-count"));
    const popup = await screen.findByRole("dialog", { name: "Miora 模型清单" });
    await userEvent.click(screen.getByRole("button", { name: "外部操作" }));
    await waitFor(() => expect(popup).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "外部操作" })).toHaveFocus();
    act(() => screen.getByTestId("group-miora-model-count").focus());
    expect(await screen.findByRole("dialog", { name: "Miora 模型清单" })).toHaveAttribute("data-open-mode", "preview");
  });

  it("关闭按钮移除弹层后产生的触发器pointerenter不能重新预览，重新移入仍可预览", async () => {
    setup();
    const trigger = screen.getByTestId("group-miora-model-count");
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("button", { name: "关闭模型清单" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const pointer = new Event("pointerover", { bubbles: true });
    Object.defineProperty(pointer, "pointerType", { value: "mouse" });
    fireEvent(trigger, pointer);
    fireEvent.blur(trigger, { relatedTarget: null });
    fireEvent.focus(trigger);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await userEvent.unhover(trigger);
    await userEvent.hover(trigger);
    expect(await screen.findByRole("dialog", { name: "Miora 模型清单" })).toHaveAttribute("data-open-mode", "preview");
  });

  it("关闭后只有命中变化不重新打开，用户真实移动鼠标后仍可重新预览", async () => {
    setup();
    const trigger = screen.getByTestId("group-miora-model-count");
    await userEvent.click(trigger);
    await userEvent.click(screen.getByRole("button", { name: "关闭模型清单" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const enter = new Event("pointerover", { bubbles: true });
    Object.defineProperty(enter, "pointerType", { value: "mouse" });
    fireEvent(trigger, enter);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const move = new Event("pointermove", { bubbles: true });
    Object.defineProperty(move, "pointerType", { value: "mouse" });
    fireEvent(trigger, move);
    expect(await screen.findByRole("dialog", { name: "Miora 模型清单" })).toHaveAttribute("data-open-mode", "preview");
  });

  it("关闭一个浮层后静止指针命中另一个实例不重开，真实移动后可预览", async () => {
    render(<>
      <ModelListPopup label="第一个" content={<ModelListPopupClose>关闭第一个</ModelListPopupClose>}>
        <button data-testid="group-miora-model-count">第一个账号</button>
      </ModelListPopup>
      <ModelListPopup label="第二个" content={<span>第二个账号明细</span>}>
        <button>第二个账号</button>
      </ModelListPopup>
    </>);
    await userEvent.click(screen.getByRole("button", { name: "第一个账号" }));
    await userEvent.click(screen.getByRole("button", { name: "关闭第一个" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const nextTrigger = screen.getByRole("button", { name: "第二个账号" });
    const enter = new Event("pointerover", { bubbles: true });
    Object.defineProperty(enter, "pointerType", { value: "mouse" });
    fireEvent(nextTrigger, enter);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    const move = new Event("pointermove", { bubbles: true });
    Object.defineProperty(move, "pointerType", { value: "mouse" });
    fireEvent(nextTrigger, move);
    expect(await screen.findByRole("dialog", { name: "第二个 模型清单" })).toHaveAttribute("data-open-mode", "preview");
  });

  it("普通模型在VIP之前，每区保留用途顺序并用真实权限显示锁定，不从倍率猜VIP", async () => {
    const access = (available: boolean, tier = "free", reason?: string) => modelAccess({ id: tier, object: "model", owned_by: "miora",
      providers: ["miora"], required_account_tier: tier, account_available: available,
      ...(reason ? { account_unavailable_reason: reason } : {}) });
    setup([
      { id: "vip-chat", label: "VIP Chat", gatewayGroup: "chat", access: access(false, "standard", "tier_locked") },
      { id: "normal-video", label: "Normal Video", gatewayGroup: "video", access: access(true) },
      { id: "vip-video", label: "VIP Video", gatewayGroup: "video", access: access(false, "standard", "tier_locked") },
      { id: "normal-chat", label: "Normal Chat", gatewayGroup: "chat", access: access(true) },
      { id: "premium-image", label: "Premium Image", gatewayGroup: "image", rate: 10, access: access(true) },
      { id: "disabled-image", label: "Feature Off Image", gatewayGroup: "image", access: access(false, "standard", "feature_flag_off") },
    ]);
    await userEvent.click(screen.getByTestId("group-miora-model-count"));
    const rows = screen.getAllByTestId("group-miora-model");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Normal Chat"), expect.stringContaining("Premium Image"), expect.stringContaining("Normal Video"),
      expect.stringContaining("VIP Chat"), expect.stringContaining("VIP Video"), expect.stringContaining("Feature Off Image"),
    ]);
    expect(screen.getAllByTestId("model-access-section").map((node) => node.textContent)).toEqual([
      "常用 · 当前账号可用", "VIP 模型（需要对应套餐）", "暂不可用模型",
    ]);
    expect(screen.getByTestId("group-miora-model-availability")).toHaveTextContent("已确认可用 3；需 VIP 且当前不可用 2；暂不可用 1");
    expect(rows[1]).toHaveTextContent("当前账号可用");
    expect(rows[1]).not.toHaveTextContent("VIP");
    expect(rows[3]).toHaveTextContent("VIP · 当前账号不可用");
    expect(rows[5]).toHaveTextContent("与 VIP 套餐权限无关");
  });
});
