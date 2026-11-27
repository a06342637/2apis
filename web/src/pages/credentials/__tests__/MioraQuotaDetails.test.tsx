import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Credential } from "../../../api/types";
import { makeCredential, userEvent } from "@/test-utils";
import { stubPopupGeometry } from "@/test-utils/popupGeometry";
import { PackageLadder } from "../cells";
import { mioraPlanExpiryTime } from "../mioraAccountPlan";

const NOW = Date.UTC(2026, 10, 27) / 1000;
function account(overrides: Partial<Credential> = {}): Credential {
  return makeCredential({ id: "credit-account", provider: "miora", nickname: "积分账号", quota_remaining: 200, quota_total: 350,
    quota_detail: { miora_subscription: { tier: "standard", status: "active", vip: true,
      expires_at: NOW + 365 * 86400, probed_at: NOW } },
    quota_packages: [
      { name: "购买积分", total: 100, used: 20, end: null },
      { name: "订阅积分", total: 200, used: 100, end: NOW + 30 * 86400 },
      { name: "试用积分", total: 50, used: 30, end: NOW + 86400 },
    ], ...overrides }) as Credential;
}

describe("Miora 每账号积分包到期明细", () => {
  beforeEach(() => stubPopupGeometry("packages-toggle-credit-account"));
  afterEach(() => vi.restoreAllMocks());

  it("试用、订阅、购买积分各自显示余额和到期，不能借会员期限或把null当永久", async () => {
    render(<PackageLadder credential={account()} now={NOW} />);
    const trigger = screen.getByRole("button", { name: "积分账号积分明细" });
    expect(trigger).toHaveTextContent("积分明细 3 项");
    await userEvent.click(trigger);
    const popup = await screen.findByRole("dialog", { name: "Miora 积分明细" });
    const rows = within(popup).getAllByTestId("package-credit-account");
    expect(rows.map(row => row.textContent)).toEqual([
      expect.stringContaining("试用积分"), expect.stringContaining("订阅积分"), expect.stringContaining("购买积分"),
    ]);
    expect(rows[0]).toHaveTextContent("剩 20 / 50");
    expect(rows[0]).toHaveTextContent("已用 30");
    expect(rows[0]).toHaveTextContent(mioraPlanExpiryTime(NOW + 86400));
    expect(rows[0]).toHaveTextContent("7 天内到期 20 积分");
    expect(rows[1]).toHaveTextContent("剩 100 / 200");
    expect(rows[1]).toHaveTextContent(mioraPlanExpiryTime(NOW + 30 * 86400));
    expect(rows[2]).toHaveTextContent("上游未提供到期时间");
    expect(rows[2]).not.toHaveTextContent("永久");
    expect(popup).not.toHaveTextContent(mioraPlanExpiryTime(NOW + 365 * 86400));
    expect(popup).toHaveTextContent("账号积分余额：200 / 350");
    expect(popup).toHaveClass("overflow-y-auto");
    expect(popup.style.maxHeight).toContain("radix-popover-content-available-height");
    await userEvent.click(screen.getByRole("button", { name: "关闭积分明细" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("积分到期跨过当前时刻后标已到期并停止倒计时，不把暂不可用当已到期", async () => {
    const row = account({ quota_packages: [{ name: "试用积分", total: 50, used: 10, end: NOW + 10 }] });
    const view = render(<PackageLadder credential={row} now={NOW} />);
    await userEvent.click(screen.getByRole("button", { name: "积分账号积分明细" }));
    let popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    expect(popup).toHaveTextContent("剩余 10 秒");
    view.rerender(<PackageLadder credential={row} now={NOW + 10} />);
    popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    expect(popup).toHaveTextContent("已到期");
    expect(popup).not.toHaveTextContent("7 天内到期");
    view.rerender(<PackageLadder credential={account({ quota_packages: [{ name: "赠送积分", total: 50, used: 10, end: null, usable: false }] })} now={NOW} />);
    expect(popup).toHaveTextContent("暂不可用");
    expect(popup).not.toHaveTextContent("已到期");
  });

  it("没有积分包信息时只标上游未提供，已耗尽明细不误报为未获取", async () => {
    const view = render(<PackageLadder credential={account({ quota_packages: null })} now={NOW} />);
    const trigger = screen.getByRole("button", { name: "积分账号积分明细" });
    expect(trigger).toHaveTextContent("积分到期未提供");
    await userEvent.click(trigger);
    const popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    expect(popup).toHaveTextContent("上游未提供积分包明细及积分到期时间");
    expect(popup).not.toHaveTextContent(mioraPlanExpiryTime(NOW + 365 * 86400));
    view.rerender(<PackageLadder credential={account({ quota_packages: [{ name: "试用积分", total: 50, used: 50, end: NOW + 10 }] })} now={NOW} />);
    expect(trigger).toHaveTextContent("积分明细");
    expect(popup).toHaveTextContent("当前没有剩余额度的积分包");
    expect(popup).not.toHaveTextContent("上游未提供");
  });

  it("不同账号的积分包独立展示，切换详情不串账号", async () => {
    render(<><PackageLadder credential={account()} now={NOW} />
      <PackageLadder credential={account({ id: "other", nickname: "另一个账号", quota_packages: [
        { name: "另一个试用包", total: 80, used: 2, end: NOW + 2 * 86400 },
      ] })} now={NOW} /></>);
    await userEvent.click(screen.getByRole("button", { name: "积分账号积分明细" }));
    expect(screen.getByRole("dialog", { name: "Miora 积分明细" })).toHaveTextContent("剩 20 / 50");
    await userEvent.click(screen.getByRole("button", { name: "关闭积分明细" }));
    await userEvent.click(screen.getByRole("button", { name: "另一个账号积分明细" }));
    const popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    expect(popup).toHaveTextContent("另一个试用包");
    expect(popup).toHaveTextContent("剩 78 / 80");
    expect(popup).not.toHaveTextContent("购买积分");
  });

  it.each([undefined, 40])("退款或冻结的积分包不计临期金额，即使提供expiring=%s", async (expiring) => {
    render(<PackageLadder credential={account({ quota_packages: [
      { name: "已退款积分包", total: 50, used: 10, end: NOW + 86400, usable: false, expiring },
    ] })} now={NOW} />);
    await userEvent.click(screen.getByRole("button", { name: "积分账号积分明细" }));
    const popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    expect(popup).toHaveTextContent("已退款积分包");
    expect(popup).toHaveTextContent("剩 40 / 50");
    expect(popup).toHaveTextContent("已用 10");
    expect(popup).toHaveTextContent("暂不可用");
    expect(popup).not.toHaveTextContent("7 天内到期");
    expect(within(popup).queryByTestId("package-expiring-credit-account")).not.toBeInTheDocument();
  });

  it("成功读取空积分包虽存储为null也明确显示无剩余，不误报上游未提供", async () => {
    render(<PackageLadder credential={account({ quota_packages: null, quota_detail: {
      miora_credits: { packages_status: "ok", next_expiring: null },
    } })} now={NOW} />);
    const trigger = screen.getByRole("button", { name: "积分账号积分明细" });
    expect(trigger).toHaveTextContent("积分明细");
    await userEvent.click(trigger);
    const popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    expect(popup).toHaveTextContent("当前没有剩余额度的积分包");
    expect(popup).not.toHaveTextContent("上游未提供");
  });

  it("只获取最早到期摘要时显示真实金额日期，不虚构总量和已用", async () => {
    const row = account({ quota_packages: null, quota_detail: { miora_credits: {
      packages_status: "summary_only", next_expiring: { credit: 12.5, expires_at: NOW + 2 * 86400 },
    } } });
    const view = render(<PackageLadder credential={row} now={NOW} />);
    const trigger = screen.getByRole("button", { name: "积分账号积分明细" });
    expect(trigger).toHaveTextContent("积分到期摘要");
    await userEvent.click(trigger);
    const popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    const summary = within(popup).getByTestId("packages-summary-credit-account");
    expect(summary).toHaveTextContent("仅获取到最早到期积分，完整积分包明细暂不可用");
    expect(summary).toHaveTextContent("12.5 积分");
    expect(summary).toHaveTextContent(mioraPlanExpiryTime(NOW + 2 * 86400));
    expect(summary).not.toHaveTextContent("已用");
    expect(summary).not.toHaveTextContent("200 / 350");
    expect(summary).not.toHaveTextContent("剩 12.5 / 12.5");
    view.rerender(<PackageLadder credential={row} now={NOW + 2 * 86400} />);
    expect(summary).toHaveTextContent("已到期");
  });

  it("旧快照仅有最早积分日期时不把总余额当该批到期金额，新未获取状态优先", async () => {
    const row = account({ quota_packages: null, quota_cycle_end: NOW + 86400 });
    const view = render(<PackageLadder credential={row} now={NOW} />);
    await userEvent.click(screen.getByRole("button", { name: "积分账号积分明细" }));
    const popup = screen.getByRole("dialog", { name: "Miora 积分明细" });
    expect(popup).toHaveTextContent(`最早积分到期时间：${mioraPlanExpiryTime(NOW + 86400)}`);
    expect(popup).toHaveTextContent("上游未提供该批次到期金额");
    expect(within(popup).queryByTestId("packages-summary-credit-account")).not.toBeInTheDocument();
    view.rerender(<PackageLadder credential={{ ...row, quota_detail: { miora_credits: {
      packages_status: "unavailable", next_expiring: null,
    } } }} now={NOW} />);
    expect(popup).toHaveTextContent("上游未提供积分包明细及积分到期时间");
    expect(popup).not.toHaveTextContent("最早积分到期时间");
  });
});
