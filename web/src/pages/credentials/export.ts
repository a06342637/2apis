/** 文件名固定使用北京时间，避免部署端或浏览器时区改变备份日期。 */
export function credentialExportFilename(now = new Date()): string {
  const beijing = new Date(now.getTime() + 8 * 60 * 60 * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  const date = `${beijing.getUTCFullYear()}${pad(beijing.getUTCMonth() + 1)}${pad(beijing.getUTCDate())}`;
  const time = `${pad(beijing.getUTCHours())}${pad(beijing.getUTCMinutes())}${pad(beijing.getUTCSeconds())}`;
  return `2apis-credentials-${date}-${time}.json`;
}

/** 一次下载一个文件；点击失败也释放包含明文凭证的对象 URL。 */
export function downloadCredentialExport(blob: Blob): void {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  try {
    anchor.href = url;
    anchor.download = credentialExportFilename();
    document.body.appendChild(anchor);
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}
