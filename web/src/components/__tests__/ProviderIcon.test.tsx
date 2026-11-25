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

  it("Miora 使用装饰性文字标记", () => {
    const { container } = render(<ProviderIcon provider="miora" size={16} />);
    expect(container.querySelector("span")).toHaveTextContent("M");
    expect(container.querySelector("span")).toHaveAttribute("aria-hidden", "true");
    expect(container.querySelector("img")).toBeNull();
  });
});
