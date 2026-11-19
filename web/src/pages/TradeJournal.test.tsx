import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { TradeDetail, TradeRow, TradesResponse } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

function trade(over: Partial<TradeRow>): TradeRow {
  return {
    key: "o1", portfolio_id: "p-agg", portfolio_name: "Portafoglio Aggressivo", symbol: "AAPL", currency: "USD",
    direction: "long", strategy_ref: "STR-7", strategy_name: "Approvata STR-7", proposal_ref: "PROP-AGG-1",
    order_id: "o1", opened_at: "2026-11-10T15:00:00Z", closed_at: "2026-11-11T15:30:00Z", outcome: "loss",
    qty: "5", open_qty: "0", entry_price: "200", exit_price: "149", mark: null, realized_pnl_eur: "-204",
    unrealized_pnl_eur: "0", costs_eur: "3.1", pnl_eur: "-207.1", pnl_pct: "-25.89", holding_s: 88200,
    exit_reason: "stop", exit_reason_label: "Stop loss", exit_reasons: ["stop"], ...over,
  };
}

const rows = [
  trade({ key: "o3", order_id: "o3", symbol: "MSFT", outcome: "open", closed_at: null, exit_price: null, mark: "420", open_qty: "6", qty: "10",
    exit_reason: "strategy_exit", exit_reason_label: "Uscita della strategia", exit_reasons: ["strategy_exit"], pnl_eur: "80", pnl_pct: "2.5", holding_s: 3600 }),
  trade({}),
];

function list(over: Partial<TradesResponse> = {}): TradesResponse {
  return {
    as_of: "2026-11-16T21:00:00Z", period: "all", total: 2, page: 1, page_size: 50,
    summary: { trades: 2, open: 1, closed: 1, wins: 0, win_rate_pct: "0.0", pnl_eur: "-127.1" },
    filters: { portfolios: [{ id: "p-agg", name: "Portafoglio Aggressivo" }], strategies: [{ ref: "STR-7", name: "Approvata STR-7" }], no_strategy: true, symbols: ["AAPL", "MSFT"] },
    items: rows, ...over,
  };
}

const detail: TradeDetail = {
  as_of: "2026-11-16T21:00:00Z",
  trade: trade({}),
  instrument_name: "Apple Inc",
  proposal: {
    id: 2, kind: "proposal", created_at: "2026-11-10T14:55:00Z", agent: "Portfolio Manager", summary: "PROP-AGG-1: buy AAPL RSI 27 sotto 30",
    subject: null, outcome: null, reason: null, symbols: ["AAPL"], details: { ref: "PROP-AGG-1", strategy_id: "STR-7", indicators: { rsi14: "27.4" } },
  },
  decisions: [],
  lessons: [{ id: 9, kind: "lesson", created_at: "2026-11-12T08:00:00Z", agent: "Portfolio Manager", summary: "Stop troppo stretto", subject: null, outcome: null, reason: null, symbols: [], details: {} }],
  orders: [
    {
      id: "o1", label: "ingresso", role: "entry", client_order_id: "PROP-AGG-1", side: "buy", type: "market", tif: "gtc", qty: "5", filled_qty: "5",
      limit_price: null, stop_price: null, avg_fill_price: "200", status: "filled", source: "agent", agent: "Execution Supervisor", reduce_only: false,
      parent_id: null, oco_group_id: "g1", created_at: "2026-11-10T15:00:00Z", updated_at: "2026-11-10T15:00:00Z", reject_code: null, reject_message: null, cancel_reason: null,
    },
    {
      id: "o2", label: "target", role: "take_profit", client_order_id: null, side: "sell", type: "limit", tif: "gtc", qty: "5", filled_qty: "0",
      limit_price: "250", stop_price: null, avg_fill_price: null, status: "canceled", source: "agent", agent: null, reduce_only: true,
      parent_id: "o1", oco_group_id: "g1", created_at: "2026-11-10T15:00:00Z", updated_at: "2026-11-11T15:30:00Z", reject_code: null, reject_message: null,
      cancel_reason: "eseguita l'altra gamba OCO, da sistema",
    },
  ],
  fills: [
    {
      fill_id: "f1", order_id: "o1", kind: "entry", side: "buy", ts: "2026-11-10T15:00:00Z", qty: "5", fill_qty: "5", price: "200", currency: "USD",
      fee_amount: "2", fee_currency: "EUR", fx: { sold: { currency: "EUR", amount: "802.5" }, bought: { currency: "USD", amount: "1000" }, rate: "1.25", fee: { EUR: "2.5" } },
      exit_reason: null, exit_reason_label: null,
    },
    {
      fill_id: "f2", order_id: "o3", kind: "exit", side: "sell", ts: "2026-11-11T15:30:00Z", qty: "5", fill_qty: "5", price: "149", currency: "USD",
      fee_amount: "1.1", fee_currency: "EUR", fx: null, exit_reason: "stop", exit_reason_label: "Stop loss",
    },
  ],
  changes: [],
  activity: [],
  timeline: [
    { ts: "2026-11-10T14:55:00Z", type: "decision.proposal", title: "Proposta · Portfolio Manager", text: "PROP-AGG-1: buy AAPL RSI 27 sotto 30", decision_id: 2, order_id: null },
    { ts: "2026-11-10T15:00:00Z", type: "fill.entry", title: "Ingresso eseguito", text: "acquisto 5 a 200 USD", decision_id: null, order_id: "o1" },
    { ts: "2026-11-11T15:30:00Z", type: "fill.exit", title: "Uscita eseguita · Stop loss", text: "vendita 5 a 149 USD", decision_id: null, order_id: "o3" },
  ],
};

