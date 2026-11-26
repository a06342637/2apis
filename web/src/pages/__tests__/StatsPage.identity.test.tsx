import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fullSession, jsonResponse, makeClient, MockEventSource } from "@/test-utils";
import { EVENTS_PAGE1, OVERVIEW } from "@/test-utils/stats";
import { Layout } from "@/Layout";
import { StatsPage } from "../StatsPage";

describe("统计明细身份缓存隔离", () => {
  afterEach(() => vi.unstubAllGlobals());
  it("用户名变化不能复用旧用户明细，旧实时连接和缓冲清除", async () => {
    MockEventSource.reset();
    let user = "alice";
    let finish!: (value: Response) => void;
    const pending = new Promise<Response>((done) => { finish = done; });
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.startsWith("/api/stats/overview")) return jsonResponse(OVERVIEW);
      if (url.startsWith("/api/stats/by-model")) return jsonResponse({ models: [] });
      if (url.startsWith("/api/stats/by-provider")) return jsonResponse({ providers: [] });
      if (url.startsWith("/api/stats/by-user")) return jsonResponse({ users: [] });
      if (url.startsWith("/api/stats/by-credential")) return jsonResponse({ credentials: [] });
      if (url.startsWith("/api/stats/model-timeline")) return jsonResponse({ models: [], points: [] });
      if (url.startsWith("/api/stats/events?")) {
        return user === "alice" ? jsonResponse({ ...EVENTS_PAGE1,
          events: [{ ...EVENTS_PAGE1.events[0], model: "alice-private-event" }] }) : pending;
      }
      return jsonResponse({});
    }));
    const client = makeClient();
    const tree = (username: string) => <QueryClientProvider client={client}><MemoryRouter>
      <Routes><Route element={<Layout session={fullSession({ username, is_admin: false })} />}>
        <Route path="*" element={<StatsPage />} />
      </Route></Routes>
    </MemoryRouter></QueryClientProvider>;
    const view = render(tree("alice"));
    await screen.findByText("alice-private-event");
    await act(async () => screen.getByTestId("events-live-toggle").click());
    const source = MockEventSource.last();
    await act(async () => source.emit({ ...EVENTS_PAGE1.events[0], model: "alice-private-live" }));
    user = "bob";
    view.rerender(tree("bob"));
    await waitFor(() => expect(screen.getByTestId("events-panel")).toBeInTheDocument());
    expect(screen.queryByText("alice-private-event")).not.toBeInTheDocument();
    expect(screen.queryByText("alice-private-live")).not.toBeInTheDocument();
    expect(source.closed).toBe(true);
    await act(async () => finish(jsonResponse({ ...EVENTS_PAGE1,
      events: [{ ...EVENTS_PAGE1.events[0], model: "bob-event" }] })));
    expect(await screen.findByText("bob-event")).toBeInTheDocument();
  });
});
