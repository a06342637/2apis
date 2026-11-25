import type { ModelInfo } from "../api/types";

export interface ModelPurpose {
  label: string;
  description: string;
  route: string | null;
}

const MIORA_ROUTES: Record<string, string> = {
  "miora-standard": "网页模式 · 快速",
  "miora-pro": "网页模式 · 进阶",
  "miora-max": "网页模式 · 专家",
  "default-model": "默认模型路由",
  "fast-model": "快速模型路由",
  "balanced-model": "均衡模型路由",
  "primary-model": "主模型路由",
  "deep-model": "深度模型路由",
  hy4flash: "标准路由",
  "hy4flash-free": "免费路由",
  "deepseek-v4.1-flash": "标准路由",
  "deepseek-v4.1-flash-sg": "SG 路由",
};

/** 未接入的官网 3D 入口参考；图像和视频目录由真实账号 API 动态下发。 */
export const MIORA_OFFICIAL_MEDIA = [
  { label: "3D 生成", models: ["H3D3.1 / 3.0", "Tripo3.1 / P1", "Miora3D（beta）"] },
] as const;

const CHAT_ENDPOINTS = ["/v1/chat/completions", "/v1/responses", "/v1/messages"];
export const MIORA_MODEL_GROUP_LABELS = {
  chat: "文本/代码 · 可调用", image: "图片生成 · 可调用",
  video: "视频生成 · 可调用", unconnected: "目录模型 · 未接入",
} as const;

function endpoints(model: ModelInfo, provider?: string): string[] | undefined {
  return (provider ? model.by_provider?.[provider]?.gateway_endpoints : undefined) ?? model.gateway_endpoints;
}

function outputs(model: ModelInfo, provider?: string): string[] | undefined {
  return (provider ? model.by_provider?.[provider]?.output_modalities : undefined) ?? model.output_modalities;
}

/** 媒体专用模型不能成为聊天默认选项；旧目录缺少新字段时仍按文本处理。 */
export function isChatModel(model: ModelInfo, provider?: string): boolean {
  const routes = endpoints(model, provider);
  if (routes !== undefined) return routes.some(route => CHAT_ENDPOINTS.includes(route));
  const modalities = outputs(model, provider);
  return modalities === undefined || modalities.includes("text");
}

export function mioraModelGroup(model: ModelInfo): keyof typeof MIORA_MODEL_GROUP_LABELS {
  const routes = endpoints(model, "miora");
  if (routes?.includes("/v1/images/generations")) return "image";
  if (routes?.includes("/v1/videos")) return "video";
  return isChatModel(model, "miora") ? "chat" : "unconnected";
}

/** 能力来自真实端点元数据；模型名称不作为媒体接入的依据。 */
export function modelPurpose(model: ModelInfo, provider?: string): ModelPurpose {
  const channel = provider ?? (model.providers.length === 1 ? model.providers[0] : "");
  if (channel === "miora") {
    const group = mioraModelGroup(model);
    if (group === "image" || group === "video") {
      const endpoint = group === "image" ? "/v1/images/generations" : "/v1/videos";
      return { label: group === "image" ? "图片生成 · 文生图" : "视频生成 · 文生视频",
        description: `通过 ${endpoint} 调用；模型可用性和参数按当前账号目录确定。`, route: "已接入生成接口" };
    }
    if (group === "unconnected") {
      return { label: "未接入调用接口", description: "该条目尚未声明可调用的网关端点。", route: "未接入" };
    }
    const raw = model.by_provider?.miora?.raw_id ?? model.id;
    return {
      label: "文本/代码 · 客户端工具（JSON模拟）",
      description: "用于文本问答、代码生成与分析；工具由客户端执行。",
      route: Object.hasOwn(MIORA_ROUTES, raw) ? MIORA_ROUTES[raw] : null,
    };
  }
  const capabilities = ["文本对话"];
  if (model.supports_images === true) capabilities.push("图像理解（输入）");
  if (model.supports_tool_call === true) capabilities.push("客户端工具");
  if (model.supports_reasoning === true) capabilities.push("推理");
  return {
    label: capabilities.join(" · "),
    description: model.supports_images === true
      ? "图像理解指分析输入图片；生成图片或视频需独立的生成接口。"
      : "按上游已声明的能力提供对话服务。",
    route: null,
  };
}

export function usesMioraOnly(model: ModelInfo | undefined, value: string): boolean {
  const forced = value.split("@")[1];
  if (forced) return forced === "miora";
  return model?.providers.length === 1 && model.providers[0] === "miora";
}
