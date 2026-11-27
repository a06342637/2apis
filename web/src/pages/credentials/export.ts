import { ApiError } from "../../api/client";
import { copyText } from "../../lib/clipboard";

export type CredentialExportMode = "download" | "clipboard";

/** 文件名固定使用北京时间，避免部署端或浏览器时区改变备份日期。 */
export function credentialExportFilename(now = new Date()): string {
  const beijing = new Date(now.getTime() + 8 * 60 * 60 * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  const date = `${beijing.getUTCFullYear()}${pad(beijing.getUTCMonth() + 1)}${pad(beijing.getUTCDate())}`;
  const time = `${pad(beijing.getUTCHours())}${pad(beijing.getUTCMinutes())}${pad(beijing.getUTCSeconds())}`;
  return `2apis-credentials-${date}-${time}.json`;
}

/** 一次下载一个文件；点击失败也释放包含明文凭证的对象 URL。 */
export function downloadCredentialExport(blob: Blob, filename = credentialExportFilename()): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  try {
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}

/** 下载和复制使用同一份导出 JSON，复制失败不报告成功。 */
export async function deliverCredentialExport(
  blob: Blob, mode: CredentialExportMode, filename?: string,
  isCurrent: () => boolean = () => true,
): Promise<boolean> {
  if (!isCurrent()) return false;
  if (mode === "download") {
    downloadCredentialExport(blob, filename);
    return true;
  }
  const text = await blob.text();
  if (!isCurrent()) return false;
  const copied = await copyText(text, isCurrent);
  if (!isCurrent()) return false;
  if (!copied) {
    throw new ApiError(0, "clipboard_failed", "复制到剪贴板失败，请重试或选择下载 JSON");
  }
  return true;
}
