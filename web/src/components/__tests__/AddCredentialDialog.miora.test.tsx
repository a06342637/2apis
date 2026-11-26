import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AddCredentialDialog } from "../AddCredentialDialog";
import { jsonResponse, renderPage, userEvent } from "@/test-utils";

function setup() {
  localStorage.clear();
  const requests: Record<string, unknown>[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if (String(input) === "/api/credentials" && init?.method === "POST") {
      requests.push(JSON.parse(String(init.body)));
      return jsonResponse({ id: "test-account", merged: false });
    }
    return jsonResponse({});
  }));
  renderPage(<AddCredentialDialog open onClose={vi.fn()} credentialCount={0} hasZen={false} hasKilo={false}
    onImported={vi.fn()} onNotice={vi.fn()} onError={vi.fn()} />);
  return requests;
}

describe("Miora 认证入口", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("从另一渠道模板返回登录页后添加Miora，应切换为Miora认证模板", async () => {
    setup();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "codearts");
    expect((screen.getByTestId("import-payload") as HTMLTextAreaElement).value).toContain("access_key_id");
    await userEvent.click(screen.getByTestId("add-credential-tab-login"));
    await userEvent.click(screen.getByTestId("add-miora-account"));
    expect(screen.getByTestId("import-provider")).toHaveValue("miora");
    expect(screen.getByTestId("import-payload")).toHaveValue('{\n  "authToken": "",\n  "refreshToken": ""\n}');
  });

  it("添加Miora时保留用户已填写的凭证内容", async () => {
    setup();
    const payload = '{"access_token":"synthetic-handwritten-token","refresh_token":"synthetic-refresh"}';
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.click(screen.getByTestId("import-payload"));
    await userEvent.paste(payload);
    await userEvent.click(screen.getByTestId("add-credential-tab-login"));
    await userEvent.click(screen.getByTestId("add-miora-account"));
    expect(screen.getByTestId("import-provider")).toHaveValue("miora");
    expect(screen.getByTestId("import-payload")).toHaveValue(payload);
  });

  it("camelCase Miora凭证在界面识别后保留refreshToken发送到Miora账号池", async () => {
    const requests = setup();
    const encode = (value: object) => btoa(JSON.stringify(value)).replace(/=+$/, "");
    const token = `${encode({ alg: "none" })}.${encode({ iss: "https://miora.design", sub: "test-account" })}.test-signature`;
    const credential = { accessToken: token, refreshToken: "synthetic-refresh" };
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.click(screen.getByTestId("import-payload"));
    await userEvent.paste(JSON.stringify(credential));
    expect(screen.getByTestId("import-provider")).toHaveValue("miora");
    await userEvent.click(screen.getByTestId("import-submit"));
    expect(requests[0]).toMatchObject({ provider: "miora", credential });
  });

  it("导出包装识别为Miora后明确手选Qoder，实际POST必须与当前手选渠道一致", async () => {
    const requests = setup();
    await userEvent.click(screen.getByTestId("add-credential-tab-import"));
    await userEvent.click(screen.getByTestId("import-payload"));
    await userEvent.paste('{"provider":"miora","credential":{"access_token":"synthetic-long-token"}}');
    await userEvent.selectOptions(screen.getByTestId("import-provider"), "qoder");
    expect(screen.getByTestId("import-detected")).toHaveTextContent("当前按手选渠道导入");
    await userEvent.click(screen.getByTestId("import-submit"));
    expect(requests[0]).toMatchObject({ provider: "qoder", credential: { access_token: "synthetic-long-token" } });
  });
});
