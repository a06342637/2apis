import { useQuery } from "@tanstack/react-query";
import { requestApiKeyJson } from "./apiKeys";
import type { ModelInfo } from "./types";

/** 管理台完整目录含锁定模型，使用独立缓存，不污染客户端/聊天可调用模型列表。 */
export function useDisplayModels(username?: string) {
  return useQuery({
    queryKey: ["model-display", username ?? "anonymous"],
    queryFn: () => requestApiKeyJson<{ object: string; data: ModelInfo[] }>(
      "/api/playground/models?include_unavailable=true"),
  });
}
