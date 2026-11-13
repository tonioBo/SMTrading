import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Order, Portfolio, Position, Transaction, Valuation } from "@/api/types";
import { renderApp } from "@/test/render";

const ws = vi.hoisted(() => ({
  handlers: new Set<{ type: string; call: (p: unknown) => void }>(),
  subscribed: [] as string[][],
}));
vi.mock("@/lib/ws", async () => {
  const { useEffect, useRef } = await import("react");
  const socket = {
    subscribeQuotes: (symbols: string[]) => {
      ws.subscribed.push(symbols);
      return () => undefined;
    },
  };
  return {
    useLiveSocket: () => ({ socket, status: "open" }),
    useLiveMessage: (type: string, handler: (p: unknown) => void) => {
      const ref = useRef(handler);
      ref.current = handler;
      useEffect(() => {
        const entry = { type, call: (p: unknown) => ref.current(p) };
        ws.handlers.add(entry);
        return () => void ws.handlers.delete(entry);
      }, [type]);
    },
  };
});
vi.mock("lightweight-charts", () => {
  const series = { setData: vi.fn() };
  const chart = { addAreaSeries: () => series, timeScale: () => ({ fitContent: vi.fn() }), remove: vi.fn() };
  return { createChart: vi.fn(() => chart), ColorType: { Solid: "solid" } };
});

function emitQuote(quote: Record<string, unknown>) {
  act(() => ws.handlers.forEach((h) => h.type === "quote" && h.call(quote)));
}

const portfolio: Portfolio = {
  id: "p1",
  name: "Alfa",
  base_currency: "EUR",
  leverage: "2",
  maintenance_margin_pct: "50",
  warning_margin_pct: "75",
  slippage_bps: 5,
  status: "active",
  timezone: "Europe/Rome",
  risk_profile_id: "r1",
  reduce_only_reason: null,
  reduce_only_until: null,
  created_at: "2026-11-01T10:00:00Z",
  equity_eur: "10500.25",
  day_pnl_eur: "120.5",
  day_pnl_pct: "1.16",
  valuation_error: null,
};

const valuation: Valuation = {
  portfolio_id: "p1",
  base_currency: "EUR",
  cash: { EUR: "8000.5", USD: "1200" },
  collateral: {},
  margin_loan: {},
  equity_eur: "10500.25",
  cash_eur: "9100",
  exposure_eur: "3000",
  initial_req_eur: "1500",
  maint_req_eur: "750",
  warn_req_eur: "1125",
  buying_power_eur: "17000",
  stale_symbols: [],
  ts: "2026-11-13T10:00:00Z",
  margin_level: "ok",
  positions: [],
};

const positions: Position[] = [
  {
    symbol: "AAPL",
    qty: "10",
    avg_price: "180",
    mark: "190.5",
    currency: "USD",
    market_value: "1905",
    market_value_eur: "1750.12",
    unrealized_pnl: "-105",
    unrealized_pnl_eur: "-96.5",
    stale: false,
    side: "long",
  },
];

function tx(seq: number, type: Transaction["type"], extra: Partial<Transaction> = {}): Transaction {
  return {
    id: `j${seq}`,
    seq,
    type,
    created_at: "2026-11-12T10:00:00Z",
    reason: null,
    order_id: null,
    author_type: "admin",
    author_id: "admin@example.com",
    reversed_journal_id: null,
    reversed_by: null,
    lines: [{ asset: "EUR", kind: "cash", quantity: "500", unit_price: null, price_ccy: null }],
    ...extra,
  };
}

const page1 = [tx(9, "DEPOSIT", { reason: "versamento" }), tx(8, "TRADE"), tx(7, "FEE", { reversed_by: "j10" })];
const page2 = [tx(4, "WITHDRAWAL")];

