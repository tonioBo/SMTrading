import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { ResearchRun, ResearchRunsResponse, StrategiesPerformanceResponse, StrategyExpectationsResponse } from "@/api/types";
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

const RUN_BASE = {
  agent: "Quant Researcher", symbols: ["SPY"], timeframe: "1d", from_date: "2023-01-01T00:00:00Z",
  to_date: "2025-12-31T00:00:00Z", initial_cash: "10000", portfolio_id: "p-agg", warnings: [], error: null,
  created_at: "2026-11-19T10:00:00Z", started_at: "2026-11-19T10:00:01Z", finished_at: "2026-11-19T10:00:09Z",
};
const wfRun: ResearchRun = {
  ...RUN_BASE, id: "wf1", kind: "walk_forward", strategy: "sma_cross", spec: {}, status: "completed",
  summary: { verdict: "fragile", oos_metrics: { total_return_pct: "4.25", max_drawdown_pct: "7.5" }, stability_pct: "50" },
};
const research: ResearchRunsResponse = {
  runs: [
    {
      ...RUN_BASE, id: "pf1", kind: "portfolio", strategy: null, status: "completed",
      spec: { sleeves: [{ strategy: "sma_cross", symbols: ["SPY"] }, { strategy: "momentum", symbols: ["QQQ"] }] },
      summary: { metrics: { total_return_pct: "12", max_drawdown_pct: "9", sharpe: "0.9" } },
    },
    {
      ...RUN_BASE, id: "sw1", kind: "param_sweep", strategy: "donchian", spec: {}, status: "failed", error: "timeout after 600s",
      summary: null,
    },
    wfRun,
  ],
};
const wfDetail: ResearchRun = {
  ...wfRun,
  result: {
    verdict: "fragile",
    reasons: ["parametri migliori instabili (stabilità 50.0000%)"],
    windows: [
      {
        window: 1, oos_from: "2024-06-01T00:00:00Z", oos_to: "2024-12-31T00:00:00Z", best_params: { fast: 10, slow: 50 },
        is_metrics: { total_return_pct: "8" }, oos_metrics: { total_return_pct: "3.5", max_drawdown_pct: "4", sharpe: "0.7", trades: 6 },
      },
    ],
    oos_equity: [],
  },
};

let current: StrategiesPerformanceResponse = body;
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/strategies/performance", () => HttpResponse.json(current)),
  http.get("*/api/strategies/expectations", () => HttpResponse.json(expectations)),
  http.get("*/api/strategies/research", () => HttpResponse.json(research)),
  http.get("*/api/strategies/research/wf1", () => HttpResponse.json(wfDetail)),
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

  it("lists the research runs and opens a walk-forward", async () => {
    renderApp("/strategie");
    await userEvent.click(await screen.findByRole("tab", { name: "Ricerca" }));
    const table = await screen.findByRole("table", { name: "Analisi di ricerca" });
    const [pf, sw, wf] = within(table).getAllByRole("row").slice(1);
    expect(within(pf).getByText("Portafoglio di strategie")).toBeInTheDocument();
    expect(within(pf).getByText("sma_cross + momentum")).toBeInTheDocument();
    expect(pf).toHaveTextContent("12,0% · DD 9,0% · Sharpe 0,90");
    expect(within(sw).getByText("fallita")).toBeInTheDocument();
    expect(within(sw).getByText("timeout after 600s")).toBeInTheDocument();
    expect(within(sw).queryByRole("button")).not.toBeInTheDocument();
    expect(within(wf).getByText("fragile")).toBeInTheDocument();
    expect(wf).toHaveTextContent("fuori campione 4,3% · DD 7,5% · stabilità 50%");
    expect(wf).toHaveTextContent("01/01/2023 – 31/12/2025 · 1d");
    await userEvent.click(within(wf).getByRole("button", { name: "Dettaglio analisi Walk-forward sma_cross" }));
    const windows = await screen.findByRole("table", { name: "Finestre del walk-forward" });
    const [w1] = within(windows).getAllByRole("row").slice(1);
    expect(w1).toHaveTextContent("fast 10 · slow 50");
    expect(w1).toHaveTextContent("8,0%");
    expect(w1).toHaveTextContent("3,5%");
    expect(screen.getByText("parametri migliori instabili (stabilità 50.0000%)")).toBeInTheDocument();
  });
});
