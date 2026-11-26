import { QueryClient } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SettingsPage } from "../SettingsPage";
import { renderPage, settle, userEvent } from "@/test-utils";
import { mockAll, openGateway } from "@/test-utils/settings";

describe("设置变更刷新普通模型和权限展示目录", () => {
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("保存后刷新两个目录缓存", async () => {
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    const field = screen.getByTestId("input-quota_probe_minutes");
    await userEvent.clear(field);
    await userEvent.type(field, "30");
    await userEvent.click(screen.getByTestId("save-settings"));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["model-display"] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["playground-models"] });
  });

  it("恢复默认后也刷新两个目录，避免旧黑名单与会员状态残留", async () => {
    const invalidate = vi.spyOn(QueryClient.prototype, "invalidateQueries");
    mockAll();
    renderPage(<SettingsPage />);
    await settle();
    await openGateway("gateway-pacer");
    await userEvent.click(screen.getByTestId("reset-pacer_max_seconds"));
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ["model-display"] }));
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["playground-models"] });
  });
});
