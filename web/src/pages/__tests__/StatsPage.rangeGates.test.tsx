import { fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StatsPage } from "../StatsPage";
import { mockFetch, renderPage, settle, userEvent } from "@/test-utils";
import { ADMIN, EVENTS_EMPTY, OVERVIEW } from "@/test-utils/stats";

function setup() {
  const spy = mockFetch({
    "/api/stats/overview": OVERVIEW,
    "/api/stats/by-provider": { providers: [] },
    "/api/stats/by-model": { models: [] },
    "/api/stats/by-user": { users: [] },
    "/api/stats/by-credential": { credentials: [] },
    "/api/stats/model-timeline": { models: [], points: [] },
    "/api/stats/timeline": { points: [] },
    "/api/stats/percentile-timeline": { points: [] },
    "/api/stats/errors": { errors: [] },
    "/api/stats/events": EVENTS_EMPTY,
  });
  renderPage(<StatsPage />, ADMIN);
  return spy;
}

async function openCustom() {
  await settle();
  await userEvent.click(screen.getByTestId("range-tabs-custom"));
  await settle();
}

describe("StatsPage 自定义时间窗取数护栏", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("同时清空自定义两端后暂停取数，不能静默请求全量", async () => {
    const spy = setup();
    await openCustom();
    fireEvent.change(screen.getByTestId("custom-from"), { target: { value: "" } });
    await settle();
    const before = spy.mock.calls.filter(([url]) => String(url).includes("/api/stats/")).length;
    fireEvent.change(screen.getByTestId("custom-to"), { target: { value: "" } });
    await settle();
    await settle();
    expect(spy.mock.calls.filter(([url]) => String(url).includes("/api/stats/")).length).toBe(before);
    expect(screen.getByTestId("custom-range-invalid")).toBeInTheDocument();
  });

  it("非法时间窗也暂停延迟分位和失败归因请求", async () => {
    const spy = setup();
    await openCustom();
    await userEvent.click(screen.getByTestId("metric-tabs-latency"));
    await settle();
    const auxiliaryCalls = () => spy.mock.calls.filter(([url]) =>
      /\/api\/stats\/(?:percentile-timeline|errors)/.test(String(url)));
    const before = auxiliaryCalls().length;
    fireEvent.change(screen.getByTestId("custom-from"), { target: { value: "2028-01-01T00:00" } });
    await settle();
    await settle();
    expect(auxiliaryCalls()).toHaveLength(before);
    fireEvent.change(screen.getByTestId("custom-from"), { target: { value: "2020-01-01T00:00" } });
    await settle();
    await settle();
    expect(auxiliaryCalls().length).toBeGreaterThan(before);
  });

  it("非法时间窗禁止CSV导出，不能改成不限时间的导出", async () => {
    const spy = setup();
    await openCustom();
    fireEvent.change(screen.getByTestId("custom-from"), { target: { value: "2028-01-01T00:00" } });
    await settle();
    expect(screen.getByTestId("events-export")).toBeDisabled();
    await userEvent.click(screen.getByTestId("events-export"));
    expect(spy.mock.calls.some(([url]) => String(url).includes("format=csv"))).toBe(false);
  });

  it("只清空一端仍可按有效的单边范围查询和导出", async () => {
    const spy = setup();
    await openCustom();
    fireEvent.change(screen.getByTestId("custom-from"), { target: { value: "" } });
    await settle();
    const latest = spy.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/api/stats/overview")).at(-1)!;
    const params = new URLSearchParams(latest.split("?")[1]);
    expect(params.has("since")).toBe(false);
    expect(params.has("until")).toBe(true);
    expect(screen.queryByTestId("custom-range-invalid")).not.toBeInTheDocument();
    expect(screen.getByTestId("events-export")).toBeEnabled();
  });
});
