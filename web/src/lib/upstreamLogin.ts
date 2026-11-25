import type { Credential } from "../api/types";

export function loginSuccessNotice(merged: boolean | undefined): string {
  return merged ? "登录成功，已更新该账号的凭证。" : "登录成功，凭证已保存。";
}

/** callback 没有 poll API；只检测本渠道、发起人新增加的凭证，忽略余额刷新。 */
export function newLoginCredential(
  credentials: Credential[], before: Set<string>, provider: string, viewer: string,
): boolean {
  return credentials.some(row => row.provider === provider && !before.has(row.id)
    && (!viewer || row.added_by === viewer));
}
