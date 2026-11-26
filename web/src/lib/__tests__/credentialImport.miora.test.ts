import { describe, expect, it } from "vitest";
import { parseCredentialInput } from "../credentialImport";

function mioraToken() {
  const encode = (value: object) => btoa(JSON.stringify(value))
    .replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${encode({ alg: "none" })}.${encode({ iss: "https://miora.design", sub: "test-account" })}.test-signature`;
}

describe("Miora 认证输入与后端别名契约", () => {
  it("后端接受的accessToken别名按JWT issuer识别，不能因refreshToken误入TRAE", () => {
    const token = mioraToken();
    const credential = { accessToken: token, refreshToken: "synthetic-refresh" };
    expect(parseCredentialInput(JSON.stringify(credential), "trae")).toEqual({
      ok: true, provider: "miora", detected: "miora", credential,
    });
  });

  it("空的优先别名不遮蔽后端接受的有效accessToken", () => {
    const credential = { access_token: "  ", accessToken: mioraToken(), refreshToken: "synthetic-refresh" };
    expect(parseCredentialInput(JSON.stringify(credential), "trae")).toEqual({
      ok: true, provider: "miora", detected: "miora", credential,
    });
  });

  it("TRAE的opaque accessToken仍按字段识别", () => {
    const credential = { accessToken: "synthetic-trae-access", refreshToken: "synthetic-trae-refresh" };
    expect(parseCredentialInput(JSON.stringify(credential), "miora")).toEqual({
      ok: true, provider: "trae", detected: "trae", credential,
    });
  });

  it.each(["Bearer ", "bEaReR\t "])("复制的%s值可直接导入Miora，认证前缀不能成为token的一部分", (prefix) => {
    const token = mioraToken();
    expect(parseCredentialInput(`${prefix}${token}`, "codebuddy")).toEqual({
      ok: true, provider: "miora", detected: "miora", credential: { authToken: token },
    });
  });

  it.each(["", "Bearer "])("opaque原始token保留手选渠道与原始token值（前缀%s）", (prefix) => {
    const token = "synthetic-opaque-token";
    expect(parseCredentialInput(`${prefix}${token}`, "miora")).toEqual({
      ok: true, provider: "miora", detected: null, credential: { authToken: token },
    });
  });

  it("手选渠道覆盖导出包装中的provider，与平面JSON的detect=false行为一致", () => {
    const credential = { access_token: mioraToken(), refresh_token: "synthetic-refresh" };
    expect(parseCredentialInput(JSON.stringify({ provider: "miora", credential }), "qoder", false)).toEqual({
      ok: true, provider: "qoder", detected: null, credential,
    });
  });

  it("未手选时继续采用导出包装中的provider", () => {
    const credential = { access_token: mioraToken(), refresh_token: "synthetic-refresh" };
    expect(parseCredentialInput(JSON.stringify({ provider: "miora", credential }), "qoder")).toEqual({
      ok: true, provider: "miora", detected: "miora", credential,
    });
  });
});
