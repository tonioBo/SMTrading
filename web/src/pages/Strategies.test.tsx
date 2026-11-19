import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { StrategiesPerformanceResponse, StrategyExpectationsResponse } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const body: StrategiesPerformanceResponse = {
  as_of: "2026-11-16T21:00:00Z",
  strategies: [
    {
      ref: "STR-103",
      name: "Approvata con limiti STR-103 rsi_reversion",
      status: "approvata",
      portfolio_id: "p-agg",
      portfolio_name: "Portafoglio Aggressivo",
      trades: 2,
      closed_trades: 1,
      open_positions: 1,
      win_rate_pct: "100.0",
      realized_pnl_eur: "12.5",
      unrealized_pnl_eur: null,
      costs_eur: "0.36",
      total_pnl_eur: null,
      first_trade_at: "2026-11-13T15:00:00Z",
      last_trade_at: "2026-11-16T20:30:00Z",
      trade_list: [
        {
          symbol: "AMZN", direction: "long", order_id: "o2", client_order_id: "PROP-AGG-20261116-2130-1",
          opened_at: "2026-11-16T20:30:00Z", closed_at: null, qty: "0.51", open_qty: "0.51", currency: "USD",
          entry_price: "258", exit_price: null, mark: null, realized_pnl_eur: "0", unrealized_pnl_eur: null,
          costs_eur: "0.18", pnl_eur: null,
        },
        {
          symbol: "QQQ", direction: "long", order_id: "o1", client_order_id: "PROP-AGG-1",
          opened_at: "2026-11-13T15:00:00Z", closed_at: "2026-11-14T15:00:00Z", qty: "1", open_qty: "0", currency: "USD",
          entry_price: "500", exit_price: "515.5", mark: null, realized_pnl_eur: "12.5", unrealized_pnl_eur: "0",
          costs_eur: "0.18", pnl_eur: "12.32",
        },
      ],
    },
    {
      ref: null, name: null, status: null, portfolio_id: "p-agg", portfolio_name: "Portafoglio Aggressivo",
      trades: 1, closed_trades: 1, open_positions: 0, win_rate_pct: "0.0", realized_pnl_eur: "-3",
      unrealized_pnl_eur: "0", costs_eur: "1", total_pnl_eur: "-4",
      first_trade_at: "2026-11-13T10:00:00Z", last_trade_at: "2026-11-13T11:00:00Z", trade_list: [],
    },
  ],
};

const expectations: StrategyExpectationsResponse = {
  as_of: "2026-11-16T21:00:00Z",
  min_trades: 5,
  strategies: [
    {
      ref: "STR-7",
      name: "Approvata STR-7 rsi_reversion",
      status: "approvata",
      approved_at: "2026-10-01T08:00:00Z",
      expected: {
        source: "approvazione", backtest_id: "bt1", from_date: "2025-11-01", to_date: "2026-11-01", trades: 12,
        trades_per_month: "1.00", win_rate_pct: "50", avg_trade_pct: "4", max_drawdown_pct: "10", sharpe: "1.1", total_return_pct: "14",
      },
      live: {
        since: "2026-10-01T08:00:00Z", trades: 6, open_trades: 0, closed_trades: 6, wins: 1, win_rate_pct: "16.7", avg_trade_pct: "-2.5",
        pnl_eur: "-40.2", max_drawdown_pct: "14.2", max_drawdown_eur: "45", trades_per_month: "3.9", days: 46,
      },
      verdict: "sotto le attese",
      reasons: ["vincenti 16,7% contro 50,0% attese"],
    },
    {
      ref: "STR-9",
      name: "Approvata STR-9",
      status: "sospesa",
      approved_at: "2026-11-10T08:00:00Z",
      expected: {
        source: null, backtest_id: null, from_date: null, to_date: null, trades: null, trades_per_month: null, win_rate_pct: null,
        avg_trade_pct: null, max_drawdown_pct: null, sharpe: null, total_return_pct: null,
      },
      live: {
        since: "2026-11-10T08:00:00Z", trades: 0, open_trades: 0, closed_trades: 0, wins: 0, win_rate_pct: null, avg_trade_pct: null,
        pnl_eur: null, max_drawdown_pct: null, max_drawdown_eur: null, trades_per_month: "0", days: 6,
      },
      verdict: "dati insufficienti",
      reasons: ["metriche del backtest non registrate nell'approvazione"],
    },
  ],
};

