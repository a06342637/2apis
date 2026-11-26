import { vi } from "vitest";

/** jsdom 无布局；将锚点放在视口中，避免 Radix 把交互弹层视作已离开可见范围。 */
export function stubPopupGeometry(triggerTestId: string) {
  vi.spyOn(document.documentElement, "clientWidth", "get").mockReturnValue(1024);
  vi.spyOn(document.documentElement, "clientHeight", "get").mockReturnValue(768);
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    return this.dataset.testid === triggerTestId
      ? new DOMRect(800, 100, 160, 40) : new DOMRect(600, 146, 384, 400);
  });
}
