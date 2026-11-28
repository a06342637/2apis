import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, renderPage, userEvent } from "@/test-utils";
import { RemoteBackupPanel } from "../RemoteBackupPanel";
import { RemoteBackupFiles } from "../RemoteBackupFiles";
import type { RemoteStatus } from "../../api/remoteBackups";

const initial: RemoteStatus = {
  config: { enabled: false, days: 1, time: "04:30", timezone: "Asia/Shanghai",
    oss: { enabled: true, keep: 10, directory: "backups", region: "cn-hongkong", bucket: "example",
      key_id: "fixture", has_key_secret: true, endpoint: "", internal: false },
    sftp: { enabled: false, keep: 10, directory: "", host: "", username: "", auth: "password",
      fingerprint: "", port: 22 } },
  running: false, last_run: null, next_run: null,
};
const root = "/api/remote-backups";

describe("远端备份", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("保存计划后可测试连接和排队备份，不提交秘密状态占位字段", async () => {
    const status = structuredClone(initial);
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === root && init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        expect(body.oss).not.toHaveProperty("has_key_secret");
        expect(body.oss).not.toHaveProperty("key_secret");
        status.config.enabled = body.enabled;
      }
      if (url === `${root}/run`) status.running = true;
      return jsonResponse(url === root ? status : { ok: true });
    });
    vi.stubGlobal("fetch", fetch);
    renderPage(<RemoteBackupPanel />);
    await screen.findByTestId("remote-backup-panel");
    await userEvent.click(screen.getByLabelText("启用远端自动备份"));
    expect(screen.getByRole("button", { name: "立即远端备份" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "保存远端备份设置" }));
    await screen.findByText("远端备份设置已保存。");
    await userEvent.click(screen.getByRole("button", { name: /测试阿里云 OSS连接/ }));
    await screen.findByText("阿里云 OSS 连接成功。");
    await userEvent.click(screen.getByRole("button", { name: "立即远端备份" }));
    await screen.findByRole("button", { name: "正在备份…" });
    expect(screen.getByRole("link", { name: "查看备份日志" })).toHaveAttribute("href", "/logs?category=backup");
  });

  it("从目标列表下载并确认暂存恢复，不覆盖运行中的库", async () => {
    const file = "coding2api-20261128T000000Z-0123456789ab.sqlite3";
    const onRestored = vi.fn(async () => undefined);
    const fetch = vi.fn(async (url: string) => jsonResponse(url === root ? initial : url.endsWith("/files")
      ? { backups: [{ name: file, size: 32, created_at: 1 }] } : { staged: true, restart_required: true }));
    vi.stubGlobal("fetch", fetch);
    renderPage(<RemoteBackupFiles initial="oss" onRestored={onRestored} formatBytes={size => `${size} B`} />);
    await screen.findByText(file);
    expect(screen.getByRole("link", { name: "下载" })).toHaveAttribute("href", `${root}/oss/files/${file}`);
    await userEvent.click(screen.getByRole("button", { name: "恢复远端备份" }));
    const dialog = screen.getByRole("dialog", { name: "确认远端备份操作" });
    expect(dialog).toHaveTextContent("重启时恢复");
    await userEvent.click(within(dialog).getByRole("button", { name: "确认" }));
    await waitFor(() => expect(onRestored).toHaveBeenCalledOnce());
    expect(fetch).toHaveBeenCalledWith(`${root}/oss/files/${file}/restore`, expect.objectContaining({ method: "POST" }));
  });
});
