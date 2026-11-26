import { afterEach, describe, expect, it, vi } from "vitest";
import { credentialExportFilename, downloadCredentialExport } from "../export";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("凭证导出文件", () => {
  it("文件名使用北京时间并正确跨UTC日期和年份", () => {
    expect(credentialExportFilename(new Date("2026-12-31T16:02:03Z")))
      .toBe("2apis-credentials-20270101-000203.json");
    expect(credentialExportFilename(new Date("2026-11-26T01:09:04Z")))
      .toBe("2apis-credentials-20261126-090904.json");
  });

  it("下载点击失败也清理DOM和对象URL", () => {
    const revoke = vi.fn();
    const OriginalURL = globalThis.URL;
    class DownloadURL extends OriginalURL {
      static createObjectURL = vi.fn(() => "blob:failed-download");
      static revokeObjectURL = revoke;
    }
    vi.stubGlobal("URL", DownloadURL);
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => { throw new Error("download blocked"); });
    expect(() => downloadCredentialExport(new Blob(["[]"]))).toThrow("download blocked");
    expect(revoke).toHaveBeenCalledExactlyOnceWith("blob:failed-download");
    expect(document.querySelector('a[download^="2apis-credentials-"]')).toBeNull();
  });
});
