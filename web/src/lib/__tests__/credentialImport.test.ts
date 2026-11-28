import { describe, expect, it } from "vitest";
import {
  detectProvider,
  headersFromCurl,
  parseCredentialInput,
} from "../credentialImport";

/** 造一个只用于识别 `iss` 的未签名 JWT。 */
function jwt(payload: Record<string, unknown>): string {
  const encode = (value: unknown) =>
    btoa(JSON.stringify(value)).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${encode({ alg: "none" })}.${encode(payload)}.sig`;
}

describe("parseCredentialInput", () => {
  it("渠道 JSON 原样透传，选中渠道没有识别特征时就用它", () => {
    expect(parseCredentialInput('{"token":"abc"}', "codebuddy")).toEqual({
      ok: true,
      provider: "codebuddy",
      credential: { token: "abc" },
      detected: null,
    });
  });

  it("带 provider 的批量导入同形对象会自动拆包", () => {
    const parsed = parseCredentialInput(
      '{"provider":"lingxi","credential":{"cookie":"wps_sid=abc"}}',
      "codebuddy",
    );
    expect(parsed).toEqual({
      ok: true,
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc" },
      detected: "lingxi",
    });
  });

  it("JSON 数组提示走批量导入", () => {
    const parsed = parseCredentialInput('[{"provider":"lingxi"}]', "codebuddy");
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("批量导入");
  });

  it("导出对象保留账号备注和标签，不混入上游凭证", () => {
    const credential = { access_key_id: "synthetic-ak", secret_access_key: "synthetic-sk", nickname: "upstream-name" };
    expect(parseCredentialInput(JSON.stringify({ provider: "codearts", nickname: "  备份账号  ",
      tags: ["自己4594", "备用"], credential }), "miora")).toEqual({
      ok: true, provider: "codearts", detected: "codearts", credential,
      nickname: "备份账号", tags: ["自己4594", "备用"],
    });
    expect(parseCredentialInput(JSON.stringify({ provider: "codearts", nickname: 42,
      tags: "invalid", credential }), "miora", false)).toEqual({
      ok: true, provider: "miora", detected: null, credential,
    });
    expect(parseCredentialInput(JSON.stringify({ provider: "codearts", nickname: "",
      tags: [], credential }), "miora")).toMatchObject({ nickname: "", tags: [] });
  });

  it("TRAE 凭证按字段名识别，即使下拉停在别的渠道", () => {
    const parsed = parseCredentialInput(
      '{"accessToken":"a","uid":"u","refreshToken":"r"}',
      "codebuddy",
    );
    expect(parsed.ok && parsed.provider).toBe("trae");
  });

  it("Miora authToken 优先于共享的 refreshToken 字段，不会误识别成 TRAE", () => {
    expect(parseCredentialInput('{"authToken":"a","refreshToken":"r"}', "trae"))
      .toEqual({
        ok: true, provider: "miora", detected: "miora",
        credential: { authToken: "a", refreshToken: "r" },
      });
  });

  it("Miora snake_case JSON 通过 JWT issuer 识别，不猜其他渠道的 token", () => {
    const token = jwt({ iss: "https://miora.design", sub: "account-1" });
    expect(parseCredentialInput(JSON.stringify({ access_token: token, refresh_token: "r" }), "trae"))
      .toEqual({
        ok: true, provider: "miora", detected: "miora",
        credential: { access_token: token, refresh_token: "r" },
      });
    expect(parseCredentialInput('{"access_token":"opaque","refresh_token":"r"}', "miora"))
      .toMatchObject({ ok: true, provider: "miora", detected: null });
    expect(detectProvider(jwt({ iss: "https://miora.design.attacker.example" }))).toBeNull();
  });

  it("Miora Cookie 和裸 JWT 可导入，refreshToken 缺失由后端标示到期需重登", () => {
    const token = jwt({ iss: "https://miora.design" });
    expect(parseCredentialInput(`authToken=${token}`, "codebuddy")).toEqual({
      ok: true, provider: "miora", detected: "miora", credential: { cookie: `authToken=${token}` },
    });
    expect(parseCredentialInput(token, "trae")).toEqual({
      ok: true, provider: "miora", detected: "miora", credential: { authToken: token },
    });
  });

  it("整段 Cookie 按 wps_sid 自动识别为灵犀", () => {
    expect(parseCredentialInput("wps_sid=abc; uid=42", "codebuddy")).toEqual({
      ok: true,
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc; uid=42" },
      detected: "lingxi",
    });
  });

  it("Cookie: 前缀会被剥掉", () => {
    const parsed = parseCredentialInput("Cookie: xhx_token=abc; path=/", "codebuddy");
    expect(parsed).toEqual({
      ok: true,
      provider: "raccoon",
      credential: { cookie: "xhx_token=abc; path=/" },
      detected: "raccoon",
    });
  });

  it("JWT 的 iss 指向小浣熊时识别为小浣熊", () => {
    const token = jwt({ iss: "xiaohuanxiong.com/iam" });
    expect(parseCredentialInput(token, "codebuddy")).toEqual({
      ok: true,
      provider: "raccoon",
      credential: { access_token: token },
      detected: "raccoon",
    });
  });

  it("单个 xiaohuanxiong2api 账号按 issuer 识别，凭证字段一个都不改", () => {
    const credential = {
      id: "user-42", name: "  小浣熊-1  ", user_id: "42",
      access_token: jwt({ iss: "xiaohuanxiong.com/iam" }),
      refresh_token: "synthetic-refresh", expires_at: 1791529021000,
      enabled: false, priority: 100, accounts: ["custom metadata"],
      metadata: { source: "sample" },
    };
    const text = JSON.stringify(credential);
    expect(parseCredentialInput(text, "codebuddy")).toEqual({
      ok: true, provider: "raccoon", detected: "raccoon", nickname: "小浣熊-1", credential,
    });
    expect(detectProvider(text)).toBe("raccoon");
    expect(parseCredentialInput(text, "qoder", false)).toEqual({
      ok: true, provider: "qoder", detected: null, credential,
    });
  });

  it("明确指定的小浣熊渠道也用账号名，昵称优先于 name", () => {
    const credential = { access_token: "opaque", name: "account", nickname: "  alias  " };
    expect(parseCredentialInput(JSON.stringify(credential), "raccoon", false)).toEqual({
      ok: true, provider: "raccoon", detected: null, nickname: "alias", credential,
    });
  });

  it("不凭共享字段或含小浣熊名字的其它 issuer 选择小浣熊", () => {
    for (const access_token of ["opaque", jwt({ iss: "https://other.example/iam" }),
      jwt({ iss: "https://xiaohuanxiong.com.other.example/iam" }),
      jwt({ iss: "https://other.example/xiaohuanxiong.com/iam" })]) {
      const credential = { access_token, refresh_token: "synthetic-refresh", name: "account" };
      expect(parseCredentialInput(JSON.stringify(credential), "qoder")).toEqual({
        ok: true, provider: "qoder", detected: null, credential,
      });
    }
  });

  it("已有的明确渠道和包装声明优先，不被小浣熊 JWT 提示覆盖", () => {
    const access_token = jwt({ iss: "https://xiaohuanxiong.com/iam" });
    const credential = { access_token, name: "account" };
    expect(detectProvider(JSON.stringify({ ...credential, provider: "qoder" }))).toBe("qoder");
    expect(detectProvider(JSON.stringify({ ...credential, authToken: "miora" }))).toBe("miora");
    expect(detectProvider(JSON.stringify({ ...credential, secret_access_key: "codearts" }))).toBe("codearts");
    expect(detectProvider(JSON.stringify({ provider: "codebuddy", credential }))).toBe("codebuddy");
  });

  it("账号文件提示走批量导入，即使文件里只有一条记录", () => {
    const accounts = [{ access_token: jwt({ iss: "xiaohuanxiong.com/iam" }) }];
    expect(parseCredentialInput(JSON.stringify({ accounts }), "raccoon", false)).toEqual({
      ok: false, error: expect.stringContaining("批量导入"),
    });
  });

  it("裸 token 无识别特征时落到选中渠道", () => {
    const parsed = parseCredentialInput("abcdefghijklmnopqrstuvwxyz", "qoder");
    expect(parsed).toEqual({
      ok: true,
      provider: "qoder",
      credential: { token: "abcdefghijklmnopqrstuvwxyz" },
      detected: null,
    });
  });

  it("裸 token 给 TRAE 时明确拒绝，不落半截凭证", () => {
    const parsed = parseCredentialInput("abcdefghijklmnopqrstuvwxyz", "trae");
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("完整 JSON");
  });

  it("Cookie 给不支持 Cookie 的渠道时明确拒绝", () => {
    const parsed = parseCredentialInput("wps_sid=abc; x=1", "codebuddy", false);
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("不使用 Cookie");
  });

  it("detect=false 时不猜渠道，按传入渠道包装", () => {
    const parsed = parseCredentialInput("wps_sid=abc; uid=1", "codebuddy", false);
    expect(parsed.ok).toBe(false);
    expect(detectProvider("wps_sid=abc; uid=1")).toBe("lingxi");
  });

  it("空输入与无法识别的文本都报错", () => {
    expect(parseCredentialInput("   ", "codebuddy").ok).toBe(false);
    const parsed = parseCredentialInput("not-json", "codebuddy");
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("必须是合法 JSON");
  });
});

describe("headersFromCurl", () => {
  it("解析 -H 的 Cookie / Authorization（大小写不敏感）", () => {
    const curl = [
      "curl 'https://lingxi.wps.cn/api' \\",
      "  -H 'accept: application/json' \\",
      "  -H 'cookie: wps_sid=abc; uid=1' \\",
      "  -H 'Authorization: Bearer tok'",
    ].join("\n");
    expect(headersFromCurl(curl)).toEqual({
      cookie: "wps_sid=abc; uid=1",
      authorization: "Bearer tok",
    });
  });

  it("支持 -b 传整段 Cookie", () => {
    expect(headersFromCurl("curl -b 'xhx_token=abc; path=/' https://x")).toEqual({
      cookie: "xhx_token=abc; path=/",
      authorization: null,
    });
  });

  it("从 cURL 提取 Cookie 后自动识别渠道", () => {
    const parsed = parseCredentialInput(
      "curl 'https://lingxi.wps.cn/x' -H 'cookie: wps_sid=abc; uid=1'",
      "codebuddy",
    );
    expect(parsed).toEqual({
      ok: true,
      provider: "lingxi",
      credential: { cookie: "wps_sid=abc; uid=1" },
      detected: "lingxi",
    });
  });

  it("从 cURL 的 Bearer 里识别小浣熊", () => {
    const token = jwt({ iss: "xiaohuanxiong.com/iam" });
    const parsed = parseCredentialInput(
      `curl 'https://x' -H 'authorization: Bearer ${token}'`,
      "codebuddy",
    );
    expect(parsed.ok && parsed.provider).toBe("raccoon");
    expect(parsed.ok && parsed.credential).toEqual({ access_token: token });
  });

  it("cURL 没有登录头时给出可操作错误", () => {
    const parsed = parseCredentialInput(
      "curl 'https://x' -H 'accept: application/json'",
      "codebuddy",
    );
    expect(parsed.ok).toBe(false);
    if (!parsed.ok) expect(parsed.error).toContain("没有 Cookie 或 Authorization");
  });

  it("Miora cURL 按目标主机识别，并保留 Cookie 和 Bearer", () => {
    expect(parseCredentialInput(
      "curl 'https://miora.design/api/ai/user/profile' -H 'Cookie: locale=zh' -H 'Authorization: Bearer opaque'",
      "codebuddy",
    )).toEqual({
      ok: true, provider: "miora", detected: "miora",
      credential: { cookie: "locale=zh", authToken: "opaque" },
    });
    expect(parseCredentialInput(
      "curl --location 'https://miora.design/api' -H 'Authorization: Bearer opaque'", "codebuddy",
    )).toMatchObject({ ok: true, provider: "miora", credential: { authToken: "opaque" } });
  });

  it("不把其他主机、请求体里的 Miora 链接或坏 URL 识别为 Miora", () => {
    for (const url of ["https://miora.design.attacker.example/api", "https://bad:port/api"]) {
      expect(parseCredentialInput(`curl '${url}' -H 'Authorization: Bearer opaque'`, "qoder"))
        .toMatchObject({ ok: true, provider: "qoder", detected: null });
    }
    expect(parseCredentialInput(
      "curl 'https://example.com/api' -H 'Authorization: Bearer opaque' --data 'https://miora.design/'",
      "qoder",
    )).toMatchObject({ ok: true, provider: "qoder", detected: null });
    expect(parseCredentialInput(
      "curl 'https://miora.design/api' -H 'Authorization: Bearer opaque'", "qoder", false,
    )).toMatchObject({ ok: true, provider: "qoder", detected: null });
  });
});
