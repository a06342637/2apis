import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { ProviderIcon } from "../ProviderIcon";

describe("ProviderIcon", () => {
  it("灵犀用本地打包的官方 logo（CSP 只放行 self）", () => {
    const { container } = render(<ProviderIcon provider="lingxi" size={16} />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img?.getAttribute("src")).toContain("lingxi");
    expect(img?.getAttribute("width")).toBe("16");
    expect(img?.getAttribute("height")).toBe("16");
    // 图标是装饰性的：渠道名由相邻文本给出，图片不重复播报
    expect(img?.getAttribute("alt")).toBe("");
    expect(img?.getAttribute("aria-hidden")).toBe("true");
  });

  it("lobehub 渠道仍渲染 SVG", () => {
    const { container } = render(<ProviderIcon provider="trae" />);
    expect(container.querySelector("svg")).not.toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("未知渠道不渲染", () => {
    const { container } = render(<ProviderIcon provider="unknown" />);
    expect(container).toBeEmptyDOMElement();
  });

  it.each([true, false])("Miora 两种显示模式都使用本地官方高清头像，colored=%s", colored => {
    const { container } = render(<ProviderIcon provider="miora" size={20} colored={colored} className="official-logo" />);
    const img = container.querySelector("img");
    expect(img).not.toBeNull();
    expect(img).toHaveAttribute("src", expect.stringContaining("miora"));
    expect(img).toHaveAttribute("width", "20");
    expect(img).toHaveAttribute("height", "20");
    expect(img).toHaveAttribute("alt", "");
    expect(img).toHaveAttribute("aria-hidden", "true");
    expect(img).toHaveClass("official-logo");
    expect(container.querySelector("span")).toBeNull();
  });
});
