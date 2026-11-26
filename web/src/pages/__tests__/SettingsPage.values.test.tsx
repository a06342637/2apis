import { useQueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "../SettingsPage";
import { jsonResponse, renderPage, settle, userEvent } from "@/test-utils";
import { body, mockAll, openGateway, openTask, tasksBody } from "@/test-utils/settings";

function RefreshSettings() {
  const client = useQueryClient();
  return <button onClick={() => void client.invalidateQueries({ queryKey: ["admin"] })}>刷新设置测试</button>;
}

describe("SettingsPage 数值与服务端刷新", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("整数设置不能将小数静默截断后保存", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "1.5");
    expect(screen.getByTestId("save-settings")).toBeDisabled();
    expect(screen.getByTestId("settings-invalid")).toHaveTextContent("填写不合法");
  });

  it("整数设置拒绝超出精确表示范围的数字", async () => {
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "9007199254740993");
    expect(screen.getByTestId("save-settings")).toBeDisabled();
  });

  it("有效科学计数法按输入数值保存，不能只取指数前的整数", async () => {
    const sent: unknown[] = [];
    mockAll((init) => {
      sent.push(JSON.parse(String(init.body)));
      return jsonResponse(body());
    });
    renderPage(<SettingsPage />);
    await settle();
    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "1e2");
    await userEvent.click(screen.getByTestId("save-settings"));
    expect(sent).toEqual([{ values: { quota_probe_minutes: 100 } }]);
  });

  it("后台刷新更新未编辑项，保留其它项已编辑的草稿", async () => {
    let current = body();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
      jsonResponse(String(input).includes("/api/tasks") ? tasksBody() : current)));
    renderPage(<><SettingsPage /><RefreshSettings /></>);
    await settle();
    const quota = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(quota);
    await userEvent.type(quota, "15");
    current = body();
    current.settings[0].value = 90;
    current.settings[1].value = 50;
    await userEvent.click(screen.getByRole("button", { name: "刷新设置测试" }));
    await openGateway("gateway-pacer");
    await waitFor(() => expect(screen.getByTestId("input-pacer_max_seconds")).toHaveValue("50"));
    expect(screen.getByTestId("dirty-count")).toHaveTextContent("1 项待保存");
    await openTask("account", "quota_probe");
    expect(screen.getByTestId("input-quota_probe_minutes")).toHaveValue("15");
  });

  it("修改后改回当前值的项不再算草稿，刷新读取新的服务端值", async () => {
    let current = body();
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) =>
      jsonResponse(String(input).includes("/api/tasks") ? tasksBody() : current)));
    renderPage(<><SettingsPage /><RefreshSettings /></>);
    await settle();
    const input = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(input);
    await userEvent.type(input, "15");
    await userEvent.clear(input);
    await userEvent.type(input, "60");
    current = body();
    current.settings[0].value = 90;
    await userEvent.click(screen.getByRole("button", { name: "刷新设置测试" }));
    await waitFor(() => expect(input).toHaveValue("90"));
    expect(screen.getByTestId("dirty-count")).toHaveTextContent("无改动");
  });
});
