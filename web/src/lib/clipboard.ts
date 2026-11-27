/**
 * 复制文本：优先 Clipboard API，非安全上下文（http + 非 localhost 的局域网部署）
 * 下 `navigator.clipboard` 不存在或写入被拒，回落到 execCommand 兜底。
 *
 * 返回是否成功，调用方据此给出「请手动复制」的提示——静默失败会让用户以为
 * 已经复制走，粘贴出来却是旧内容。
 */
export async function copyText(text: string, isCurrent: () => boolean = () => true): Promise<boolean> {
  if (!isCurrent()) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return isCurrent();
    }
  } catch {
    // 权限被拒 / 非安全上下文：继续尝试旧接口
  }
  // 请求在 Clipboard API 拒绝期间失效，不再发起迟到的降级复制。
  return isCurrent() && legacyCopy(text);
}

function legacyCopy(text: string): boolean {
  let area: HTMLTextAreaElement | undefined;
  try {
    area = document.createElement("textarea");
    area.value = text;
    // 移出视口但保留可选中状态，避免页面滚动抖动
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    if (area) {
      area.value = "";
      area.remove();
    }
  }
}
