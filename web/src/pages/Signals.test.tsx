import { act, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { SignalsResponse } from "@/api/types";
import { SIGNALS_REFRESH_MS } from "@/api/hooks/signals";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const signals: SignalsResponse = {
  as_of: "2026-11-16T15:00:00Z",
  portfolios: [
    {
      portfolio_id: "p1",
      portfolio_name: "Crescita",
      strategies: [
        {
          ref: "STR-001",
          name: "RSI reversion",
          type: "rsi_reversion",
          params: { period: 14, entry: 30 },
          timeframe: "1d",
          status: "ok",
          symbols: [
            { symbol: "AAPL", last: "190.5", indicator: "RSI(14)", value_closed: "31.2", value_now: "29.8", threshold: "30", direction: "below", distance: "-0.2", triggered_closed: false, triggered_now: true, as_of: "2026-11-16T14:59:00Z" },
            { symbol: "MSFT", last: "410", indicator: "RSI(14)", value_closed: "34.5", value_now: null, threshold: "30", direction: "below", distance: "4.5", triggered_closed: false, triggered_now: false, as_of: "2026-11-16T14:58:00Z" },
            { symbol: "NVDA", last: "120", indicator: "RSI(14)", value_closed: "28", value_now: "33", threshold: "30", direction: "below", distance: "3", triggered_closed: true, triggered_now: false, as_of: "2026-11-16T14:58:00Z" },
            { symbol: "TSLA", last: "250", indicator: "RSI(14)", value_closed: "60", value_now: "61", threshold: "30", direction: "below", distance: "31", triggered_closed: false, triggered_now: false, as_of: "2026-11-16T14:58:00Z" },
          ],
        },
        { ref: "STR-002", name: "Pairs", type: "pairs", params: {}, timeframe: "1h", status: "non supportata", symbols: [] },
      ],
    },
  ],
};

let current: SignalsResponse = signals;
let fetches = 0;
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/signals", () => {
    fetches += 1;
    return HttpResponse.json(current);
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  current = signals;
  fetches = 0;
  vi.useRealTimers();
});
afterAll(() => server.close());

describe("Signals", () => {
  it("lists portfolios, strategies and symbols with their badges", async () => {
    renderApp("/segnali");
    expect(await screen.findByRole("heading", { name: "Segnali" })).toBeInTheDocument();
    expect(await screen.findByText("Crescita")).toBeInTheDocument();
    expect(screen.getByText("STR-001")).toBeInTheDocument();
    expect(screen.getByText("RSI reversion")).toBeInTheDocument();
    expect(screen.getByText("period=14, entry=30")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Segnali STR-001" });
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]).getByText("AAPL")).toBeInTheDocument();
    expect(within(rows[1]).getByText("🟢 scatterebbe ora")).toBeInTheDocument();
    expect(within(rows[1]).getByText("29,80")).toBeInTheDocument();
    expect(within(rows[1]).getByText("sotto 30,00")).toBeInTheDocument();
    expect(within(rows[2]).getByText("🟡 vicino")).toBeInTheDocument();
    expect(within(rows[2]).getByText("—")).toBeInTheDocument(); // value_now null
    expect(within(rows[3]).getByText("🟢 segnale sulla chiusura")).toBeInTheDocument();
    expect(within(rows[4]).getByText("⚪ lontano")).toBeInTheDocument();
    expect(screen.getByText("non supportata")).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    current = { as_of: "2026-11-16T15:00:00Z", portfolios: [] };
    renderApp("/segnali");
    expect(await screen.findByText("Nessuna strategia approvata")).toBeInTheDocument();
  });

  it("refreshes once a minute", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp("/segnali");
    expect(await screen.findByText("Crescita")).toBeInTheDocument();
    expect(fetches).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SIGNALS_REFRESH_MS - 1000);
    });
    expect(fetches).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetches).toBe(2);
  });

  it("is listed in the sidebar after Portafogli", async () => {
    renderApp("/segnali");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const labels = within(nav).getAllByRole("link").map((a) => a.textContent);
    expect(labels.indexOf("Segnali")).toBe(labels.indexOf("Portafogli") + 1);
  });
});
