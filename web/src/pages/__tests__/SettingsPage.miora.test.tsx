import { screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeSetting } from "../../api/types";
import { jsonResponse, renderPage, userEvent } from "@/test-utils";
import { body, GROUPS, openGateway, openTask, tasksBody } from "@/test-utils/settings";
import { SettingsPage } from "../SettingsPage";

const MIORA_SETTINGS: RuntimeSetting[] = [
  { key: "miora_chat_min_interval", label: "Miora 聊天最小间隔（秒）", kind: "float", value: 0, default: 5 },
  { key: "miora_max_concurrency", label: "Miora 每账号并发上限", kind: "int", value: 0, default: 1 },
  { key: "miora_request_window_seconds", label: "Miora 滚动窗口（秒）", kind: "float", value: 30, default: 60 },
  { key: "miora_max_requests_per_window", label: "Miora 窗口内最多请求数", kind: "int", value: 10, default: 0 },
].map((row) => ({ ...row, env_name: row.key.toUpperCase(), description: "按账号独立限速。",
  overridden: true, task: null, group: "miora_throttle", section: null }));
const SUGGESTED = {
  miora_chat_min_interval: 5,
  miora_max_concurrency: 1,
  miora_request_window_seconds: 60,
  miora_max_requests_per_window: 3,
};

function setup(options: { failSettings?: boolean; rejectSave?: boolean; noTasks?: boolean } = {}) {
  const settings: RuntimeSetting[] = [...body().settings, ...MIORA_SETTINGS.map((row) => ({ ...row }))];
  const writes: Record<string, unknown>[] = [];
  const response = () => ({ settings, groups: GROUPS,
    overridden: settings.filter((setting) => setting.overridden).length });
  const fetchSpy = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === "/api/system-updates") return jsonResponse({ enabled: false });
    if (url === "/api/settings") {
      if (init?.method === "PUT") {
        const { values } = JSON.parse(String(init.body)) as {
          values: Record<string, RuntimeSetting["value"] | null>;
        };
        writes.push(values);
        if (options.rejectSave) return jsonResponse({ error: { code: "invalid", message: "限速设置无效" } }, 400);
        for (const setting of settings) {
          if (setting.key in values) {
            setting.value = values[setting.key] ?? setting.default;
            setting.overridden = values[setting.key] !== null;
          }
        }
      } else if (options.failSettings) {
        return jsonResponse({ error: { code: "unavailable", message: "设置服务暂不可用" } }, 503);
      }
      return jsonResponse(response());
    }
    if (url === "/api/tasks") {
      const tasks = tasksBody();
      return jsonResponse(options.noTasks ? { ...tasks, tasks: [], groups: [] } : tasks);
    }
    return jsonResponse({});
  });
  vi.stubGlobal("fetch", fetchSpy);
  renderPage(<SettingsPage />);
  return { writes, fetchSpy };
}

async function openMiora() {
  await screen.findByTestId("settings-sections-gateway");
  await openGateway("gateway-miora_throttle");
  return screen.findByTestId("miora-account-settings-help");
}

async function replace(key: string, value: string) {
  const input = screen.getByTestId(`input-${key}`);
  await userEvent.clear(input);
  await userEvent.type(input, value);
}

