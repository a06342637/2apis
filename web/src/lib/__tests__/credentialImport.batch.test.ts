import { describe, expect, it } from "vitest";
import { parseBatchCredentialInput } from "../credentialImport";

function jwt(uid: string) {
  const part = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, "")
    .replace(/\+/g, "-").replace(/\//g, "_");
  return part({ alg: "RS256", typ: "JWT" }) + "."
    + part({ iss: "https://miora.design", sub: uid }) + ".synthetic-signature";
}

describe("批量凭证边界和邮箱昵称", () => {
  it("邮箱紧贴JSON对象仍作为独立备注，支持漂亮JSON并覆盖原昵称", () => {
    const parsed = parseBatchCredentialInput([
      "one@example.com" + JSON.stringify({ authToken: jwt("one"), refreshToken: "r1", nickname: "old" }, null, 2),
      "two@example.com" + JSON.stringify({ provider: "miora", nickname: "another-old", tags: ["tag"],
        credential: { authToken: jwt("two"), refreshToken: "r2" } }),
    ].join("\r\n"));
    expect(parsed.errors).toEqual([]);
    expect(parsed.entries).toMatchObject([
      { nickname: "one@example.com", credential: { authToken: jwt("one"), refreshToken: "r1" } },
      { nickname: "two@example.com", tags: ["tag"], credential: { authToken: jwt("two"), refreshToken: "r2" } },
    ]);
  });

  it("多行邮箱紧贴花括号Cookie，每个账号保留自己的认证票和刷新票", () => {
    const parsed = parseBatchCredentialInput([
      `one@example.com{authToken=${jwt("one")}; refreshToken=r1}`,
      `two@example.com{Cookie: authToken="${jwt("two")}"; refreshToken='r2';}`,
    ].join("\r\n"), "codebuddy");
    expect(parsed.errors).toEqual([]);
    expect(parsed.entries).toMatchObject([
      { provider: "miora", nickname: "one@example.com", credential: { authToken: jwt("one"), refreshToken: "r1" } },
      { provider: "miora", nickname: "two@example.com", credential: { authToken: jwt("two"), refreshToken: "r2" } },
    ]);
  });

  it("包裹Cookie的坏行、票据冲突和相邻好行彼此隔离，失败回填仍不借票", () => {
    const parsed = parseBatchCredentialInput([
      "missing@example.com{refreshToken=only-refresh}",
      "conflict@example.com{authToken=one; authToken=two; refreshToken=conflicting-refresh}",
      `good@example.com{authToken=${jwt("good")}; refreshToken=good-refresh}`,
      "bad-json@example.com{\"authToken\":\"broken\",",
      `next@example.com{"authToken":"${jwt("next")}","refreshToken":"next-refresh"}`,
    ].join("\n"));
    expect(parsed.errors.map(error => error.line)).toEqual([1, 2, 4]);
    expect(parsed.entries).toMatchObject([
      { nickname: "good@example.com", credential: { authToken: jwt("good"), refreshToken: "good-refresh" } },
      { nickname: "next@example.com", credential: { authToken: jwt("next"), refreshToken: "next-refresh" } },
    ]);
    expect(parsed.unparsed[0]).toContain("missing@example.com");
    expect(parsed.unparsed[1]).toContain("conflict@example.com");
    expect(parseBatchCredentialInput(parsed.unparsed.join("\n")).entries).toEqual([]);
  });

  it("包裹Cookie必须独立成行，拆行或尾随内容不会被拼成其它账号", () => {
    const parsed = parseBatchCredentialInput([
      "split@example.com{authToken=split-account;",
      "refreshToken=another-account-refresh}",
      "suffix@example.com{authToken=account; refreshToken=refresh}unexpected-opaque-account",
      "valid@example.com{authToken=valid-account; refreshToken=valid-refresh}",
    ].join("\n"));
    expect(parsed.errors.map(error => error.line)).toEqual([1, 2, 3]);
    expect(parsed.entries).toMatchObject([
      { nickname: "valid@example.com", credential: { authToken: "valid-account", refreshToken: "valid-refresh" } },
    ]);
  });

  it("重复行保持逐行索引供服务端跳过，同值Cookie别名允许，不同刷新票拒绝", () => {
    const line = "same@example.com{authToken=account; authToken=account; refreshToken=refresh}";
    const parsed = parseBatchCredentialInput(line + "\n" + line);
    expect(parsed.errors).toEqual([]);
    expect(parsed.entries).toHaveLength(2);
    expect(parsed.entries[0]).toEqual(parsed.entries[1]);
    expect(parseBatchCredentialInput("bad@example.com{authToken=account; refreshToken=r1; refreshToken=r2}").entries).toEqual([]);
  });

  it("多个漂亮JSON和相邻JSON对象分别识别，字符串里的花括号不截断", () => {
    const first = { authToken: jwt("one"), refreshToken: "r1", nickname: 'note { "} \\' };
    const second = { authToken: jwt("two"), refreshToken: "r2" };
    const parsed = parseBatchCredentialInput(JSON.stringify(first, null, 2) + JSON.stringify(second));
    expect(parsed.errors).toEqual([]);
    expect(parsed.entries).toMatchObject([
      { provider: "miora", credential: { authToken: first.authToken, refreshToken: "r1" } },
      { provider: "miora", credential: { authToken: second.authToken, refreshToken: "r2" } },
    ]);
  });

  it("邮箱同一行或上一行分别绑定自己的JSON、Cookie和token", () => {
    const token = jwt("raw");
    const parsed = parseBatchCredentialInput([
      "first@example.com",
      JSON.stringify({ authToken: jwt("json"), refreshToken: "json-refresh" }, null, 2),
      "second@example.com Cookie: authToken=cookie-account; refreshToken=cookie-refresh",
      "third@example.com Bearer " + token,
    ].join("\r\n"));
    expect(parsed.errors).toEqual([]);
    expect(parsed.entries.map((entry) => entry.nickname)).toEqual([
      "first@example.com", "second@example.com", "third@example.com",
    ]);
    expect(parsed.entries[1].credential).toMatchObject({
      authToken: "cookie-account", refreshToken: "cookie-refresh",
    });
    expect(parsed.entries[2].credential).toMatchObject({ authToken: token });
  });

  it("JSON数组/原有包装保留provider、显式nickname和tags", () => {
    const parsed = parseBatchCredentialInput(JSON.stringify([
      { provider: "codebuddy", credential: { token: "old" }, nickname: "custom", tags: ["tag"] },
      { authToken: jwt("flat"), refreshToken: "flat-refresh" },
    ]));
    expect(parsed.errors).toEqual([]);
    expect(parsed.entries[0]).toEqual({
      provider: "codebuddy", credential: { token: "old" }, nickname: "custom", tags: ["tag"],
    });
    expect(parsed.entries[1].provider).toBe("miora");
  });

  it("原始opaque token保留所选默认渠道，不会合并相邻token", () => {
    const parsed = parseBatchCredentialInput("opaque-account-one\nopaque-account-two", "codebuddy");
    expect(parsed.errors).toEqual([]);
    expect(parsed.entries.map((entry) => entry.credential)).toEqual([
      { token: "opaque-account-one" }, { token: "opaque-account-two" },
    ]);
  });

  it("Cookie里的刷新票保留，重复同值允许而不同票据拒绝", () => {
    const valid = parseBatchCredentialInput(
      "authToken=opaque-account; authToken=opaque-account; refreshToken=refresh");
    expect(valid.entries[0].credential).toMatchObject({
      authToken: "opaque-account", refreshToken: "refresh",
    });
    expect(parseBatchCredentialInput("authToken=one; authToken=two").entries).toEqual([]);
    expect(parseBatchCredentialInput(
      "authToken=one; refreshToken=one-refresh; refreshToken=two-refresh").entries).toEqual([]);
  });

  it.each([
    { authToken: jwt("one"), access_token: jwt("two") },
    { authToken: jwt("one"), refreshToken: "one", refresh_token: "two" },
  ])("同条JSON中的不同账号票据不能静默选其中一份", (credential) => {
    const parsed = parseBatchCredentialInput(JSON.stringify(credential));
    expect(parsed.entries).toEqual([]);
    expect(parsed.errors[0].message).toContain("多份不同票据");
  });

  it("截断JWT和疑似签名续行都拒绝，后面的完整JWT仍单独识别", () => {
    const token = jwt("broken");
    const parsed = parseBatchCredentialInput([
      token.slice(0, token.lastIndexOf(".") + 1),
      "this-is-an-opaque-signature-continuation",
      jwt("next"),
    ].join("\n"));
    expect(parsed.errors).toHaveLength(2);
    expect(parsed.entries).toHaveLength(1);
    expect(parsed.entries[0].credential).toMatchObject({ authToken: jwt("next") });
  });

  it("明确空行分界后可继续导入opaque token", () => {
    const parsed = parseBatchCredentialInput("eyJtruncated-token\n\nopaque-account-next");
    expect(parsed.errors).toHaveLength(1);
    expect(parsed.entries[0].credential).toMatchObject({ authToken: "opaque-account-next" });
  });

  it("坏JSON只留在失败记录里，后续邮箱和完整票据不会借用前一个邮箱", () => {
    const parsed = parseBatchCredentialInput([
      "bad@example.com", "{broken", "good@example.com", jwt("good"),
    ].join("\n"));
    expect(parsed.errors).toHaveLength(1);
    expect(parsed.entries[0].nickname).toBe("good@example.com");
    expect(parsed.unparsed[0]).toContain("bad@example.com");
  });

  it("只给邮箱或把一个邮箱放在多账号数组前都拒绝，不猜对应账号", () => {
    expect(parseBatchCredentialInput("missing@example.com").errors[0].message).toContain("缺少");
    const parsed = parseBatchCredentialInput("one@example.com " + JSON.stringify([
      { authToken: jwt("one") }, { authToken: jwt("two") },
    ]));
    expect(parsed.entries).toEqual([]);
    expect(parsed.errors[0].message).toContain("一个邮箱只能");
  });

  it("一个邮箱可以对应单元素数组，明确邮箱优先于JSON昵称", () => {
    const parsed = parseBatchCredentialInput("mail@example.com " + JSON.stringify([
      { provider: "miora", credential: { authToken: jwt("one") }, nickname: "old-name" },
    ]));
    expect(parsed.entries[0].nickname).toBe("mail@example.com");
  });

  it("数组中的非法长字符串回填后仍是错误，不变成opaque账号", () => {
    const parsed = parseBatchCredentialInput(JSON.stringify(["not-a-valid-account-object"]));
    expect(parsed.entries).toEqual([]);
    const retry = parseBatchCredentialInput(parsed.unparsed.join("\n"));
    expect(retry.entries).toEqual([]);
    expect(retry.errors).toHaveLength(1);
  });

  it("未给Miora authToken的片段不会借用上一条记录的刷新票", () => {
    const parsed = parseBatchCredentialInput(
      '{"authToken":"account-one"}\n{"refresh_token":"another-account-refresh"}');
    expect(parsed.entries).toHaveLength(1);
    expect(parsed.entries[0].credential).not.toHaveProperty("refreshToken");
    expect(parsed.errors).toHaveLength(1);
  });

  it("camelCase刷新票单独一条也不能误识别成其它渠道", () => {
    const parsed = parseBatchCredentialInput('{"refreshToken":"synthetic-refresh-only"}');
    expect(parsed.entries).toEqual([]);
    expect(parsed.errors[0].message).toContain("缺少");
    expect(parseBatchCredentialInput(parsed.unparsed.join("\n")).entries).toEqual([]);
  });

  it("空白Miora昵称留空由后端命名，自定义昵称去掉两端空白", () => {
    const parsed = parseBatchCredentialInput(JSON.stringify([
      { authToken: "synthetic-one", nickname: "   " },
      { authToken: "synthetic-two", nickname: "  my account  " },
    ]));
    expect(parsed.entries.map((entry) => entry.nickname)).toEqual(["", "my account"]);
  });
});