let current: StrategiesPerformanceResponse = body;
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/strategies/performance", () => HttpResponse.json(current)),
  http.get("*/api/strategies/expectations", () => HttpResponse.json(expectations)),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  current = body;
});
afterAll(() => server.close());

describe("Strategies", () => {
  it("lists one row per strategy and portfolio, Italian formatting, «—» when unknown", async () => {
    renderApp("/strategie");
    expect(await screen.findByRole("heading", { name: "Strategie" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Risultati per strategia" });
    expect(screen.getByText(/Realizzato e costi al cambio di oggi/)).toBeInTheDocument();
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    const [str, none] = rows;
    expect(within(str).getByText("STR-103")).toBeInTheDocument();
    expect(within(str).getByText("Approvata con limiti STR-103 rsi_reversion")).toBeInTheDocument();
    expect(within(str).getByRole("link", { name: "Portafoglio Aggressivo" })).toHaveAttribute("href", "/portafogli/p-agg");
    expect(within(str).getByText("approvata")).toBeInTheDocument();
    expect(within(str).getByText("100,0%")).toBeInTheDocument();
    expect(within(str).getByText("+12,50 €")).toBeInTheDocument();
    expect(within(str).getByText("0,36 €")).toBeInTheDocument();
    expect(within(str).getAllByText("—")).toHaveLength(2); // unrealised and total unknown
    expect(within(none).getByText("Senza strategia")).toBeInTheDocument();
    expect(within(none).getByText("-4,00 €")).toBeInTheDocument();
  });

  it("opens the trades of a strategy", async () => {
    renderApp("/strategie");
    const button = await screen.findByRole("button", { name: "Dettaglio STR-103 Portafoglio Aggressivo" });
    expect(screen.queryByRole("table", { name: /Operazioni STR-103/ })).not.toBeInTheDocument();
    await userEvent.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    const trades = screen.getByRole("table", { name: "Operazioni STR-103 Portafoglio Aggressivo" });
    const [open, closed] = within(trades).getAllByRole("row").slice(1);
    expect(within(open).getByText("AMZN")).toBeInTheDocument();
    expect(within(open).getByText("aperta")).toBeInTheDocument();
    expect(within(open).getByText("258,00 $")).toBeInTheDocument();
    expect(within(open).getByText("PROP-AGG-20261116-2130-1")).toBeInTheDocument();
    expect(within(closed).getByText("515,50 $")).toBeInTheDocument();
    expect(within(closed).getByText("+12,32 €")).toBeInTheDocument();
    expect(within(closed).getByText("14/11/2026 16:00:00")).toBeInTheDocument();
  });

  it("says so when nothing was executed", async () => {
    current = { as_of: body.as_of, strategies: [] };
    renderApp("/strategie");
    expect(await screen.findByText("Nessuna operazione eseguita")).toBeInTheDocument();
  });

  it("compares the backtest with the live results in its own tab", async () => {
    renderApp("/strategie");
    const tab = await screen.findByRole("tab", { name: "Previsto contro reale" });
    await userEvent.click(tab);
    const table = await screen.findByRole("table", { name: "Previsto contro reale" });
    const [s7, s9] = within(table).getAllByRole("row").slice(1);
    expect(within(s7).getByRole("link", { name: "STR-7" })).toHaveAttribute("href", "/diario-operazioni?strategy=STR-7");
    expect(s7).toHaveTextContent("50,0% · 16,7%");
    expect(s7).toHaveTextContent("4,00% · -2,50%");
    expect(s7).toHaveTextContent("10,0% · 14,2%");
    expect(s7).toHaveTextContent("12 · 6/6");
    expect(within(s7).getByText("sotto le attese")).toBeInTheDocument();
    expect(within(s7).getByText("vincenti 16,7% contro 50,0% attese")).toBeInTheDocument();
    expect(s7).toHaveTextContent("backtest 01/11/2025 – 01/11/2026");
    expect(within(s9).getByText("dati insufficienti")).toBeInTheDocument();
    expect(s9).toHaveTextContent("— · —");
    // the cards for small screens carry the same verdicts
    const cards = screen.getByRole("list", { name: "Previsto contro reale (schede)" });
    expect(within(cards).getAllByRole("listitem")).toHaveLength(2);
  });
});
