import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Dashboard } from "@/api/types";
import type { LiveEvent } from "@/lib/ws";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import { haltFixture } from "@/test/halts";

const live = vi.hoisted(() => ({
  handlers: new Map<string, Set<(e: unknown) => void>>(),
  status: "open" as string,
  subscribed: [] as string[][],
}));
vi.mock("@/lib/ws", async () => {
  const { useEffect, useRef } = await import("react");
  const socket = {
    subscribeQuotes: (symbols: string[]) => {
      live.subscribed.push(symbols);
      return () => {};
    },
  };
  return {
    useLiveSocket: () => ({ socket, status: live.status }),
    // like the real hook: one subscription per mount and type, always the latest handler
    useLiveMessage: (type: string, handler: (e: unknown) => void) => {
      const ref = useRef(handler);
      ref.current = handler;
      useEffect(() => {
        const call = (e: unknown) => ref.current(e);
        if (!live.handlers.has(type)) live.handlers.set(type, new Set());
        live.handlers.get(type)!.add(call);
        return () => void live.handlers.get(type)?.delete(call);
      }, [type]);
    },
  };
});
vi.mock("lightweight-charts", () => {
  const series = { setData: vi.fn(), update: vi.fn() };
  const chart = { addAreaSeries: () => series, timeScale: () => ({ fitContent: vi.fn() }), remove: vi.fn() };
  return { createChart: vi.fn(() => chart), ColorType: { Solid: "solid" } };
});

function emit(event: LiveEvent) {
  act(() => live.handlers.get("event")?.forEach((h) => h(event)));
}

function tick(symbol: string) {
  act(() => live.handlers.get("quote")?.forEach((h) => h({ symbol, last: "1" })));
}

