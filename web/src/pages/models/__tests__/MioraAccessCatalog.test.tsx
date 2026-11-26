import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { groupModelsByProvider } from "@/components/ModelPicker";
import type { ModelInfo } from "@/api/types";
import { MioraAccessCatalog } from "../MioraAccessCatalog";

const model = (id: string, fields: Partial<ModelInfo>): ModelInfo => ({
  id, name: id, object: "model", providers: ["miora"], owned_by: "2apis",
  gateway_endpoints: ["/v1/videos"], output_modalities: ["video"], ...fields,
});

describe("Miora 目录页权限", () => {
  it("普通模型整体在VIP前，锁定模型只读说明，不提供调用或探活按钮", () => {
    const models = groupModelsByProvider([
      model("vip", { required_account_tier: "standard", account_available: false,
        account_unavailable_reason: "tier_locked" }),
      model("ordinary", { account_available: true }),
      model("off", { account_available: false, account_unavailable_reason: "feature_flag_off" }),
    ]).get("miora")!;
    render(<MioraAccessCatalog models={models} />);
    const panel = screen.getByTestId("miora-access-catalog");
    expect(panel).toHaveTextContent("1 个已确认可用 / 3 个目录条目");
    expect(within(panel).getAllByRole("heading").map(heading => heading.textContent)).toEqual([
      "Miora 账号权限目录", "普通 / 常用模型 · 1", "VIP 模型 · 1", "暂不可用 · 1"]);
    expect(within(screen.getByTestId("miora-access-vip")).getByText("VIP")).toBeInTheDocument();
    expect(screen.getByTestId("miora-access-vip")).toHaveTextContent("Standard 套餐；当前账号池不可用");
    expect(screen.getByTestId("miora-access-off")).toHaveTextContent("与 VIP 套餐权限无关");
    expect(within(panel).queryByRole("button")).not.toBeInTheDocument();
  });

  it("缺权限元数据不声称已确认可用，空目录不显示面板", () => {
    const view = render(<MioraAccessCatalog models={groupModelsByProvider([model("old", {})]).get("miora")!} />);
    expect(screen.getByTestId("miora-access-catalog")).toHaveTextContent("0 个已确认可用 / 1 个目录条目");
    expect(screen.getByTestId("miora-access-old")).toHaveTextContent("权限未声明");
    view.rerender(<MioraAccessCatalog models={[]} />);
    expect(screen.queryByTestId("miora-access-catalog")).not.toBeInTheDocument();
  });
});
