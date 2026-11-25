import { describe, expect, it } from "vitest";
import type { ModelInfo } from "../../api/types";
import { isChatModel, mioraModelGroup, modelPurpose, usesMioraOnly } from "../modelCapabilities";

function model(id: string, overrides: Partial<ModelInfo> = {}): ModelInfo {
  return { id, object: "model", owned_by: "2apis", providers: ["miora"], ...overrides };
}

describe("模型用途说明", () => {
  it("生成用途由真实端点决定，媒体专用模型不会成为聊天模型", () => {
    const image = model("gem-3.1", { gateway_endpoints: ["/v1/images/generations"], output_modalities: ["image"] });
    const video = model("vidu-q2-pro-video", { gateway_endpoints: ["/v1/videos"], output_modalities: ["video"] });
    expect(modelPurpose(image).label).toBe("图片生成 · 文生图");
    expect(modelPurpose(image).description).toContain("/v1/images/generations");
    expect(modelPurpose(video).label).toBe("视频生成 · 文生视频");
    expect(isChatModel(image)).toBe(false);
    expect(isChatModel(video)).toBe(false);
    expect(mioraModelGroup(image)).toBe("image");
    expect(mioraModelGroup(video)).toBe("video");
    expect(mioraModelGroup(model("3d", { gateway_endpoints: [] }))).toBe("unconnected");
    expect(modelPurpose(model("3d", { gateway_endpoints: [] })).route).toBe("未接入");
    expect(isChatModel(model("image-only", { output_modalities: ["image"] }))).toBe(false);
    expect(isChatModel(model("mixed", { output_modalities: ["text", "image"] }))).toBe(true);
    expect(isChatModel(model("messages", { gateway_endpoints: ["/v1/messages"] }))).toBe(true);
    expect(isChatModel(model("legacy"))).toBe(true);
  });

  it("共享条目在凭证目录按对应渠道的真实端点说明", () => {
    const shared = model("shared", { providers: ["miora", "codearts"],
      gateway_endpoints: ["/v1/images/generations", "/v1/chat/completions"],
      by_provider: { miora: { gateway_endpoints: ["/v1/images/generations"], output_modalities: ["image"] },
        codearts: { gateway_endpoints: ["/v1/chat/completions"], output_modalities: ["text"] } } });
    expect(modelPurpose(shared, "miora").label).toContain("图片生成");
    expect(isChatModel(shared, "miora")).toBe(false);
    expect(isChatModel(shared, "codearts")).toBe(true);
  });
  it.each(["gpt-6-astra", "gemini-3.8-flash"])("%s 在 Miora 仅说明已接入的文本/代码通路", id => {
    const usage = modelPurpose(model(id, { supports_images: true, supports_tool_call: true }));
    expect(usage.label).toContain("文本/代码");
    expect(usage.label).toContain("客户端工具（JSON模拟）");
    expect(usage.label).not.toMatch(/生图|视频生成|图像理解/);
    expect(usage.description).toContain("工具由客户端执行");
  });

  it.each([
    ["miora-standard", "网页模式 · 快速"], ["miora-pro", "网页模式 · 进阶"],
    ["miora-max", "网页模式 · 专家"], ["default-model", "默认模型路由"],
    ["fast-model", "快速模型路由"], ["balanced-model", "均衡模型路由"],
    ["primary-model", "主模型路由"], ["deep-model", "深度模型路由"],
    ["hy4flash", "标准路由"], ["hy4flash-free", "免费路由"],
    ["deepseek-v4.1-flash", "标准路由"], ["deepseek-v4.1-flash-sg", "SG 路由"],
  ])("%s 的模式/路由标识明确为 %s", (id, expected) => {
    expect(modelPurpose(model(id)).route).toBe(expected);
  });

  it("归一名仍以渠道原始 ID 区分标准与免费路由，未知 ID 不猜用途", () => {
    expect(modelPurpose(model("hy4flash-免费路由", {
      by_provider: { miora: { raw_id: "hy4flash-free" } },
    })).route).toBe("免费路由");
    expect(modelPurpose(model("future-model")).route).toBeNull();
    expect(modelPurpose(model("toString")).route).toBeNull();
  });

  it("通用图片能力仅标注图像理解输入，不推断生图或视频生成", () => {
    const usage = modelPurpose(model("gemini", {
      providers: ["codearts"], supports_images: true,
      supports_tool_call: true, supports_reasoning: true,
    }));
    expect(usage.label).toBe("文本对话 · 图像理解（输入） · 客户端工具 · 推理");
    expect(usage.description).toContain("分析输入图片");
    expect(usage.label).not.toMatch(/生图|视频生成/);
    expect(modelPurpose(model("plain", { providers: ["trae"] })).label).toBe("文本对话");
  });

  it("共享模型按实际选择渠道说明，强制 Miora 不继承其他渠道图片能力", () => {
    const shared = model("glm-5.3", { providers: ["miora", "codearts"], supports_images: true });
    expect(usesMioraOnly(shared, "glm-5.3")).toBe(false);
    expect(usesMioraOnly(shared, "glm-5.3@miora")).toBe(true);
    expect(usesMioraOnly(shared, "glm-5.3@codearts")).toBe(false);
    expect(usesMioraOnly(model("hy4flash"), "hy4flash")).toBe(true);
    expect(usesMioraOnly(undefined, "")).toBe(false);
    expect(modelPurpose(shared, "miora").label).not.toContain("图像理解");
  });
});
