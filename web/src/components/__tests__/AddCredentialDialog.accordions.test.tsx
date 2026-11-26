import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AddCredentialDialog } from "../AddCredentialDialog";
import * as clipboard from "../../lib/clipboard";

function setup() {
  localStorage.clear();
  render(<AddCredentialDialog open onClose={vi.fn()} credentialCount={0} hasZen={false}
    hasKilo={false} onImported={vi.fn()} onNotice={vi.fn()} onError={vi.fn()} />);
}

describe("凭证渠道说明统一折叠", () => {
  afterEach(() => vi.restoreAllMocks());
  it("Miora 与手工渠道默认折叠，使用和登录链接相同的卡片结构", async () => {
    setup();
    const base = screen.getByTestId("login-link-codebuddy");
    for (const id of ["miora-login-entry", "manual-raccoon", "manual-lingxi", "manual-free"]) {
      const details = screen.getByTestId(id) as HTMLDetailsElement;
      expect(details.tagName).toBe("DETAILS");
      expect(details.open).toBe(false);
      expect(details.className).toBe(base.className);
      expect(details.querySelector("summary")).toHaveClass("cursor-pointer", "text-xs", "font-medium");
      await userEvent.click(details.querySelector("summary")!);
      expect(details.open).toBe(true);
      await userEvent.click(details.querySelector("summary")!);
      expect(details.open).toBe(false);
    }
  });

  it("展开Miora可以转入JSON导入，导入说明仍可折叠", async () => {
    setup();
    await userEvent.click(screen.getByTestId("miora-login-summary"));
    await userEvent.click(screen.getByTestId("add-miora-account"));
    expect(screen.getByTestId("import-provider")).toHaveValue("miora");
    const details = screen.getByTestId("miora-json-help") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    await userEvent.click(details.querySelector("summary")!);
    expect(details.open).toBe(true);
    expect(within(details).getByTestId("miora-import-help")).toBeVisible();
  });

  it("登录与JSON导入中的命令点击均复制完整文本并提示结果", async () => {
    const copy = vi.spyOn(clipboard, "copyText").mockResolvedValue(true);
    setup();
    await userEvent.click(screen.getByTestId("miora-login-summary"));
    await userEvent.click(screen.getByTestId("miora-copy-command"));
    expect(copy).toHaveBeenCalledWith('copy(JSON.stringify({authToken:localStorage.getItem("authToken"),refreshToken:localStorage.getItem("refreshToken")},null,2))');
    expect(screen.getByTestId("miora-copy-command")).toHaveTextContent("已复制");
    await userEvent.click(screen.getByTestId("add-miora-account"));
    await userEvent.click(screen.getByTestId("miora-json-help").querySelector("summary")!);
    await userEvent.click(screen.getByTestId("miora-copy-command"));
    expect(copy).toHaveBeenCalledTimes(2);
    expect(screen.getByTestId("miora-copy-command")).toHaveTextContent("已复制");
  });

  it("复制失败不会显示成功，保留代码供手动复制", async () => {
    vi.spyOn(clipboard, "copyText").mockResolvedValue(false);
    setup();
    await userEvent.click(screen.getByTestId("miora-login-summary"));
    await userEvent.click(screen.getByTestId("miora-copy-command"));
    expect(screen.getByTestId("miora-copy-command")).toHaveTextContent("复制失败，请选中代码手动复制");
    expect(screen.getByTestId("miora-copy-command").querySelector("code")).toHaveTextContent("copy(JSON.stringify");
  });
});