const seen: string[] = [];
let current: TradesResponse = list();
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/trades", ({ request }) => {
    seen.push(new URL(request.url).search);
    return HttpResponse.json(current);
  }),
  http.get("*/api/trades/:key", ({ params }) =>
    params.key === "o1" ? HttpResponse.json(detail) : HttpResponse.json({ code: "NOT_FOUND", message: "operazione non trovata" }, { status: 404 }),
  ),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  current = list();
  seen.length = 0;
});
afterAll(() => server.close());

describe("TradeJournal", () => {
  it("lists the trades with outcome, exit reason, result and the summary", async () => {
    renderApp("/diario-operazioni");
    expect(await screen.findByRole("heading", { name: "Diario operazioni" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Operazioni" });
    const [open, stopped] = within(table).getAllByRole("row").slice(1);
    expect(open).toHaveTextContent("MSFT");
    expect(within(open).getByText("Aperta")).toBeInTheDocument();
    expect(open).toHaveTextContent("(aperte 6)");
    expect(open).toHaveTextContent("420,00 $");
    expect(open).toHaveTextContent("Uscita della strategia");
    expect(within(stopped).getByText("Perdente")).toBeInTheDocument();
    expect(stopped).toHaveTextContent("Stop loss");
    expect(stopped).toHaveTextContent("-207,10 €(-25,89%)");
    expect(stopped).toHaveTextContent("1 g");
    expect(stopped).toHaveTextContent("PROP-AGG-1");
    expect(within(stopped).getByRole("link", { name: /Dettaglio AAPL/ })).toHaveAttribute("href", "/diario-operazioni/o1");
    expect(screen.getByLabelText("Riepilogo")).toHaveTextContent("2 operazioni · 1 aperte · 1 chiuse · vincenti 0,0% (0/1) · risultato -127,10 €");
    // the same trades as cards on small screens
    const cards = screen.getByRole("list", { name: "Operazioni (schede)" });
    expect(within(cards).getAllByRole("listitem")).toHaveLength(2);
  });

  it("filters by the query string and the selects, and resets the page", async () => {
    renderApp("/diario-operazioni?strategy=STR-7&page=2");
    await screen.findByRole("table", { name: "Operazioni" });
    expect(seen.at(-1)).toContain("strategy=STR-7");
    expect(seen.at(-1)).toContain("page=2");
    expect(screen.getByLabelText("Strategia")).toHaveValue("STR-7");
    await userEvent.selectOptions(screen.getByLabelText("Esito"), "win");
    await vi.waitFor(() => expect(seen.at(-1)).toContain("outcome=win"));
    expect(seen.at(-1)).toContain("page=1");
    await userEvent.selectOptions(screen.getByLabelText("Periodo"), "1m");
    await vi.waitFor(() => expect(seen.at(-1)).toContain("period=1m"));
    expect(screen.getByRole("option", { name: "Senza strategia" })).toBeInTheDocument();
  });

  it("pages and shows the empty state", async () => {
    current = list({ total: 120 });
    renderApp("/diario-operazioni");
    expect(await screen.findByText("Pagina 1 di 3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Successive" }));
    await vi.waitFor(() => expect(seen.at(-1)).toContain("page=2"));
    current = list({ total: 0, items: [] });
    renderApp("/diario-operazioni?outcome=open");
    expect(await screen.findByText("Nessuna operazione con questi filtri")).toBeInTheDocument();
  });

  it("is listed in the sidebar after Posizioni", async () => {
    renderApp("/diario-operazioni");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const names = within(nav).getAllByRole("link").map((a) => a.textContent);
    expect(names.indexOf("Diario operazioni")).toBe(names.indexOf("Posizioni") + 1);
  });
});

describe("TradeDetail", () => {
  it("shows the summary, the timeline, the proposal's signal, lessons, orders and fills", async () => {
    renderApp("/diario-operazioni/o1");
    expect(await screen.findByRole("heading", { name: "Operazione AAPL" })).toBeInTheDocument();
    expect(screen.getByText("AAPL · Apple Inc")).toBeInTheDocument();
    expect(screen.getByText("Stop loss", { selector: "dd" })).toBeInTheDocument();
    const timeline = screen.getByRole("list", { name: "Cronologia" });
    const items = within(timeline).getAllByRole("listitem");
    expect(items.map((i) => i.querySelector(".font-medium")?.textContent)).toEqual([
      "Proposta · Portfolio Manager",
      "Ingresso eseguito",
      "Uscita eseguita · Stop loss",
    ]);
    expect(screen.getByText("indicators")).toBeInTheDocument();
    expect(screen.getByText('{"rsi14":"27.4"}')).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Lezioni" })).getByText("Stop troppo stretto")).toBeInTheDocument();
    const orders = screen.getByRole("table", { name: "Ordini" });
    const [, tp] = within(orders).getAllByRole("row").slice(1);
    expect(tp).toHaveTextContent("vendita limite");
    expect(tp).toHaveTextContent("annullato");
    expect(tp).toHaveTextContent("eseguita l'altra gamba OCO, da sistema");
    const fills = screen.getByRole("table", { name: "Esecuzioni" });
    const [entry, exit] = within(fills).getAllByRole("row").slice(1);
    expect(entry).toHaveTextContent("802,50 € → 1.000,00 $ a 1.25 (costo 2,50 €)");
    expect(exit).toHaveTextContent("Uscita · Stop loss");
    expect(screen.getByRole("link", { name: "Torna al diario operazioni" })).toHaveAttribute("href", "/diario-operazioni");
  });

  it("says when the trade does not exist", async () => {
    renderApp("/diario-operazioni/zzz");
    expect(await screen.findByText("operazione non trovata")).toBeInTheDocument();
  });
});
