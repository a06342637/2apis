import { describe, expect, it } from "vitest";
import type { Credential } from "../../api/types";
import { loginSuccessNotice, newLoginCredential } from "../upstreamLogin";

describe("upstream login notices and callback detection", () => {
  it("区分新增与合并登录", () => {
    expect(loginSuccessNotice(true)).toContain("更新");
    expect(loginSuccessNotice(false)).toContain("已保存");
  });
  it("不会将别的渠道/发起人/已有行的变化当作当前登录", () => {
    const rows = [{ id: "old", provider: "trae", added_by: "root" },
      { id: "miora", provider: "miora", added_by: "root" },
      { id: "other", provider: "trae", added_by: "other" }] as Credential[];
    expect(newLoginCredential(rows, new Set(["old"]), "trae", "root")).toBe(false);
    rows.push({ id: "new", provider: "trae", added_by: "root" } as Credential);
    expect(newLoginCredential(rows, new Set(["old"]), "trae", "root")).toBe(true);
  });
});
