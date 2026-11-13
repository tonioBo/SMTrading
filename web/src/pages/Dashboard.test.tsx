import { act, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Dashboard } from "@/api/types";
import type { LiveEvent } from "@/lib/ws";
import { renderApp } from "@/test/render";

const handlers = vi.hoisted(() => new Set<(e: unknown) => void>());
vi.mock("@/lib/ws", async () => {
  const { useEffect, useRef } = await import("react");
  return {
    useLiveSocket: () => ({ socket: null, status: "open" }),
    // like the real hook: one subscription per mount, always calling the latest handler
    useLiveMessage: (_type: string, handler: (e: unknown) => void) => {
      const ref = useRef(handler);
      ref.current = handler;
      useEffect(() => {
        const call = (e: unknown) => ref.current(e);
        handlers.add(call);
        return () => void handlers.delete(call);
      }, []);
    },
  };
});
vi.mock("lightweight-charts", () => {
  const series = { setData: vi.fn() };
  const chart = { addAreaSeries: () => series, timeScale: () => ({ fitContent: vi.fn() }), remove: vi.fn() };
  return { createChart: vi.fn(() => chart), ColorType: { Solid: "solid" } };
});

function emit(event: LiveEvent) {
  act(() => handlers.forEach((h) => h(event)));
}

const dashboard: Dashboard = {
  total_equity_eur: "12345.60",
  day_pnl_eur: "-250.5",
  day_pnl_pct: "-1.99",
  portfolios: [
    { id: "p1", name: "Alfa", status: "active", equity_eur: "10000", day_pnl_eur: "-250.5", day_pnl_pct: "-2.4", stale_symbols: [], error: null },
    { id: "p2", name: "Beta", status: "active", equity_eur: "2345.6", day_pnl_eur: "10", day_pnl_pct: "0.4", stale_symbols: [], error: null },
  ],
  connectors: [{ name: "binance", enabled: true, health_status: "ok", last_checked_at: null }],
  margin_alerts: [
    { id: 1, portfolio_id: "p1", kind: "margin_warning", payload: {}, created_at: "2026-11-12T10:00:00Z" },
  ],
  kill_switch: false,
  worker_heartbeat_age_s: 12,
};

let killSwitchOn = false;
let posted: unknown = null;
let equity: { ts: string; equity_eur: string }[] = [];
let current = dashboard;
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: killSwitchOn })),
  http.post("*/api/kill-switch", async ({ request }) => {
    posted = await request.json();
    killSwitchOn = (posted as { on: boolean }).on;
    return HttpResponse.json({ on: killSwitchOn, canceled: 3 });
  }),
  http.get("*/api/dashboard", () => HttpResponse.json(current)),
  http.get("*/api/dashboard/equity", () => HttpResponse.json({ period: "1m", bucket_s: 300, series: equity, portfolios: [] })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  killSwitchOn = false;
  posted = null;
  equity = [];
  current = dashboard;
  handlers.clear();
});
afterAll(() => server.close());

describe("Dashboard", () => {
  it("renders KPIs, a loss-styled negative P&L and portfolio links", async () => {
    renderApp("/");
    expect(await screen.findByText("12.345,60 €")).toBeInTheDocument();
    const kpi = screen.getByText("P&L del giorno", { selector: "div" }).closest("div.rounded-lg") as HTMLElement;
    expect(within(kpi).getByText("-250,50 €")).toHaveClass("loss");
    expect(screen.getByText("Worker attivo (12 s fa)")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Alfa" })).toHaveAttribute("href", "/portafogli/p1");
    expect(screen.getByText("+0,40%", { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "binance: operativo" })).toBeInTheDocument();
    expect(screen.getByText(/Avviso di margine · Alfa/)).toBeInTheDocument();
    expect(await screen.findByText("Nessun dato")).toBeInTheDocument();
  });

  it("shows amber and red worker heartbeat states", async () => {
    current = { ...dashboard, worker_heartbeat_age_s: 60 };
    const { unmount } = renderApp("/");
    expect(await screen.findByText("Worker in ritardo (60 s fa)")).toBeInTheDocument();
    unmount();
    current = { ...dashboard, worker_heartbeat_age_s: null };
    renderApp("/");
    expect(await screen.findByText("Worker non raggiungibile")).toBeInTheDocument();
  });

  it("draws the chart when the series has data", async () => {
    equity = [{ ts: "2026-11-12T10:00:00Z", equity_eur: "100.5" }];
    renderApp("/");
    expect(await screen.findByTestId("equity-chart")).toBeInTheDocument();
  });

  it("updates the kill-switch badge from the kill_switch WS event", async () => {
    renderApp("/");
    expect(await screen.findByText("Trading attivo")).toBeInTheDocument();
    emit({ type: "kill_switch", on: true });
    expect(await screen.findByText("Kill-switch attivo")).toBeInTheDocument();
  });

  it("lists order events in the live feed", async () => {
    renderApp("/");
    await screen.findByText("12.345,60 €");
    emit({ type: "order.filled", ts: "2026-11-12T10:00:00Z", symbol: "AAPL", side: "buy", qty: "2", price: "190.5", currency: "USD" });
    emit({ type: "order.canceled", symbol: "MSFT" });
    emit({ type: "quote_noise" });
    const feed = screen.getByRole("list", { name: "Feed ordini" });
    expect(within(feed).getAllByRole("listitem")).toHaveLength(2);
    expect(within(feed).getByText(/AAPL acquisto 2 @ 190,50 \$/)).toBeInTheDocument();
  });

  it("confirms the kill-switch and posts cancel_open", async () => {
    const user = userEvent.setup();
    renderApp("/");
    await user.click(await screen.findByRole("button", { name: "Attiva kill-switch" }));
    await user.click(screen.getByLabelText("Annulla anche gli ordini aperti"));
    await user.click(screen.getByRole("button", { name: "Attiva" }));
    expect(await screen.findByText("Kill-switch attivo")).toBeInTheDocument();
    expect(posted).toEqual({ on: true, cancel_open: true });
    expect(await screen.findByRole("button", { name: "Disattiva kill-switch" })).toBeInTheDocument();
  });
});