const openOrder: Order = {
  id: "o1",
  portfolio_id: "p1",
  agent_id: null,
  client_order_id: null,
  symbol: "MSFT",
  side: "buy",
  type: "limit",
  tif: "gtc",
  qty: "3",
  notional: null,
  limit_price: "400",
  stop_price: null,
  filled_qty: "0",
  avg_fill_price: null,
  status: "pending",
  reduce_only: false,
  reject_code: null,
  reject_message: null,
  source: "agent",
  created_at: "2026-11-12T10:00:00Z",
  updated_at: "2026-11-12T10:00:00Z",
};

let current: Portfolio = portfolio;
let txRequests: URL[] = [];
let cashRequests: { key: string | null; body: unknown }[] = [];
let releaseDeposit: (() => void) | null = null;
let depositFails = false;
let reverseRequests: { id: string; body: unknown }[] = [];
let reverseError: string | null = null;
let resetRequests: unknown[] = [];
let patches: unknown[] = [];
let canceled: string[] = [];
let orderStatuses: (string | null)[] = [];

const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/portfolios/p1", () => HttpResponse.json(current)),
  http.patch("*/api/portfolios/p1", async ({ request }) => {
    patches.push(await request.json());
    current = { ...current, status: "active", reduce_only_reason: null };
    return HttpResponse.json(current);
  }),
  http.get("*/api/portfolios/p1/valuation", () => HttpResponse.json(valuation)),
  http.get("*/api/portfolios/p1/positions", () => HttpResponse.json(positions)),
  http.get("*/api/portfolios/p1/performance", () =>
    HttpResponse.json({ series: [], return_pct: "5", max_drawdown_pct: "-2.5", sharpe: "1.234" }),
  ),
  http.get("*/api/portfolios/p1/orders", ({ request }) => {
    orderStatuses.push(new URL(request.url).searchParams.get("status"));
    return HttpResponse.json([openOrder]);
  }),
  http.post("*/api/orders/:id/cancel", ({ params }) => {
    canceled.push(String(params.id));
    return HttpResponse.json({ ...openOrder, status: "canceled" });
  }),
  http.get("*/api/risk-profiles", () =>
    HttpResponse.json([
      {
        id: "r1",
        name: "Prudente",
        max_order_notional_eur: "5000",
        max_position_pct: "20",
        max_daily_loss_pct: "3",
        max_daily_loss_eur: null,
        allowed_symbols: [],
        allowed_asset_classes: [],
        allowed_markets: [],
        short_allowed: true,
      },
    ]),
  ),
  http.get("*/api/portfolios/p1/transactions", ({ request }) => {
    const url = new URL(request.url);
    txRequests.push(url);
    return url.searchParams.get("before_seq") === "7"
      ? HttpResponse.json({ items: page2, next_before_seq: null })
      : HttpResponse.json({ items: page1, next_before_seq: 7 });
  }),
  http.post("*/api/portfolios/p1/deposit", async ({ request }) => {
    cashRequests.push({ key: request.headers.get("Idempotency-Key"), body: await request.json() });
    await new Promise<void>((resolve) => (releaseDeposit = resolve));
    if (depositFails) {
      return HttpResponse.json({ code: "INTERNAL", message: "errore temporaneo", details: null }, { status: 500 });
    }
    return HttpResponse.json({ journal: {}, cash: {} });
  }),
  http.post("*/api/journals/:id/reverse", async ({ request, params }) => {
    reverseRequests.push({ id: String(params.id), body: await request.json() });
    if (reverseError) return HttpResponse.json({ code: "INVALID_REVERSAL", message: reverseError, details: null }, { status: 400 });
    return HttpResponse.json({});
  }),
  http.post("*/api/portfolios/p1/reset", async ({ request }) => {
    resetRequests.push({ key: request.headers.get("Idempotency-Key"), body: await request.json() });
    return HttpResponse.json(
      { code: "INVALID_ORDER", message: "posizioni aperte su mercati chiusi", details: { symbols: ["AAPL"] } },
      { status: 400 },
    );
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  current = portfolio;
  txRequests = [];
  cashRequests = [];
  releaseDeposit = null;
  depositFails = false;
  reverseRequests = [];
  reverseError = null;
  resetRequests = [];
  patches = [];
  canceled = [];
  orderStatuses = [];
  ws.subscribed.length = 0;
});
afterAll(() => server.close());

