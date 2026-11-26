import { screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { jsonResponse, mockFetch, renderPage, userEvent } from "@/test-utils";
import { ApiKeysPage } from "../ApiKeysPage";

const KEY = { id: "key-layout", username: "root", name: "工作客户端", preview: "sk-…test",
  created_at: 1, last_used_at: null, provider_binding: "miora", allowed_ips: "",
  allowed_models: "", expires_at: null };

function setup() {
  const requests: URLSearchParams[] = [];
  mockFetch({
    "/api/api-keys": (url: string) => {
      requests.push(new URL(url, "https://test.local").searchParams);
      return jsonResponse({ api_keys: [KEY], total: 101 });
    },
    "/api/credentials": { credentials: [] },
    "/api/credential-groups": { groups: [], is_operator: true },
  });
  renderPage(<ApiKeysPage />);
  return requests;
}

describe("API Key 列表与排序布局", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("五个分组列保持表格语义，排序选择器包含全部原有字段", async () => {
    setup();
    const table = await screen.findByTestId("keys-table");
    expect(table).toHaveAttribute("role", "table");
    expect(table).toHaveAttribute("data-table-cards");
    expect(within(table).getAllByRole("columnheader").map((head) => head.textContent)).toEqual(
      ["名称 / Key", "渠道与范围", "权限与配额", "用量 · 今日", "操作"]);
    expect(within(table).getAllByRole("cell")).toHaveLength(5);
    expect(screen.getByLabelText("排序")).toBe(screen.getByTestId("keys-sort"));
    expect(within(screen.getByTestId("keys-sort")).getAllByRole("option").map(
      (option) => (option as HTMLOptionElement).value)).toEqual(
      ["created_at", "name", "provider_binding", "expires_at", "last_used_at"]);
  });

  it("排序方向和选择字段继续请求后端，最近使用默认降序", async () => {
    const requests = setup();
    await screen.findByTestId("keys-table");
    await userEvent.click(screen.getByRole("button", { name: "当前升序，切换为降序" }));
    await waitFor(() => expect(requests.at(-1)?.get("order")).toBe("desc"));
    expect(screen.getByRole("button", { name: "当前降序，切换为升序" })).toBeInTheDocument();
    for (const field of ["name", "provider_binding", "expires_at", "last_used_at", "created_at"]) {
      await userEvent.selectOptions(screen.getByTestId("keys-sort"), field);
      await waitFor(() => expect(requests.at(-1)?.get("sort")).toBe(field));
      expect(requests.at(-1)?.get("order")).toBe(field === "last_used_at" ? "desc" : "asc");
    }
    await userEvent.click(screen.getByTestId("sort-name"));
    await waitFor(() => expect(requests.at(-1)?.get("sort")).toBe("name"));
    expect(screen.getByTestId("keys-sort")).toHaveValue("name");
    expect(screen.getByRole("columnheader", { name: "名称 / Key" })).toHaveAttribute("aria-sort", "ascending");
  });

  it("第二页选择排序字段会回到第一页，方向切换也重置分页", async () => {
    const requests = setup();
    await screen.findByTestId("keys-table");
    await waitFor(() => expect(screen.getByTestId("keys-next")).toBeEnabled());
    await userEvent.click(screen.getByTestId("keys-next"));
    await waitFor(() => expect(Number(requests.at(-1)?.get("offset"))).toBeGreaterThan(0));
    await userEvent.selectOptions(screen.getByTestId("keys-sort"), "last_used_at");
    await waitFor(() => expect(requests.at(-1)?.get("sort")).toBe("last_used_at"));
    expect(requests.at(-1)?.get("offset")).toBe("0");
    expect(screen.getByTestId("keys-page-info")).toHaveTextContent("第 1 /");
    await waitFor(() => expect(screen.getByTestId("keys-next")).toBeEnabled());
    await userEvent.click(screen.getByTestId("keys-next"));
    await waitFor(() => expect(Number(requests.at(-1)?.get("offset"))).toBeGreaterThan(0));
    await userEvent.click(screen.getByTestId("keys-sort-direction"));
    await waitFor(() => expect(requests.at(-1)?.get("order")).toBe("asc"));
    expect(requests.at(-1)?.get("offset")).toBe("0");
    expect(screen.getByTestId("keys-page-info")).toHaveTextContent("第 1 /");
  });
});
