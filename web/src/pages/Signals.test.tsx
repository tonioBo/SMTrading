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
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/signals", () => {
    fetches += 1;
    return HttpResponse.json(current);
  }),
  ...commonHandlers, // last: the handlers above (this page's data) take precedence
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

  it("shows «in posizione» / «ordine in attesa» instead of the signal, keeping the values", async () => {
    const base = signals.portfolios[0].strategies[0];
    current = {
      ...signals,
      portfolios: [{
        ...signals.portfolios[0],
        strategies: [{
          ...base,
          symbols: [
            { ...base.symbols[0], position: { qty: "0.51", avg_price: "258" }, open_entry: false },
            { ...base.symbols[2], position: null, open_entry: true },
          ],
        }],
      }],
    };
    renderApp("/segnali");
    const table = await screen.findByRole("table", { name: "Segnali STR-001" });
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]).getByText("📌 in posizione")).toBeInTheDocument();
    expect(within(rows[1]).queryByText("🟢 scatterebbe ora")).not.toBeInTheDocument();
    expect(within(rows[1]).getByText("29,80")).toBeInTheDocument();
    expect(within(rows[2]).getByText("⏳ ordine in attesa")).toBeInTheDocument();
    expect(within(rows[2]).getByText("sotto 30,00")).toBeInTheDocument();
  });

  it("in position: the exit rule line with its badge, both sides", async () => {
    const base = signals.portfolios[0].strategies[0];
    const held = { position: { qty: "1", avg_price: "100" }, open_entry: false };
    current = {
      ...signals,
      portfolios: [{
        ...signals.portfolios[0],
        strategies: [{
          ...base,
          symbols: [
            { ...base.symbols[0], ...held, exit: { rule: "RSI 14 sopra 70", value: "8.04", threshold: "70", distance: "61.96", triggered: false } },
            { ...base.symbols[1], ...held, exit: { rule: "RSI 14 sopra 70", value: "75.5", threshold: "70", distance: "-5.5", triggered: true } },
            { ...base.symbols[2], ...held, exit: { rule: "RSI 14 sopra 70", value: "60", threshold: "70", distance: "10", triggered: false } },
            { ...base.symbols[3], position: null, exit: null },
          ],
        }],
      }],
    };
    renderApp("/segnali");
    const table = await screen.findByRole("table", { name: "Segnali STR-001" });
    const rows = within(table).getAllByRole("row");
    expect(within(rows[1]).getByText("uscita: RSI 14 sopra 70 · attuale 8,0")).toBeInTheDocument();
    expect(within(rows[1]).queryByText(/uscita (scatterebbe|vicina)/)).not.toBeInTheDocument();
    expect(within(rows[2]).getByText("🔻 uscita scatterebbe")).toBeInTheDocument();
    expect(within(rows[3]).getByText("🟡 uscita vicina")).toBeInTheDocument();
    expect(within(rows[4]).queryByText(/uscita/)).not.toBeInTheDocument();
  });

  it("cross_up already above: «già sopra · attende un nuovo incrocio», not «vicino»", async () => {
    current = {
      ...signals,
      portfolios: [{
        ...signals.portfolios[0],
        strategies: [{
          ref: "STR-106", name: "Incrocio", type: "sma_cross", params: { fast: 1, slow: 50 }, timeframe: "1d", status: "ok",
          symbols: [
            { symbol: "DIA", last: "440", indicator: "Chiusura / SMA 50", value_closed: "440", value_now: "441", threshold: "435", direction: "cross_up", distance: "-6", triggered_closed: false, triggered_now: false, as_of: "2026-11-16T14:58:00Z" },
          ],
        }],
      }],
    };
    renderApp("/segnali");
    const table = await screen.findByRole("table", { name: "Segnali STR-106" });
    expect(within(table).getByText("⚪ già sopra · attende un nuovo incrocio")).toBeInTheDocument();
    expect(within(table).getByText("Chiusura / SMA 50")).toBeInTheDocument();
    expect(within(table).queryByText("🟡 vicino")).not.toBeInTheDocument();
  });

  it("renders «—» for a symbol whose data could not be read", async () => {
    current = {
      as_of: "2026-11-16T15:00:00Z",
      portfolios: [
        {
          portfolio_id: "p1",
          portfolio_name: "Crescita",
          strategies: [
            {
              ref: "STR-009",
              name: "RSI rotto",
              type: "rsi_reversion",
              params: { period: 14, entry: 30 },
              timeframe: "1d",
              status: "dati incompleti",
              symbols: [
                { symbol: "ZZZ", last: null, indicator: null, value_closed: null, value_now: null, threshold: null, direction: null, distance: null, triggered_closed: null, triggered_now: null, as_of: null },
              ],
            },
          ],
        },
      ],
    };
    renderApp("/segnali");
    const table = await screen.findByRole("table", { name: "Segnali STR-009" });
    const row = within(table).getAllByRole("row")[1];
    expect(within(row).getByText("ZZZ")).toBeInTheDocument();
    expect(within(row).getAllByText("—").length).toBeGreaterThanOrEqual(7);
    expect(within(row).getByText("⚪ lontano")).toBeInTheDocument();
    expect(screen.getByText("dati incompleti")).toBeInTheDocument();
  });

  it("shows strategies without portfolio as plain text and small values with more decimals", async () => {
    current = {
      as_of: "2026-11-16T15:00:00Z",
      portfolios: [
        {
          portfolio_id: null,
          portfolio_name: null,
          strategies: [
            {
              ref: "STR-010",
              name: "Breakout FX",
              type: "breakout",
              params: { lookback: 20 },
              timeframe: "1d",
              status: "ok",
              symbols: [
                { symbol: "EURUSD", last: "1.0842", indicator: "max 20", value_closed: "1.0812", value_now: "1.0842", threshold: "1.0875", direction: "above", distance: "0.0033", triggered_closed: false, triggered_now: false, as_of: "2026-11-16T14:59:00Z" },
              ],
            },
          ],
        },
      ],
    };
    renderApp("/segnali");
    expect(await screen.findByText("Senza portafoglio")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Senza portafoglio" })).not.toBeInTheDocument();
    const row = within(screen.getByRole("table", { name: "Segnali STR-010" })).getAllByRole("row")[1];
    expect(within(row).getByText("0,0033")).toBeInTheDocument();
    expect(within(row).getByText("sopra 1,0875")).toBeInTheDocument();
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