describe("SettingsPage Miora 限速与系统更新", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("独立显示 Miora 四项配置、当前值、默认值与账号限速说明", async () => {
    setup();
    const help = await openMiora();
    expect(screen.getByRole("tab", { name: "Miora 账号与限速" })).toHaveAttribute("aria-selected", "true");
    for (const setting of MIORA_SETTINGS) {
      expect(screen.getByTestId(`input-${setting.key}`)).toHaveValue(String(setting.value));
      expect(screen.getByTestId(`default-${setting.key}`)).toHaveTextContent(String(setting.default));
      expect(screen.getByTestId(`source-${setting.key}`)).toHaveTextContent("DB 覆盖");
    }
    expect(help).toHaveTextContent("不同账号独立排队");
    expect(help).toHaveTextContent("默认不限制窗口内请求次数");
    expect(help).toHaveTextContent("每 60 秒最多 3 次请求");
    expect(help).toHaveTextContent("上游的硬门槛");
    expect(help).toHaveTextContent("Retry-After");
  });

  it("填入建议只修改 Miora 草稿，不自动提交，并保留其他组草稿", async () => {
    const { writes } = setup();
    await screen.findByTestId("input-quota_probe_minutes");
    await replace("quota_probe_minutes", "15");
    await openGateway("gateway-pacer");
    await replace("pacer_max_seconds", "30");
    await openMiora();
    await userEvent.click(screen.getByTestId("suggest-miora-limits"));
    for (const [key, value] of Object.entries(SUGGESTED)) {
      expect(screen.getByTestId(`input-${key}`)).toHaveValue(String(value));
    }
    expect(writes).toEqual([]);
    expect(screen.getByTestId("settings-notice")).toHaveTextContent("点击「保存」后生效");
    expect(screen.getByTestId("dirty-count")).toHaveTextContent("6 项待保存");
    await openGateway("gateway-pacer");
    expect(screen.getByTestId("input-pacer_max_seconds")).toHaveValue("30");
    await openTask("account", "quota_probe");
    expect(screen.getByTestId("input-quota_probe_minutes")).toHaveValue("15");
  });

  it("保存建议和其他组改动时发送标量，刷新后草稿归零且显示生效值", async () => {
    const { writes } = setup();
    await screen.findByTestId("input-quota_probe_minutes");
    await replace("quota_probe_minutes", "15");
    await openMiora();
    await userEvent.click(screen.getByTestId("suggest-miora-limits"));
    await userEvent.click(screen.getByTestId("save-settings"));
    await waitFor(() => {
      expect(screen.getByTestId("dirty-count")).toHaveTextContent("无改动");
      expect(screen.getByTestId("input-miora_chat_min_interval")).toHaveValue("5");
    });
    expect(writes).toEqual([{ quota_probe_minutes: 15, ...SUGGESTED }]);
    expect(screen.getByTestId("settings-notice")).toHaveTextContent("已保存 5 项，立即生效");
    for (const [key, value] of Object.entries(SUGGESTED)) {
      expect(screen.getByTestId(`input-${key}`)).toHaveValue(String(value));
      expect(screen.getByTestId(`source-${key}`)).toHaveTextContent("DB 覆盖");
    }
    expect(screen.getByTestId("save-settings")).toBeDisabled();
    await openTask("account", "quota_probe");
    expect(screen.getByTestId("input-quota_probe_minutes")).toHaveValue("15");
  });

  it("恢复一项 Miora 默认只提交 null，其余 Miora 和后台任务草稿保留", async () => {
    const { writes } = setup();
    await screen.findByTestId("input-quota_probe_minutes");
    await replace("quota_probe_minutes", "15");
    await openMiora();
    await replace("miora_chat_min_interval", "17");
    await userEvent.click(screen.getByTestId("reset-miora_request_window_seconds"));
    await waitFor(() => expect(screen.getByTestId("source-miora_request_window_seconds")).toHaveTextContent("来自 .env"));
    expect(writes).toEqual([{ miora_request_window_seconds: null }]);
    expect(screen.getByTestId("input-miora_request_window_seconds")).toHaveValue("60");
    expect(screen.getByTestId("input-miora_chat_min_interval")).toHaveValue("17");
    expect(screen.getByTestId("reset-miora_request_window_seconds")).toBeDisabled();
    expect(screen.getByTestId("dirty-count")).toHaveTextContent("2 项待保存");
    await openTask("account", "quota_probe");
    expect(screen.getByTestId("input-quota_probe_minutes")).toHaveValue("15");
  });

  it("保存失败保留建议草稿和错误原因", async () => {
    const { writes } = setup({ rejectSave: true });
    await openMiora();
    await userEvent.click(screen.getByTestId("suggest-miora-limits"));
    await userEvent.click(screen.getByTestId("save-settings"));
    expect(await screen.findByTestId("settings-error")).toHaveTextContent("限速设置无效");
    expect(writes).toEqual([SUGGESTED]);
    expect(screen.getByTestId("dirty-count")).toHaveTextContent("4 项待保存");
    expect(screen.getByTestId("input-miora_chat_min_interval")).toHaveValue("5");
  });

  it("更新系统是第三个一级 tab，真实面板按需加载，切换保留草稿与二级选择", async () => {
    const { writes, fetchSpy } = setup();
    await screen.findByTestId("input-quota_probe_minutes");
    const top = screen.getByTestId("settings-sections");
    expect(within(top).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["后台任务", "网关配置", "更新系统"]);
    expect(screen.queryByTestId("system-update-panel")).not.toBeInTheDocument();
    expect(fetchSpy.mock.calls.some(([url]) => String(url) === "/api/system-updates")).toBe(false);
    await openTask("account", "activity");
    await userEvent.selectOptions(screen.getByTestId("input-activity_report_enabled"), "true");
    await openMiora();
    await userEvent.click(screen.getByTestId("suggest-miora-limits"));
    await userEvent.click(screen.getByTestId("settings-sections-updates"));
    expect(screen.getByTestId("settings-sections-updates")).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("system-update-panel")).toBeInTheDocument();
    expect(await screen.findByText("此部署尚未启用主机更新助手。")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-gateway-groups")).not.toBeInTheDocument();
    expect(screen.queryByTestId("settings-task-groups")).not.toBeInTheDocument();
    expect(screen.getByTestId("dirty-count")).toHaveTextContent("5 项待保存");
    await userEvent.click(screen.getByTestId("settings-sections-gateway"));
    expect(screen.queryByTestId("system-update-panel")).not.toBeInTheDocument();
    expect(screen.getByTestId("settings-gateway-groups-gateway-miora_throttle")).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("input-miora_chat_min_interval")).toHaveValue("5");
    await userEvent.click(screen.getByTestId("settings-sections-tasks"));
    expect(screen.getByTestId("input-activity_report_enabled")).toHaveValue("true");
    expect(writes).toEqual([]);
  });

  it("配置列表加载失败时仍可打开更新系统", async () => {
    setup({ failSettings: true });
    expect(await screen.findByTestId("settings-error-state")).toHaveTextContent("设置服务暂不可用");
    await userEvent.click(screen.getByTestId("settings-sections-updates"));
    expect(await screen.findByTestId("system-update-panel")).toBeInTheDocument();
    expect(await screen.findByText("此部署尚未启用主机更新助手。")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-error-state")).not.toBeInTheDocument();
  });

  it("没有已装配任务时保留网关与更新入口，切回仍保留当前组", async () => {
    setup({ noTasks: true });
    await openMiora();
    expect(screen.queryByTestId("settings-sections-tasks")).not.toBeInTheDocument();
    await replace("miora_chat_min_interval", "8");
    await userEvent.click(screen.getByTestId("settings-sections-updates"));
    await screen.findByTestId("system-update-panel");
    await userEvent.click(screen.getByTestId("settings-sections-gateway"));
    expect(screen.getByTestId("input-miora_chat_min_interval")).toHaveValue("8");
  });
});
