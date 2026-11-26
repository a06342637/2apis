import { describe, expect, it } from "vitest";
import type { Credential } from "../../../api/types";
import { makeCredential } from "@/test-utils";
import { mioraAccountPlan } from "../mioraAccountPlan";

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
    { vip: false },
    { tier: "free" as const },
  ])("不完整或矛盾的付费状态不冒认VIP：%j", (fields) => {
    expect(mioraAccountPlan(credential(subscription(fields)), NOW)?.label).toBe("套餐未获取");
  });
});
