import { ApiError, request } from "./client";
import type { CredentialGroup, CredentialGroupsResponse, Provider } from "./types";

export async function listCredentialGroups(signal?: AbortSignal): Promise<CredentialGroupsResponse> {
  const result = await request<CredentialGroupsResponse>("/api/credential-groups", { signal });
  if (!Array.isArray(result?.groups)) throw new ApiError(200, "invalid_response", "账号分组列表格式无效");
  return result;
}

export function createCredentialGroup(payload: {
  provider: Provider; name: string; credential_ids: string[];
}): Promise<CredentialGroup> {
  return request("/api/credential-groups", { method: "POST", body: JSON.stringify(payload) });
}

export function updateCredentialGroup(id: string, payload: {
  name?: string; credential_ids?: string[]; expected_updated_at?: number;
}): Promise<CredentialGroup> {
  return request(`/api/credential-groups/${encodeURIComponent(id)}`, {
    method: "PUT", body: JSON.stringify(payload),
  });
}

export function deleteCredentialGroup(id: string): Promise<{ ok: boolean }> {
  return request(`/api/credential-groups/${encodeURIComponent(id)}`, { method: "DELETE" });
}
