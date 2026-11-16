import { act, screen, within } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { POSITIONS_REFRESH_MS } from "@/api/hooks/positions";
import type { OpenPosition, OpenPositionsResponse } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

function pos(over: Partial<OpenPosition>): OpenPosition {
  return {
    portfolio_id: "p1", portfolio_name: "Portafoglio Aggressivo", symbol: "AMZN", name: "Amazon.com Inc",
    market: "NASDAQ", currency: "USD", side: "long", qty: "0.51", avg_price: "258", opened_at: "2026-11-16T20:29:22Z",
    last: "262.4", stale: false, value_eur: "115.32", pnl_eur: "1.93", pnl_pct: "1.71",
    stop: { order_id: "o1", price: "242.2", tif: "gtc" }, target: { order_id: "o2", price: "284", tif: "gtc" },
    distance_to_stop_pct: "7.70", risk_at_stop_eur: "7.75", protection: "ok", ...over,
  };
}

const body: OpenPositionsResponse = {
  as_of: "2026-11-16T21:00:00Z",
  positions: [
    pos({}),
    pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "ENI.MI", name: null, currency: "EUR", stop: null, target: null, distance_to_stop_pct: null, risk_at_stop_eur: null, protection: "senza stop" }),
    pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "BTC-USD", stop: { order_id: "o3", price: "90000", tif: "day" }, protection: "scade oggi" }),
    pos({ portfolio_id: "p2", portfolio_name: "Prudente", symbol: "ZZZ", last: null, value_eur: null, pnl_eur: null, pnl_pct: null, distance_to_stop_pct: null, risk_at_stop_eur: null }),
  ],
  portfolios: [
    {
      portfolio_id: "p1", portfolio_name: "Portafoglio Aggressivo", mode: "reale", live_state: "armed", mode_label: "eToro · reale",
      positions: 1, value_eur: "115.32", pnl_eur: "1.93", risk_at_stop_eur: "7.75", risk_unknown: 0, unprotected: 0,
      expiring_today: 0, daily_loss_limit_eur: "40", risk_pct_of_limit: "19.38",
    },
    {
      portfolio_id: "p2", portfolio_name: "Prudente", mode: "simulazione", live_state: null, mode_label: "SMTrading (simulato)",
      positions: 3, value_eur: null, pnl_eur: null, risk_at_stop_eur: "0", risk_unknown: 1, unprotected: 1,
      expiring_today: 1, daily_loss_limit_eur: null, risk_pct_of_limit: null,
    },
  ],
};

let current: OpenPositionsResponse = body;
let fetches = 0;
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/positions/open", () => {
    fetches += 1;
    return HttpResponse.json(current);
  }),
  ...commonHandlers, // last: the handlers above (this page's data) take precedence
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  current = body;
  fetches = 0;
  vi.useRealTimers();
});
afterAll(() => server.close());

describe("Positions", () => {
  it("groups the positions by portfolio with protection badges and the risk line", async () => {
    renderApp("/posizioni");
    expect(await screen.findByRole("heading", { name: "Posizioni" })).toBeInTheDocument();
    expect(await screen.findByText("Rischio agli stop: 7,75 € su 40,00 € (19%)")).toBeInTheDocument();
    const agg = screen.getByRole("table", { name: "Posizioni Portafoglio Aggressivo" });
    const row = within(agg).getAllByRole("row")[1];
    expect(within(row).getByText("AMZN")).toBeInTheDocument();
    expect(within(row).getByText("0,51")).toBeInTheDocument();
    expect(within(row).getByText("242,20 $")).toBeInTheDocument();
    expect(within(row).getByText("284,00 $")).toBeInTheDocument();
    expect(within(row).getByText("7,70%")).toBeInTheDocument();
    expect(within(row).getByText("7,75 €")).toBeInTheDocument();
    expect(within(row).getByText("🟢 protetta")).toBeInTheDocument();

    const pru = screen.getByRole("table", { name: "Posizioni Prudente" });
    const rows = within(pru).getAllByRole("row");
    expect(within(rows[1]).getByText("🔴 senza stop")).toBeInTheDocument();
    expect(within(rows[2]).getByText("🟠 scade oggi")).toBeInTheDocument();
    expect(within(rows[2]).getByText("giornaliero")).toBeInTheDocument();
    // no quote: «—», never 0
    expect(within(rows[3]).getAllByText("—").length).toBeGreaterThanOrEqual(5);
    expect(within(rows[3]).queryByText("0,00 €")).not.toBeInTheDocument();
    expect(screen.getByText("Rischio agli stop: 0,00 € (nessun limite di perdita giornaliera)")).toBeInTheDocument();
    expect(screen.getByText("1 senza stop")).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    current = { as_of: "2026-11-16T21:00:00Z", positions: [], portfolios: [] };
    renderApp("/posizioni");
    expect(await screen.findByText("Nessuna posizione aperta")).toBeInTheDocument();
  });

  it("refreshes once a minute", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    renderApp("/posizioni");
    expect(await screen.findByText("Rischio agli stop: 7,75 € su 40,00 € (19%)")).toBeInTheDocument();
    expect(fetches).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POSITIONS_REFRESH_MS - 1000);
    });
    expect(fetches).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetches).toBe(2);
  });

  it("is listed in the sidebar after Segnali", async () => {
    renderApp("/posizioni");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const labels = within(nav).getAllByRole("link").map((a) => a.textContent);
    expect(labels.indexOf("Posizioni")).toBe(labels.indexOf("Segnali") + 1);
  });
});
