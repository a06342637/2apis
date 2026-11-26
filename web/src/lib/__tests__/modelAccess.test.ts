import { describe, expect, it } from "vitest";
import type { ModelInfo } from "@/api/types";
import { groupModelsByProvider } from "@/components/ModelPicker";
import { modelAccess, sortModelsByAccess, sortProviderModelsByAccess } from "../modelAccess";

const model = (id: string, extra: Partial<ModelInfo> = {}): ModelInfo => ({
  id, object: "model", owned_by: "2apis", providers: ["miora"], ...extra,
});

describe("模型账号权限", () => {
  it("倍率/型号不推断 VIP，缺权限元数据时保守说明", () => {
    const access = modelAccess(model("gpt-image-pro-max", { credit_rate: 100 }));
    expect(access.vip).toBe(false);
    expect(access.available).toBeUndefined();
    expect(access.description).toContain("未声明额外套餐限制");
    expect(modelAccess(model("free", { required_account_tier: "free", account_available: true })).label)
      .toBe("当前账号可用");
  });

  it("真实套餐要求标 VIP，同时说明当前账号是否可用", () => {
    const locked = model("premium", { required_account_tier: "standard", account_available: false,
      account_unavailable_reason: "tier_locked" });
    expect(modelAccess(locked)).toMatchObject({ label: "VIP", vip: true, available: false });
    expect(modelAccess(locked).description).toContain("Standard 套餐；当前账号池不可用");
    expect(modelAccess({ ...locked, account_available: true }).description).toContain("有权限的账号");
    expect(modelAccess(model("x", { required_account_tier: "future" })).description).toContain("future");
    expect(modelAccess(model("x", { required_account_tier: "constructor" })).description).toContain("constructor");
  });

  it("上游功能未开放独立标暂不可用，不能误导付费可解决", () => {
    const unavailable = model("off", { required_account_tier: "standard", account_available: false,
      account_unavailable_reason: "feature_flag_off" });
    expect(modelAccess(unavailable)).toMatchObject({ label: "暂不可用", vip: false });
    expect(modelAccess(unavailable).description).toContain("与 VIP 套餐权限无关");
    expect(modelAccess(model("unavailable", { account_available: false })).label).toBe("暂不可用");
  });

  it("共享模型的渠道权限独立，不能将 Miora 的要求套到其他渠道", () => {
    const shared = model("shared", { providers: ["miora", "codearts"], required_account_tier: "standard",
      account_available: false, by_provider: { miora: { required_account_tier: "standard",
        account_available: false, account_unavailable_reason: "tier_locked" }, codearts: {} } });
    expect(modelAccess(shared, "miora").vip).toBe(true);
    expect(modelAccess(shared, "codearts").vip).toBe(false);
    expect(modelAccess(shared).vip).toBe(false);
    expect(modelAccess(shared, "trae").available).toBeUndefined();
  });

  it("权限排序稳定，普通在上、VIP在下，原始列表不被修改", () => {
    const models = [model("vip", { required_account_tier: "standard", account_available: false }),
      model("normal-b", { account_available: true }), model("normal-a"),
      model("off", { account_available: false, account_unavailable_reason: "feature_flag_off" }),
      model("vip-2", { required_account_tier: "pro", account_available: true })];
    expect(sortModelsByAccess(models).map(({ id }) => id)).toEqual([
      "normal-b", "normal-a", "vip", "vip-2", "off"]);
    expect(models[0].id).toBe("vip");
    const mapped = groupModelsByProvider(models).get("miora")!;
    expect(mapped.find(({ id }) => id === "vip")?.access?.vip).toBe(true);
    expect(mapped.find(({ id }) => id === "normal-a")?.access).toBeUndefined();
    expect(sortProviderModelsByAccess(mapped).map(({ id }) => id)).toEqual([
      "normal-a", "normal-b", "vip", "vip-2", "off"]);
  });
});