const dashboard: Dashboard = {
  total_equity_eur: "12345.60",
  day_pnl_eur: "-250.5",
  day_pnl_pct: "-1.99",
  portfolios: [
    { id: "p1", name: "Alfa", status: "active", mode: "simulazione", equity_eur: "10000", day_pnl_eur: "-250.5", day_pnl_pct: "-2.4", stale_symbols: [], error: null },
    { id: "p2", name: "Beta", status: "active", mode: "paper", equity_eur: "2345.6", day_pnl_eur: "10", day_pnl_pct: "0.4", stale_symbols: [], error: null },
  ],
  modes: { simulazione: 1, paper: 1, reale: 0 },
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
let fetches = 0;
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: killSwitchOn })),
  http.post("*/api/kill-switch", async ({ request }) => {
    posted = await request.json();
    killSwitchOn = (posted as { on: boolean }).on;
    return HttpResponse.json({ on: killSwitchOn, canceled: 3 });
  }),
  http.get("*/api/dashboard", () => {
    fetches += 1;
    return HttpResponse.json(current);
  }),
  http.get("*/api/dashboard/equity", () => HttpResponse.json({ period: "1m", bucket_s: 300, series: equity, portfolios: [] })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  killSwitchOn = false;
  posted = null;
  equity = [];
  current = dashboard;
  fetches = 0;
  live.handlers.clear();
  live.status = "open";
  live.subscribed = [];
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

  it("shows a mode badge per portfolio, the count per mode and the banner", async () => {
    server.use(http.get("*/api/dashboard/modes", () => HttpResponse.json(dashboard.modes)));
    renderApp("/");
    const counts = await screen.findByRole("list", { name: "Portafogli per modalità" });
    expect(within(counts).getByText(/simulazione:/)).toHaveTextContent("simulazione: 1");
    expect(within(counts).getByText(/paper:/)).toHaveTextContent("paper: 1");
    expect(within(counts).queryByText(/reale/)).toBeNull();
    expect(screen.getByLabelText(/Modalità: simulazione\./)).toBeInTheDocument();
    expect(screen.getByLabelText(/Modalità: paper\./)).toBeInTheDocument();
    expect(await screen.findByRole("status", { name: "Modalità di trading" })).toHaveTextContent(
      "Simulazione e paper trading (conti demo) – nessun denaro reale",
    );
  });

  it("says simulation only when no portfolio has a broker, and flags real money in red", async () => {
    current = { ...dashboard, modes: { simulazione: 2, paper: 0, reale: 0 } };
    server.use(http.get("*/api/dashboard/modes", () => HttpResponse.json(current.modes)));
    const { unmount } = renderApp("/");
    expect(await screen.findByRole("status", { name: "Modalità di trading" })).toHaveTextContent(
      "Simulazione – denaro virtuale, prezzi di mercato reali",
    );
    unmount();
    current = { ...dashboard, modes: { simulazione: 1, paper: 0, reale: 1 } };
    renderApp("/");
    const banner = await screen.findByRole("status", { name: "Modalità di trading" });
    expect(banner).toHaveTextContent("denaro reale");
    expect(banner.className).toContain("text-loss");
  });

  it("follows the held symbols and refetches at most once per window while ticks arrive", async () => {
    current = { ...dashboard, held_symbols: ["AAPL", "BTC-USD"] };
    renderApp("/");
    await screen.findByText("12.345,60 €");
    await waitFor(() => expect(live.subscribed).toContainEqual(["AAPL", "BTC-USD"]));
    const before = fetches;
    current = { ...current, total_equity_eur: "13000" };
    for (let i = 0; i < 6; i++) tick("AAPL");
    expect(await screen.findByText("13.000,00 €")).toBeInTheDocument(); // leading refetch
    expect(fetches).toBe(before + 1);
    await waitFor(() => expect(fetches).toBe(before + 2), { timeout: 4500 }); // one trailing
    await act(() => new Promise((r) => setTimeout(r, 300)));
    expect(fetches).toBe(before + 2);
  });

  it("refetches on every relevant event, not on unrelated ones", async () => {
    renderApp("/");
    await screen.findByText("12.345,60 €");
    const before = fetches;
    emit({ type: "something.else" });
    await act(() => new Promise((r) => setTimeout(r, 100)));
    expect(fetches).toBe(before);
    current = { ...dashboard, total_equity_eur: "999" };
    emit({ type: "portfolio.cash", portfolio_id: "p1" });
    expect(await screen.findByText("999,00 €")).toBeInTheDocument();
    expect(fetches).toBe(before + 1);
  });

  it("shows «Live» while connected (even when quiet), «In pausa» only when the WS is down", async () => {
    const { unmount, queryClient } = renderApp("/");
    const indicator = await screen.findByRole("status", { name: "Aggiornamento dashboard" });
    await waitFor(() => expect(indicator).toHaveTextContent(/Live · ultimo aggiornamento \d{2}:\d{2}:\d{2}/));
    // N1: still polled while connected (dead worker, quiet market)
    const query = queryClient.getQueryCache().find({ queryKey: ["dashboard"], exact: true });
    expect(query?.observers[0]?.options.refetchInterval).toBe(45_000);
    // quiet for longer than the freshness window: still «Live», never «In pausa»
    const realNow = Date.now;
    Date.now = () => realNow() + 60_000;
    try {
      await act(() => new Promise((r) => setTimeout(r, 1100)));
      expect(indicator).toHaveTextContent("Live · ultimo aggiornamento");
      expect(indicator).not.toHaveTextContent("In pausa");
    } finally {
      Date.now = realNow;
    }
    unmount();
    live.status = "closed";
    const second = renderApp("/");
    const paused = await screen.findByRole("status", { name: "Aggiornamento dashboard" });
    await waitFor(() => expect(paused).toHaveTextContent("In pausa · ultimo aggiornamento"));
    const polled = second.queryClient.getQueryCache().find({ queryKey: ["dashboard"], exact: true });
    expect(polled?.observers[0]?.options.refetchInterval).toBe(30_000);
  });
});

describe("Dashboard halt banner", () => {
  it("shows the active halt and revokes it after confirmation", async () => {
    const user = userEvent.setup();
    let active = [haltFixture()];
    const cleared: string[] = [];
    server.use(
      http.get("*/api/halts", () => HttpResponse.json({ active, history: [] })),
      http.post("*/api/halts/:id/clear", ({ params }) => {
        cleared.push(String(params.id));
        const halt = { ...active[0], active: false };
        active = [];
        return HttpResponse.json({ halt, changed: true });
      }),
    );
    renderApp("/");
    const banner = await screen.findByRole("alert", { name: "Blocco aperture" });
    expect(banner).toHaveTextContent("Aperture sospese dal CEO — Drawdown oltre il 5% — dal 14/11/2026 10:30:00");
    expect(banner).toHaveTextContent("(impostato da CEO SMTrading)");
    await user.click(within(banner).getByRole("button", { name: "Revoca" }));
    const dialog = await screen.findByRole("dialog", { name: "Revocare il blocco aperture?" });
    await user.click(within(dialog).getByRole("button", { name: "Revoca" }));
    await waitFor(() => expect(cleared).toEqual(["1"]));
    await waitFor(() => expect(screen.queryByRole("alert", { name: "Blocco aperture" })).not.toBeInTheDocument());
  });
});
