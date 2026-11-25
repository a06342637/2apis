import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse } from "@/test-utils";
import { createCredentialGroup, deleteCredentialGroup, listCredentialGroups, updateCredentialGroup } from "../credentialGroups";

describe("credentialGroups API contract", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("读取遵守服务端readonly成员裁剪，不生成或猜测成员", async () => {
    const body = { groups: [{ id: "g", provider: "miora", name: "Name", member_count: 2 }], is_operator: false };
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(body)));
    expect(await listCredentialGroups()).toEqual(body);
    expect(fetch).toHaveBeenCalledWith("/api/credential-groups", expect.objectContaining({ credentials: "same-origin" }));
  });

  it("坏列表不能悄悄当成零分组", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({})));
    await expect(listCredentialGroups()).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("create/rename/delete使用稳定id和CSRF自定义头，PUT不改provider", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ ok: true })));
    await createCredentialGroup({ provider: "miora", name: "new", credential_ids: ["a"] });
    await updateCredentialGroup("id/name?", { name: "renamed", credential_ids: [], expected_updated_at: 42 });
    await deleteCredentialGroup("id/name?");
    const calls = vi.mocked(fetch).mock.calls;
    expect(calls[0][0]).toBe("/api/credential-groups");
    expect(calls[1][0]).toBe("/api/credential-groups/id%2Fname%3F");
    expect(calls[2][0]).toBe(calls[1][0]);
    expect(JSON.parse(String(calls[1][1]?.body))).toEqual({ name: "renamed", credential_ids: [], expected_updated_at: 42 });
    for (const [, init] of calls) expect(init).toMatchObject({
      credentials: "same-origin", headers: { "X-Requested-With": "XMLHttpRequest" },
    });
    expect(calls[2][1]?.method).toBe("DELETE");
  });

  it("409乐观锁错误保留可识别状态，不自动重试写入", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ error: { code: "conflict", message: "changed" } }, 409)));
    await expect(updateCredentialGroup("id", { name: "Name", expected_updated_at: 1 }))
      .rejects.toMatchObject({ status: 409, code: "conflict" });
    expect(fetch).toHaveBeenCalledOnce();
  });
});
