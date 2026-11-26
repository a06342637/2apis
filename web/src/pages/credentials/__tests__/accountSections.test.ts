import { describe, expect, it } from "vitest";
import type { Credential, CredentialGroup } from "../../../api/types";
import { groupByProvider } from "../../../lib/credentialList";
import { makeCredential } from "@/test-utils";
import { accountSections, withAccountGroupProviders } from "../accountSections";

function group(provider: Credential["provider"] = "miora") {
  return groupByProvider(["b", "a", "c"].map((id) => makeCredential({ id, provider }) as Credential))[0];
}
function saved(id: string, provider: CredentialGroup["provider"], members: string[]): CredentialGroup {
  return { id, provider, name: id, credential_ids: members, member_count: members.length, created_at: 1, updated_at: 1 };
}

describe("二级账号组分区", () => {
  it("缺少组数据时保留渠道展示，不能把未知关系当成未分组", () => {
    expect(accountSections(group(), undefined)).toBeNull();
  });
  it("组内保留后端排序，重叠会员可各自显示，未归组单独列出", () => {
    const sections = accountSections(group(), [saved("one", "miora", ["a", "b"]), saved("two", "miora", ["a"]), saved("other", "codearts", ["c"])])!;
    expect(sections.map((section) => [section.key, section.items.map((item) => item.id)])).toEqual([
      ["one", ["b", "a"]], ["two", ["a"]], ["ungrouped-miora", ["c"]],
    ]);
  });
  it("成员ID缺失时只展示组信息，其余账号不标成未分组", () => {
    const hidden = saved("hidden", "miora", ["a"]);
    delete hidden.credential_ids;
    const sections = accountSections(group(), [hidden])!;
    expect(sections[0]).toMatchObject({ memberCount: 1, membershipKnown: false, items: [] });
    expect(sections[1]).toMatchObject({ name: "渠道账号", items: group().items });
  });
  it.each(["codearts", "miora", "trae", "codebuddy", "qoder", "officeace", "lingxi"] as const)("%s 可使用相同二级分组", (provider) => {
    expect(accountSections(group(provider), [saved("one", provider, ["a"])])![0].items.map((item) => item.id)).toEqual(["a"]);
  });
  it("空组保留渠道入口、正确账本和0账号，不影响已有渠道的对象", () => {
    const original = group();
    const result = withAccountGroupProviders([original], [saved("ca-empty", "codearts", []), saved("ca-empty-2", "codearts", [])]);
    expect(result).toHaveLength(2);
    expect(result[0]).toBe(original);
    expect(result[1]).toMatchObject({ key: "codearts", ledger: "credits", items: [], remaining: null, total: null });
  });
});
