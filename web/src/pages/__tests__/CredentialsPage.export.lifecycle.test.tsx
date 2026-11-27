import { QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Layout } from "@/Layout";
import { api } from "@/api/client";
import { copyText } from "@/lib/clipboard";
import { fullSession, makeClient, makeCredential, mockCredentialsFetch, settle, userEvent } from "@/test-utils";
import { CredentialsPage } from "../CredentialsPage";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn() }));
const DATA = JSON.stringify([{ provider: "miora", nickname: "私有备注", tags: ["原标签"],
  credential: { authToken: "synthetic-original", refreshToken: "synthetic-refresh" } }]);
const createURL = vi.fn(() => "blob:export-lifetime");
const cases = (["batch", "row"] as const).flatMap((entry) =>
  (["clipboard", "download"] as const).flatMap((mode) =>
    (["unmount", "identity"] as const).map((leave) => ({ entry, mode, leave }))));
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function mount() {
  mockCredentialsFetch({ "/api/credentials": { is_operator: true,
    credentials: [makeCredential({ id: "mi-a", provider: "miora" })] } });
  const client = makeClient();
  const tree = (username: string) => <QueryClientProvider client={client}><MemoryRouter>
    <Routes><Route element={<Layout session={fullSession({ username, is_admin: true })} />}>
      <Route path="*" element={<CredentialsPage />} />
    </Route></Routes>
  </MemoryRouter></QueryClientProvider>;
  return { view: render(tree("alice")), tree };
}
async function choose(entry: "batch" | "row", mode: "clipboard" | "download") {
  if (entry === "batch") {
    if (!screen.queryByTestId("batch-export")) await userEvent.click(screen.getByTestId("group-select-miora"));
    await userEvent.click(screen.getByTestId("batch-export"));
  } else {
    if (!screen.queryByTestId("actions-mi-a")) await userEvent.click(screen.getByTestId("group-toggle-miora"));
    await userEvent.click(screen.getByTestId("actions-mi-a"));
    await userEvent.click(screen.getByRole("menuitem", { name: "导出凭证" }));
  }
  await userEvent.click(await screen.findByRole("menuitem", {
    name: mode === "clipboard" ? "复制到剪贴板" : "下载 JSON" }));
}
describe("凭证导出生命周期", () => {
  beforeEach(() => {
    vi.mocked(copyText).mockReset().mockResolvedValue(true);
    createURL.mockClear();
    const OriginalURL = globalThis.URL;
    class ExportURL extends OriginalURL {
      static createObjectURL = createURL;
      static revokeObjectURL = vi.fn();
    }
    vi.stubGlobal("URL", ExportURL);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
  });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it.each(cases)("$entry $mode API after $leave stops delivery", async ({ entry, mode, leave }) => {
    const pending = deferred<{ blob: Blob }>();
    const request = vi.spyOn(api, entry === "batch" ? "exportCredentials" : "exportCredential")
      .mockReturnValue(pending.promise);
    const { view, tree } = mount();
    await settle();
    await choose(entry, mode);
    expect(request).toHaveBeenCalledOnce();
    if (leave === "unmount") view.unmount();
    else { view.rerender(tree("bob")); await settle(); }
    await act(async () => pending.resolve({ blob: new Blob([DATA]) }));
    await settle();
    expect(copyText).not.toHaveBeenCalled();
    expect(createURL).not.toHaveBeenCalled();
    expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
    expect(screen.queryByTestId("credentials-error")).not.toBeInTheDocument();
  });

  it.each(cases.filter((item) => item.mode === "clipboard"))(
    "$entry Blob text after $leave stops clipboard delivery", async ({ entry, leave }) => {
      const pending = deferred<string>();
      const blob = new Blob([DATA]);
      const read = vi.spyOn(blob, "text").mockReturnValue(pending.promise);
      vi.spyOn(api, entry === "batch" ? "exportCredentials" : "exportCredential").mockResolvedValue({ blob });
      const { view, tree } = mount();
      await settle();
      await choose(entry, "clipboard");
      await waitFor(() => expect(read).toHaveBeenCalledOnce());
      if (leave === "unmount") view.unmount();
      else { view.rerender(tree("bob")); await settle(); }
      await act(async () => pending.resolve(DATA));
      await settle();
      expect(copyText).not.toHaveBeenCalled();
      expect(createURL).not.toHaveBeenCalled();
      expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
    });

  it.each(["batch", "row"] as const)("%s old finally cannot unlock a new user's pending export", async (entry) => {
    const previous = deferred<{ blob: Blob }>();
    const current = deferred<{ blob: Blob }>();
    vi.spyOn(api, entry === "batch" ? "exportCredentials" : "exportCredential")
      .mockReturnValueOnce(previous.promise).mockReturnValueOnce(current.promise);
    const { view, tree } = mount();
    await settle();
    await choose(entry, "clipboard");
    view.rerender(tree("bob"));
    await settle();
    await choose(entry, "clipboard");
    await act(async () => previous.resolve({ blob: new Blob([DATA]) }));
    await settle();
    expect(screen.getByTestId(entry === "batch" ? "batch-export" : "actions-mi-a")).toBeDisabled();
    expect(copyText).not.toHaveBeenCalled();
    await act(async () => current.resolve({ blob: new Blob([DATA.replace("original", "new-user")]) }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("已复制到剪贴板");
    expect(copyText).toHaveBeenCalledOnce();
    expect(vi.mocked(copyText).mock.calls[0][0]).toContain("synthetic-new-user");
  });

  it.each(["batch", "row"] as const)("%s returning to the same username cannot revive an old export", async (entry) => {
    const pending = deferred<{ blob: Blob }>();
    vi.spyOn(api, entry === "batch" ? "exportCredentials" : "exportCredential")
      .mockReturnValue(pending.promise);
    const { view, tree } = mount();
    await settle();
    await choose(entry, "clipboard");
    view.rerender(tree("bob"));
    await settle();
    view.rerender(tree("alice"));
    await settle();
    await act(async () => pending.resolve({ blob: new Blob([DATA]) }));
    await settle();
    expect(copyText).not.toHaveBeenCalled();
    expect(screen.queryByTestId("credentials-notice")).not.toBeInTheDocument();
  });

  it("single-row pending export locks both entry points against duplicate requests", async () => {
    const pending = deferred<{ blob: Blob }>();
    const single = vi.spyOn(api, "exportCredential").mockReturnValue(pending.promise);
    const batch = vi.spyOn(api, "exportCredentials").mockResolvedValue({ blob: new Blob([DATA]) });
    mount();
    await settle();
    await userEvent.click(screen.getByTestId("group-select-miora"));
    await choose("row", "clipboard");
    expect(screen.getByTestId("actions-mi-a")).toBeDisabled();
    expect(screen.getByTestId("batch-export")).toBeDisabled();
    fireEvent.click(screen.getByTestId("actions-mi-a"));
    fireEvent.click(screen.getByTestId("batch-export"));
    expect(single).toHaveBeenCalledOnce();
    expect(batch).not.toHaveBeenCalled();
    await act(async () => pending.resolve({ blob: new Blob([DATA]) }));
    expect(await screen.findByTestId("credentials-notice")).toHaveTextContent("已复制到剪贴板");
    expect(copyText).toHaveBeenCalledOnce();
  });
});
