import { describe, expect, it } from "vitest";
import type { Credential } from "../../../api/types";
import { makeCredential } from "@/test-utils";
import { mioraAccountPlan, mioraPlanExpiryTime } from "../mioraAccountPlan";

const NOW = 1_800_000_000;
type Subscription = NonNullable<NonNullable<Credential["quota_detail"]>["miora_subscription"]>;
function credential(subscription?: Subscription | null): Credential {
  return makeCredential({ provider: "miora", quota_remaining: 62, quota_total: 100,
    ...(subscription !== undefined ? { quota_detail: { miora_subscription: subscription } } : {}) }) as Credential;
}
const subscription = (overrides: Partial<Subscription> = {}): Subscription => ({ tier: "standard", status: "active",
  vip: true, expires_at: NOW + 100, probed_at: NOW, ...overrides });

describe("Miora 账号套餐状态", () => {
  it("其它渠道不出现Miora套餐徽章", () => {
    expect(mioraAccountPlan({ ...credential(subscription()), provider: "codearts" }, NOW)).toBeNull();
  });
  it.each([undefined, null])("无套餐信息时不能按已有积分余额猜免费：%s", (info) => {
    expect(mioraAccountPlan(credential(info), NOW)).toMatchObject({ label: "套餐未获取", tone: "muted" });
  });
  it("免费标识不表示零倍率，赠送积分仍按模型倍率消耗", () => {
    const view = mioraAccountPlan(credential(subscription({ tier: "free", status: "free", vip: false, expires_at: null })), NOW);
    expect(view).toMatchObject({ label: "免费", tone: "ok" });
    expect(view?.description).toContain("实际倍率扣赠送积分");
  });
  it.each(["basic", "standard", "pro"] as const)("有效%s套餐显示准确VIP等级", (tier) => {
    const view = mioraAccountPlan(credential(subscription({ tier })), NOW);
    expect(view).toMatchObject({ label: `VIP · ${tier === "basic" ? "Basic" : tier === "standard" ? "Standard" : "Pro"}`, tone: "accent" });
  });
  it("同一份active快照跨过到期时刻不再保持VIP", () => {
    const row = credential(subscription());
    expect(mioraAccountPlan(row, NOW + 99)?.label).toBe("VIP · Standard");
    expect(mioraAccountPlan(row, NOW + 100)).toMatchObject({ label: "套餐已到期", tone: "warn" });
  });
  it("上游明确expired即使无日期也不标成免费或有效VIP", () => {
    expect(mioraAccountPlan(credential(subscription({ status: "expired", vip: false, expires_at: null })), NOW)?.label).toBe("套餐已到期");
  });
  it("最新探测失败时不把保留的旧active记录当成当前VIP，到期仍如实提示", () => {
    const row = { ...credential(subscription()), health: null };
    expect(mioraAccountPlan(row, NOW)?.label).toBe("套餐未获取");
    expect(mioraAccountPlan(row, NOW + 100)?.label).toBe("套餐已到期");
  });
  it.each([
    { status: "unknown" as const, vip: null, tier: null },
    { expires_at: null },
    { expires_at: Number.NaN },
    { expires_at: 0 },
    { expires_at: -1 },
    { expires_at: Number.POSITIVE_INFINITY },
    { vip: false },
    { tier: "free" as const },
  ])("不完整或矛盾的付费状态不冒认VIP：%j", (fields) => {
    expect(mioraAccountPlan(credential(subscription(fields)), NOW)?.label).toBe("套餐未获取");
  });
  it("剩余时间与7天临期提醒跟随当前时间，到期后不再显示剩余会员", () => {
    const row = credential(subscription({ expires_at: NOW + 8 * 86400 }));
    expect(mioraAccountPlan(row, NOW)).toMatchObject({ expiresAt: NOW + 8 * 86400,
      remainingSeconds: 8 * 86400, expiring: false });
    expect(mioraAccountPlan(row, NOW + 86400)).toMatchObject({ remainingSeconds: 7 * 86400, expiring: true });
    expect(mioraAccountPlan(row, NOW + 8 * 86400)).toMatchObject({ remainingSeconds: 0, expiring: false, label: "套餐已到期" });
  });
  it("免费或未确认套餐没有虚构到期日期或倒计时", () => {
    expect(mioraAccountPlan(credential(subscription({ tier: "free", status: "free", vip: false, expires_at: null })), NOW))
      .toMatchObject({ expiresAt: null, remainingSeconds: null, expiring: false });
    expect(mioraAccountPlan(credential(), NOW)).toMatchObject({ expiresAt: null, remainingSeconds: null, expiring: false });
    expect(mioraAccountPlan({ ...credential(subscription()), health: null }, NOW)).toMatchObject({ expiresAt: null, remainingSeconds: null });
  });
  it("会员到期时间明确按北京时间跨日展示", () => {
    expect(mioraPlanExpiryTime(Date.UTC(2026, 10, 26, 20) / 1000)).toBe("2026/11/27 04:00:00");
  });
});