async function openTab(name: string) {
  const user = userEvent.setup();
  renderApp("/portafogli/p1");
  await user.click(await screen.findByRole("tab", { name }));
  return user;
}

describe("PortfolioDetail", () => {
  it("shows KPIs and cash per currency in the overview", async () => {
    renderApp("/portafogli/p1");
    expect(await screen.findByRole("heading", { name: "Alfa" })).toBeInTheDocument();
    expect(screen.getByText("10.500,25 €")).toBeInTheDocument();
    expect(await screen.findByText("+5,00%")).toBeInTheDocument();
    const cash = await screen.findByRole("list", { name: "Cash per valuta" });
    expect(within(cash).getByText("8.000,50 €")).toBeInTheDocument();
    expect(within(cash).getByText("1.200,00 $")).toBeInTheDocument();
  });

  it("disables Deposita while the request is pending and reuses the key on retry", async () => {
    depositFails = true;
    const user = await openTab("Operazioni");
    await user.click(screen.getByRole("button", { name: "Nuovo deposito" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Importo"), "1.500,50");
    const confirm = within(dialog).getByRole("button", { name: "Deposita" });
    await user.click(confirm);
    await waitFor(() => expect(confirm).toBeDisabled());
    expect(cashRequests).toHaveLength(1);
    expect(cashRequests[0].body).toEqual({ amount: "1500.50", currency: "EUR" });
    const firstKey = cashRequests[0].key;
    expect(firstKey).toMatch(/^[0-9a-f-]{36}$/);
    act(() => releaseDeposit!());
    expect(await within(dialog).findByText("errore temporaneo")).toBeInTheDocument();
    expect(confirm).toBeEnabled();

    depositFails = false;
    await user.click(confirm);
    await waitFor(() => expect(cashRequests).toHaveLength(2));
    expect(cashRequests[1].key).toBe(firstKey);
    act(() => releaseDeposit!());
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "Nuovo deposito" }));
    await user.type(within(screen.getByRole("dialog")).getByLabelText("Importo"), "10");
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Deposita" }));
    await waitFor(() => expect(cashRequests).toHaveLength(3));
    expect(cashRequests[2].key).not.toBe(firstKey);
    act(() => releaseDeposit!());
  });

  it("requires a reversal reason, shown next to the field", async () => {
    const user = await openTab("Movimenti");
    const table = await screen.findByRole("table", { name: "Movimenti" });
    await within(table).findByText("versamento");
    // TRADE and already reversed rows have no Storna action
    expect(within(table).getAllByRole("button", { name: "Storna" })).toHaveLength(1);
    await user.click(within(table).getByRole("button", { name: "Storna" }));
    const dialog = screen.getByRole("dialog");
    const reason = within(dialog).getByLabelText("Motivazione");
    await user.type(reason, "   ");
    await user.click(within(dialog).getByRole("button", { name: "Storna" }));
    const message = within(dialog).getByText("La motivazione è obbligatoria");
    expect(reason).toHaveAttribute("aria-invalid", "true");
    expect(reason.getAttribute("aria-describedby")).toBe(message.id);
    expect(reverseRequests).toHaveLength(0);

    reverseError = "lo storno non è consentito";
    await user.type(reason, " errore di battitura ");
    await user.click(within(dialog).getByRole("button", { name: "Storna" }));
    expect(await within(dialog).findByText("lo storno non è consentito")).toBeInTheDocument();
    expect(reverseRequests).toEqual([{ id: "j9", body: { reason: "errore di battitura" } }]);
  });

  it("loads the next page of movements with the cursor", async () => {
    const user = await openTab("Movimenti");
    const table = await screen.findByRole("table", { name: "Movimenti" });
    await within(table).findByText("versamento");
    expect(within(table).queryByText("Prelievo")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Carica altri" }));
    expect(await within(table).findByText("Prelievo")).toBeInTheDocument();
    expect(txRequests.at(-1)!.searchParams.get("before_seq")).toBe("7");
    expect(screen.queryByRole("button", { name: "Carica altri" })).not.toBeInTheDocument();
  });

  it("exports CSV through a link to the transactions.csv endpoint with the filters", async () => {
    const user = await openTab("Movimenti");
    const link = await screen.findByRole("link", { name: "Esporta CSV" });
    expect(link).toHaveAttribute("href", "/api/portfolios/p1/transactions.csv");
    await user.selectOptions(screen.getByLabelText("Tipo"), "DEPOSIT");
    expect(link).toHaveAttribute("href", "/api/portfolios/p1/transactions.csv?type=DEPOSIT");
    await waitFor(() => expect(txRequests.at(-1)!.searchParams.get("type")).toBe("DEPOSIT"));
  });

  it("updates the position price from live quotes and colours the unrealized P&L", async () => {
    await openTab("Posizioni");
    const price = await screen.findByTestId("price-AAPL");
    expect(price).toHaveTextContent("190,50 $");
    expect(screen.getByText("-105,00 $")).toHaveClass("loss");
    await waitFor(() => expect(ws.subscribed).toContainEqual(["AAPL"]));
    emitQuote({ symbol: "AAPL", last: "191.25", bid: "191.2", ask: "191.3" });
    expect(price).toHaveTextContent("191,25 $");
    emitQuote({ symbol: "MSFT", last: "1" });
    expect(price).toHaveTextContent("191,25 $");
  });

  it("filters orders by status and cancels an open order", async () => {
    const user = await openTab("Ordini");
    const table = await screen.findByRole("table", { name: "Ordini" });
    await within(table).findByText("MSFT");
    expect(orderStatuses).toEqual(["open"]);
    await user.selectOptions(screen.getByLabelText("Stato"), "");
    await waitFor(() => expect(orderStatuses).toEqual(["open", null]));
    await user.click(within(table).getByRole("button", { name: "Annulla" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Annulla ordine" }));
    await waitFor(() => expect(canceled).toEqual(["o1"]));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("requires the portfolio name to reset and shows the server refusal", async () => {
    const user = await openTab("Operazioni");
    await user.click(screen.getByRole("button", { name: "Reset portafoglio" }));
    const dialog = screen.getByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: "Esegui reset" });
    await user.type(within(dialog).getByLabelText("Nuovo capitale"), "5000");
    await user.type(within(dialog).getByLabelText("Motivazione"), "ripartenza");
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Scrivi «Alfa» per confermare"), "Alfa");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(await within(dialog).findByText("posizioni aperte su mercati chiusi")).toBeInTheDocument();
    expect(resetRequests).toEqual([
      { key: expect.stringMatching(/^[0-9a-f-]{36}$/), body: { new_cash: "5000", currency: "EUR", reason: "ripartenza" } },
    ]);
  });

  it("shows risk details and unlocks a reduce_only portfolio", async () => {
    current = { ...portfolio, status: "reduce_only", reduce_only_reason: "perdita giornaliera" };
    const user = await openTab("Rischio");
    expect(await screen.findByText("perdita giornaliera")).toBeInTheDocument();
    expect(await screen.findByText("Prudente")).toBeInTheDocument();
    // equity 10500.25 - maintenance 750
    expect(await screen.findByText("9.750,25 €")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Sblocca" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Sblocca" }));
    await waitFor(() => expect(patches).toEqual([{ status: "active" }]));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Sblocca" })).not.toBeInTheDocument());
  });

  it("has no Sblocca button on an active portfolio", async () => {
    await openTab("Rischio");
    await screen.findByText("Prudente");
    expect(screen.queryByRole("button", { name: "Sblocca" })).not.toBeInTheDocument();
  });
});
