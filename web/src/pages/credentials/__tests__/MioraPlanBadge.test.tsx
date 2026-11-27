import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Credential } from "../../../api/types";
import { makeCredential, userEvent } from "@/test-utils";
import { MioraPlanBadge } from "../MioraPlanBadge";
import { mioraPlanExpiryTime } from "../mioraAccountPlan";
import { stubPopupGeometry } from "@/test-utils/popupGeometry";

const NOW = Date.UTC(2026, 10, 27) / 1000;
type Subscription = NonNullable<NonNullable<Credential["quota_detail"]>["miora_subscription"]>;
function credential(overrides: Partial<Subscription> = {}): Credential {
  return makeCredential({ id: "miora-plan", provider: "miora", nickname: "会员账号", health: 80,
    quota_detail: { miora_subscription: { tier: "standard", status: "active", vip: true,
      expires_at: NOW + 10 * 86400, probed_at: NOW, ...overrides } } }) as Credential;
}

describe("Miora 会员剩余与到期明细", () => {
  beforeEach(() => stubPopupGeometry("miora-plan-trigger-miora-plan"));
  afterEach(() => vi.restoreAllMocks());
  it("有效VIP在行内显示剩余时间，明细展示北京时间到期日期", async () => {
    render(<MioraPlanBadge credential={credential()} now={NOW} />);
    expect(screen.getByTestId("miora-plan-miora-plan")).toHaveTextContent("VIP · Standard");
    expect(screen.getByTestId("miora-plan-remaining-miora-plan")).toHaveTextContent("剩余 10.0 天");
    expect(screen.getByTestId("miora-plan-remaining-miora-plan")).not.toHaveTextContent("临期");
    await userEvent.hover(screen.getByRole("button", { name: "会员账号会员明细" }));
    const details = await screen.findByRole("dialog", { name: "Miora 会员明细" });
    expect(details).toHaveTextContent("Miora 会员明细");
    expect(details).toHaveTextContent(mioraPlanExpiryTime(NOW + 10 * 86400));
    expect(details).toHaveTextContent("北京时间");
    expect(details).toHaveTextContent("剩余 10.0 天");
    expect(details).toHaveClass("overflow-y-auto");
    expect(details.style.maxHeight).toContain("radix-popover-content-available-height");
  });
  it("临期倒计时随当前时间更新，跨过到期时刻显示已到期", async () => {
    const row = credential({ expires_at: NOW + 2 * 86400 });
    const view = render(<MioraPlanBadge credential={row} now={NOW} />);
    expect(screen.getByTestId("miora-plan-remaining-miora-plan")).toHaveTextContent("剩余 2.0 天 · 临期");
    await userEvent.hover(screen.getByRole("button", { name: "会员账号会员明细" }));
    expect(await screen.findByRole("dialog", { name: "Miora 会员明细" })).toHaveTextContent("7 天内到期");
    view.rerender(<MioraPlanBadge credential={row} now={NOW + 86400} />);
    expect(screen.getByTestId("miora-plan-remaining-miora-plan")).toHaveTextContent("剩余 1.0 天 · 临期");
    view.rerender(<MioraPlanBadge credential={row} now={NOW + 2 * 86400} />);
    expect(screen.getByTestId("miora-plan-miora-plan")).toHaveTextContent("套餐已到期");
    expect(screen.getByTestId("miora-plan-remaining-miora-plan")).toHaveTextContent("已到期");
    expect(screen.getByTestId("miora-plan-remaining-miora-plan")).not.toHaveTextContent("剩余");
  });
  it.each([
    { fields: { tier: "free", status: "free", vip: false, expires_at: null } as Partial<Subscription>, label: "免费", detail: "免费套餐无会员到期时间" },
    { fields: { tier: null, status: "unknown", vip: null, expires_at: null } as Partial<Subscription>, label: "套餐未获取", detail: "暂无已确认到期时间" },
    { fields: { status: "expired", vip: false, expires_at: null } as Partial<Subscription>, label: "套餐已到期", detail: "暂无已确认到期时间" },
  ])("$label不虚构会员倒计时", async ({ fields, label, detail }) => {
    render(<MioraPlanBadge credential={credential(fields)} now={NOW} />);
    expect(screen.getByTestId("miora-plan-miora-plan")).toHaveTextContent(label);
    expect(screen.queryByTestId("miora-plan-remaining-miora-plan")).not.toBeInTheDocument();
    await userEvent.hover(screen.getByRole("button", { name: "会员账号会员明细" }));
    const popup = await screen.findByRole("dialog", { name: "Miora 会员明细" });
    expect(popup).toHaveTextContent(detail);
    expect(popup).not.toHaveTextContent("会员剩余");
  });
  it("探测失败不把缓存VIP日期变成当前倒计时，其它渠道不显示Miora会员", () => {
    const view = render(<MioraPlanBadge credential={{ ...credential(), health: null }} now={NOW} />);
    expect(screen.getByTestId("miora-plan-miora-plan")).toHaveTextContent("套餐未获取");
    expect(screen.queryByTestId("miora-plan-remaining-miora-plan")).not.toBeInTheDocument();
    view.rerender(<MioraPlanBadge credential={{ ...credential(), provider: "codearts" }} now={NOW} />);
    expect(screen.queryByTestId("miora-plan-miora-plan")).not.toBeInTheDocument();
  });
  it("触屏点击固定明细并可关闭，键盘Escape恢复焦点且不重新弹出", async () => {
    render(<MioraPlanBadge credential={credential()} now={NOW} />);
    const trigger = screen.getByRole("button", { name: "会员账号会员明细" });
    const touch = new Event("pointerover", { bubbles: true });
    Object.defineProperty(touch, "pointerType", { value: "touch" });
    fireEvent(trigger, touch);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    fireEvent.click(trigger);
    let popup = await screen.findByRole("dialog", { name: "Miora 会员明细" });
    expect(popup).toHaveAttribute("data-open-mode", "pinned");
    await userEvent.click(screen.getByRole("button", { name: "关闭会员明细" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    popup = await screen.findByRole("dialog", { name: "Miora 会员明细" });
    expect(popup).toHaveAttribute("data-open-mode", "pinned");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });
});
