import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { toast } from "sonner";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { QueryClient } from "@tanstack/react-query";
import type { Composition, FeeRule, LiveLink, Order, Portfolio, Position, Transaction, Valuation } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import { haltFixture } from "@/test/halts";

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
  mode: "simulazione",
  broker_kind: null,
  mode_label: "SMTrading (simulato)",
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
let positionsMark = "190.5";
let queryClient: QueryClient | null = null;
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
let unarchived = 0;
const liveLinkFixture: LiveLink = {
  id: "l1",
  name: "Alpaca vero",
  kind: "alpaca_live",
  live_state: "armed",
  armed_at: "2026-11-14T10:00:00Z",
  armed_by: "admin@example.com",
  disarmed_at: null,
  disarmed_reason: null,
  key_masked: "••••0001",
  account_masked: "••••1234",
  snapshot: null,
  last_test_at: null,
  health_failures: 0,
  portfolios: [{ id: "p1", name: "Alfa", status: "active", base_currency: "EUR", quota: "1000.00", positions_count: 0 }],
  currency: "EUR",
  broker_cash: "1000",
  quotas_total: "1000.00",
  unassigned: "0.00",
};
let liveLinks: LiveLink[] = [liveLinkFixture];
const feeRuleFixture: FeeRule = {
  id: "fr", name: "", scope: "portfolio", scope_ref: null, asset_class: null, side: "both", currency: "EUR", fixed: "0", pct: "0",
  min_fee: null, max_fee: null, tiers: null, fx_pct: "0", priority: 0, active: true, created_at: "2026-11-01T10:00:00Z",
};
let feeRules: FeeRule[] = [];
let presetApplies: { id: string; body: unknown }[] = [];

const compositionFixture: Composition = {
  as_of: "2026-11-16T21:00:00Z",
  equity_eur: "880.22",
  cash_eur: "380.22",
  positions: [
    {
      symbol: "AMZN", name: "Amazon.com, Inc.", asset_class: "us_equity", market: "NASDAQ", currency: "USD",
      qty: "0.51", avg_price: "232.1", last: "222.4", value_eur: "97.6", weight_pct: "11.09",
      pnl_eur: "-4.26", pnl_pct: "-4.18", stale: false,
    },
    {
      symbol: "ENI.MI", name: "Eni S.p.A.", asset_class: "eu_equity", market: "MTA", currency: "EUR",
      qty: "30", avg_price: "13.1", last: "13.4", value_eur: "402.4", weight_pct: "45.72",
      pnl_eur: "9", pnl_pct: "2.29", stale: false,
    },
  ],
  by_asset_class: [
    { key: "eu_equity", label: "Azioni EU", value_eur: "402.4", weight_pct: "45.72" },
    { key: "cash", label: "Contante", value_eur: "380.22", weight_pct: "43.19" },
    { key: "us_equity", label: "Azioni USA", value_eur: "97.6", weight_pct: "11.09" },
  ],
  by_currency: [
    { key: "EUR", label: "EUR", value_eur: "782.62", weight_pct: "88.91" },
    { key: "USD", label: "USD", value_eur: "97.6", weight_pct: "11.09" },
  ],
  unpriced_symbols: [],
};
let composition: Composition = compositionFixture;

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/portfolios/p1", () => HttpResponse.json(current)),
  http.get("*/api/live-links", () => HttpResponse.json(liveLinks)),
  http.get("*/api/fee-rules", () => HttpResponse.json(feeRules)),
  http.post("*/api/portfolios/:id/fee-rule/apply-preset", async ({ request, params }) => {
    presetApplies.push({ id: String(params.id), body: await request.json() });
    const base = { ...feeRuleFixture, scope_ref: String(params.id), fx_pct: "0.75" };
    const rules: FeeRule[] = [
      { ...base, id: "fr2", name: "Alfa – eToro" },
      { ...base, id: "fr3", name: "Alfa – eToro · Crypto", asset_class: "crypto", pct: "1" },
    ];
    const mine = (r: FeeRule) => r.scope === "portfolio" && r.scope_ref === params.id;
    const deactivated = feeRules.filter((r) => mine(r) && r.active).map((r) => ({ id: r.id, name: r.name }));
    feeRules = [...feeRules.map((r) => (mine(r) ? { ...r, active: false } : r)), ...rules];
    return HttpResponse.json({ rules, created: ["fr2", "fr3"], deactivated, preset: {} });
  }),
  http.patch("*/api/portfolios/p1", async ({ request }) => {
    patches.push(await request.json());
    current = { ...current, status: "active", reduce_only_reason: null };
    return HttpResponse.json(current);
  }),
  http.get("*/api/portfolios/p1/valuation", () => HttpResponse.json(valuation)),
  http.get("*/api/portfolios/p1/composition", () => HttpResponse.json(composition)),
  http.get("*/api/portfolios/p1/positions", () => HttpResponse.json(positions.map((p) => ({ ...p, mark: positionsMark })))),
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
        description: "Uso normale con cautela.",
      },
      {
        id: "r2",
        name: "Libero",
        max_order_notional_eur: null,
        max_position_pct: null,
        max_daily_loss_pct: null,
        max_daily_loss_eur: null,
        allowed_symbols: [],
        allowed_asset_classes: [],
        allowed_markets: [],
        short_allowed: true,
        description: "Nessun limite.",
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
  http.post("*/api/portfolios/p1/unarchive", () => {
    unarchived += 1;
    current = { ...current, status: "active", archived_at: null, archived_by: null };
    return HttpResponse.json(current);
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
  composition = compositionFixture;
  positionsMark = "190.5";
  queryClient = null;
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
  unarchived = 0;
  liveLinks = [liveLinkFixture];
  feeRules = [];
  presetApplies = [];
  ws.subscribed.length = 0;
});
afterAll(() => server.close());

async function openTab(name: string) {
  const user = userEvent.setup();
  queryClient = renderApp("/portafogli/p1").queryClient;
  await user.click(await screen.findByRole("tab", { name }));
  return user;
}

describe("PortfolioDetail", () => {
  it("shows an archived portfolio read-only and restores it after confirmation", async () => {
    current = { ...portfolio, status: "archived", archived_at: "2026-11-13T10:00:00Z", archived_by: "admin@example.com" };
    const user = userEvent.setup();
    renderApp("/portafogli/p1");
    const banner = await screen.findByRole("status", { name: "Portafoglio archiviato" });
    expect(within(banner).getByText(/sola lettura/)).toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Operazioni" })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "Impostazioni" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Movimenti" }));
    const table = await screen.findByRole("table", { name: "Movimenti" });
    await within(table).findByText("versamento");
    expect(within(table).queryByRole("button", { name: "Storna" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Ordini" }));
    await screen.findByText("MSFT");
    expect(screen.queryByRole("button", { name: "Annulla" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Nuovo ordine" })).not.toBeInTheDocument();

    await user.click(within(banner).getByRole("button", { name: "Ripristina" }));
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Ripristina" }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "Portafoglio archiviato" })).not.toBeInTheDocument());
    expect(unarchived).toBe(1);
    expect(screen.getByRole("tab", { name: "Operazioni" })).toBeInTheDocument();
  });

  it("shows KPIs and cash per currency in the overview", async () => {
    renderApp("/portafogli/p1");
    expect(await screen.findByRole("heading", { name: "Alfa" })).toBeInTheDocument();
    expect(screen.getByText("10.500,25 €")).toBeInTheDocument();
    expect(await screen.findByText("+5,00%")).toBeInTheDocument();
    const cash = await screen.findByRole("list", { name: "Cash per valuta" });
    expect(within(cash).getByText("8.000,50 €")).toBeInTheDocument();
    expect(within(cash).getByText("1.200,00 $")).toBeInTheDocument();
  });

  it("shows the composition: donut with cash slice, groups and positions with weight and P&L", async () => {
    renderApp("/portafogli/p1");
    const card = await screen.findByRole("region", { name: "Composizione" });
    const legend = await within(card).findByRole("list", { name: "Legenda composizione" });
    const items = within(legend).getAllByRole("listitem").map((li) => li.textContent);
    expect(items).toEqual(["AMZN11,1%", "ENI.MI45,7%", "Contante43,2%"]);
    expect(screen.getByTestId("slice-cash")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Composizione per titolo" })).toHaveTextContent("880 €");
    const byClass = screen.getByRole("list", { name: "Per mercato/tipo" });
    expect(within(byClass).getByText("Azioni EU")).toBeInTheDocument();
    expect(within(byClass).getByText("402,40 €")).toBeInTheDocument();
    const byCcy = screen.getByRole("list", { name: "Per valuta" });
    expect(within(byCcy).getByText("USD")).toBeInTheDocument();
    expect(within(byCcy).getByText("782,62 €")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Composizione per titolo" });
    const amzn = within(table).getByText("AMZN").closest("tr")!;
    expect(within(amzn).getByText("222,40 $")).toBeInTheDocument();
    expect(within(amzn).getByText("97,60 €")).toBeInTheDocument();
    expect(within(amzn).getByText("-4,26 €")).toHaveClass("text-loss");
    expect(within(amzn).getByText("11,1%")).toBeInTheDocument();
  });

  it("shows «Solo contante» when the portfolio holds no position", async () => {
    composition = {
      ...compositionFixture, equity_eur: "500", cash_eur: "500", positions: [],
      by_asset_class: [{ key: "cash", label: "Contante", value_eur: "500", weight_pct: "100" }],
      by_currency: [{ key: "EUR", label: "EUR", value_eur: "500", weight_pct: "100" }],
    };
    renderApp("/portafogli/p1");
    expect(await screen.findByText("Solo contante")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Legenda composizione" })).not.toBeInTheDocument();
  });

  it("shows a position without quote with dashes, never zero", async () => {
    composition = {
      ...compositionFixture,
      equity_eur: null,
      positions: [
        compositionFixture.positions[1],
        { ...compositionFixture.positions[0], last: null, value_eur: null, weight_pct: null, pnl_eur: null, pnl_pct: null },
      ],
      unpriced_symbols: ["AMZN"],
    };
    renderApp("/portafogli/p1");
    const table = await screen.findByRole("table", { name: "Composizione per titolo" });
    const amzn = within(table).getByText("AMZN").closest("tr")!;
    expect(within(amzn).getByText("nessuna quotazione")).toBeInTheDocument();
    expect(within(amzn).getAllByText("—")).toHaveLength(4);
    expect(within(amzn).queryByText(/^0,00/)).not.toBeInTheDocument();
    expect(screen.getByText(/Senza quotazione: AMZN/)).toBeInTheDocument();
    expect(screen.queryByTestId("slice-AMZN")).not.toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Composizione per titolo" })).toHaveTextContent("—");
  });

  it("disables Deposita while the request is pending and reuses the key on retry", async () => {
    depositFails = true;
    const user = await openTab("Operazioni");
    await user.click(screen.getByRole("button", { name: "Nuovo deposito" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Importo"), "1.500,50");
    expect(within(dialog).getByText("= 1.500,50 €")).toBeInTheDocument();
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
    await user.type(reason, "!");
    expect(within(dialog).queryByText("lo storno non è consentito")).not.toBeInTheDocument();
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

  it("drops the live price when the positions are refetched", async () => {
    const user = await openTab("Posizioni");
    const price = await screen.findByTestId("price-AAPL");
    emitQuote({ symbol: "AAPL", last: "191.25" });
    expect(price).toHaveTextContent("191,25 $");
    positionsMark = "189";
    await act(() => queryClient!.invalidateQueries({ queryKey: ["portfolios", "p1", "positions"] }));
    await waitFor(() => expect(screen.getByTestId("price-AAPL")).toHaveTextContent("189,00 $"));
    expect(user).toBeDefined();
  });

  it("shows the live deposit guard refusal next to the amount", async () => {
    const message =
      "Il conto del broker non ha ancora questo contante: fai prima il bonifico al broker, poi registra il deposito";
    server.use(
      http.post("*/api/portfolios/p1/deposit", () =>
        HttpResponse.json({ code: "INVALID_ORDER", message, details: { field: "amount" } }, { status: 400 }),
      ),
    );
    const user = await openTab("Operazioni");
    await user.click(screen.getByRole("button", { name: "Nuovo deposito" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Importo"), "200");
    await user.click(within(dialog).getByRole("button", { name: "Deposita" }));
    expect(await within(dialog).findByText(message)).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Importo")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("refuses an ambiguous amount in the deposit dialog", async () => {
    const user = await openTab("Operazioni");
    await user.click(screen.getByRole("button", { name: "Nuovo deposito" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Importo"), "10.000");
    expect(within(dialog).getByText("Usa la virgola per i decimali (es. 10.000,00)")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Deposita" }));
    expect(cashRequests).toEqual([]);
    await user.type(within(dialog).getByLabelText("Importo"), ",50");
    expect(within(dialog).getByText("= 10.000,50 €")).toBeInTheDocument();
  });

  it("cancel button calls cancel endpoint (orders filtered by status)", async () => {
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

  it("a viewer sees the orders without Nuovo ordine", async () => {
    server.use(
      http.get("*/api/auth/me", () =>
        HttpResponse.json({ email: "vera@example.com", totp_enabled: false, role: "viewer", must_change_password: false }),
      ),
    );
    await openTab("Ordini");
    await screen.findByText("MSFT");
    expect(screen.queryByRole("button", { name: "Nuovo ordine" })).not.toBeInTheDocument();
  });

  it("opens the order form from Nuovo ordine and sends a previewed order", async () => {
    const previews: unknown[] = [];
    const placed: unknown[] = [];
    server.use(
      http.get("*/api/instruments", () =>
        HttpResponse.json({
          items: [{ symbol: "MSFT", name: "Microsoft", asset_class: "us_equity", market: "US", currency: "USD", lot_size: "1", tick_size: "0.01", shortable: true, active: true, provider_symbols: {}, source: "catalog", added_by: null }],
          total: 1,
          offset: 0,
          limit: 10,
        }),
      ),
      http.post("*/api/portfolios/p1/orders/preview", async ({ request }) => {
        previews.push(await request.json());
        return HttpResponse.json({
          ok: true,
          code: null,
          message: null,
          estimated_price: "400",
          estimated_notional_eur: "370",
          estimated_fee: { rule_id: "f1", amount: "1", currency: "EUR" },
          estimated_fx_fee_eur: "0",
          buying_power_after_eur: "16629",
          real: false,
          currency: "EUR",
        });
      }),
      http.post("*/api/portfolios/p1/orders", async ({ request }) => {
        placed.push(await request.json());
        return HttpResponse.json({ ...openOrder, id: "o9" });
      }),
    );
    const user = await openTab("Ordini");
    await user.click(await screen.findByRole("button", { name: "Nuovo ordine" }));
    const dialog = screen.getByRole("dialog", { name: "Nuovo ordine" });
    await user.type(within(dialog).getByLabelText("Simbolo"), "ms");
    await user.click(await within(dialog).findByRole("button", { name: /MSFT/ }));
    await user.type(within(dialog).getByLabelText("Quantità"), "1");
    expect(within(dialog).getByRole("button", { name: "Invia" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(previews).toEqual([{ symbol: "MSFT", side: "buy", type: "market", tif: "gtc", qty: "1" }]);
    expect(placed).toEqual(previews);
  });

  it("a real order sends the TOTP, which is not kept in the mutation cache", async () => {
    current = { ...portfolio, mode: "reale", live_state: "armed", broker_link_id: "l1" };
    const placed: unknown[] = [];
    server.use(
      http.get("*/api/instruments", () =>
        HttpResponse.json({
          items: [{ symbol: "MSFT", name: "Microsoft", asset_class: "us_equity", market: "US", currency: "USD", lot_size: "1", tick_size: "0.01", shortable: true, active: true, provider_symbols: {}, source: "catalog", added_by: null }],
          total: 1,
          offset: 0,
          limit: 10,
        }),
      ),
      http.post("*/api/portfolios/p1/orders/preview", () =>
        HttpResponse.json({
          ok: true,
          code: null,
          message: null,
          estimated_price: "400",
          estimated_notional_eur: "370",
          estimated_fee: { rule_id: "f1", amount: "1", currency: "EUR" },
          estimated_fx_fee_eur: "0",
          buying_power_after_eur: "16629",
          real: true,
          currency: "EUR",
        }),
      ),
      http.post("*/api/portfolios/p1/orders", async ({ request }) => {
        placed.push(await request.json());
        return HttpResponse.json({ ...openOrder, id: "o9" });
      }),
    );
    const user = await openTab("Ordini");
    await user.click(await screen.findByRole("button", { name: "Nuovo ordine" }));
    const dialog = screen.getByRole("dialog", { name: "Nuovo ordine" });
    await user.type(within(dialog).getByLabelText("Simbolo"), "ms");
    await user.click(await within(dialog).findByRole("button", { name: /MSFT/ }));
    await user.type(within(dialog).getByLabelText("Quantità"), "1");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    const confirm = await screen.findByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    await user.type(within(confirm).getByLabelText("Codice TOTP"), "246810");
    await user.click(within(confirm).getByRole("button", { name: "Invia ordine reale" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(placed).toEqual([{ symbol: "MSFT", side: "buy", type: "market", tif: "gtc", qty: "1", totp: "246810" }]);
    await waitFor(() =>
      expect(JSON.stringify(queryClient!.getMutationCache().getAll().map((m) => m.state.variables))).not.toContain("246810"),
    );
  });

  it("shows the broker state of routed orders and marks the simulated ones", async () => {
    current = { ...portfolio, broker_link_id: "b1" };
    server.use(
      http.get("*/api/portfolios/p1/orders", () =>
        HttpResponse.json([
          { ...openOrder, broker_link_id: "b1", broker_replicated: true, broker_status: "cancel_requested", broker_order_id: "ALP-42" },
          { ...openOrder, id: "o2", symbol: "AAPL", broker_link_id: null, broker_replicated: false, broker_status: null, broker_order_id: null },
        ]),
      ),
    );
    await openTab("Ordini");
    const table = await screen.findByRole("table", { name: "Ordini" });
    const routed = (await within(table).findByText("MSFT")).closest("tr") as HTMLElement;
    expect(within(routed).getByText("Broker")).toBeInTheDocument();
    expect(within(routed).getByText("Annullamento richiesto")).toBeInTheDocument();
    expect(within(routed).getByText("ALP-42")).toBeInTheDocument();
    const simulated = within(table).getByText("AAPL").closest("tr") as HTMLElement;
    expect(within(simulated).getByText("Solo simulato")).toBeInTheDocument();
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

  it("resume and flatten ask for totp", async () => {
    current = { ...portfolio, mode: "reale", live_state: "armed", broker_kind: "t212_live", mode_label: "🔴 Trading 212 · reale", broker_link_id: "l1", status: "reduce_only", reduce_only_reason: "live_reconcile" };
    const resumes: unknown[] = [];
    const flattens: unknown[] = [];
    server.use(
      http.post("*/api/portfolios/p1/live-resume", async ({ request }) => {
        resumes.push(await request.json());
        current = { ...current, status: "active", reduce_only_reason: null };
        return HttpResponse.json(current);
      }),
      http.post("*/api/portfolios/p1/live-flatten", async ({ request }) => {
        flattens.push(await request.json());
        return HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP già usato", details: { field: "totp" } }, { status: 403 });
      }),
    );
    const user = userEvent.setup();
    renderApp("/portafogli/p1");
    const panel = await screen.findByRole("region", { name: "Denaro reale" });
    expect(screen.getByLabelText(/^Modalità: 🔴 Trading 212 · reale\./).textContent).toBe("🔴 Trading 212 · reale");

    await user.click(within(panel).getByRole("button", { name: "Ho verificato, riprendi" }));
    let dialog = await screen.findByRole("dialog", { name: "Riprendere il portafoglio reale?" });
    const resume = within(dialog).getByRole("button", { name: "Riprendi" });
    expect(resume).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "654321");
    await user.click(resume);
    await waitFor(() => expect(resumes).toEqual([{ totp: "654321" }]));
    await waitFor(() => expect(within(panel).queryByRole("button", { name: "Ho verificato, riprendi" })).not.toBeInTheDocument());

    await user.click(within(panel).getByRole("button", { name: "Chiudi tutte le posizioni reali" }));
    dialog = await screen.findByRole("dialog", { name: "Chiudere tutte le posizioni reali?" });
    const close = within(dialog).getByRole("button", { name: "Chiudi tutte le posizioni" });
    expect(close).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "111222");
    await user.click(close);
    await waitFor(() => expect(flattens).toEqual([{ totp: "111222" }]));
    expect(await within(dialog).findByText("codice TOTP già usato")).toBeInTheDocument();
  });

  it("a real portfolio suspended by a live anomaly has no Sblocca, and trailing stops carry the note", async () => {
    current = { ...portfolio, mode: "paper", live_state: "dry_run", broker_kind: "alpaca_live", mode_label: "🔴 Alpaca · reale · prova a secco", broker_link_id: "l1", status: "reduce_only", reduce_only_reason: "live_order_unknown" };
    server.use(http.get("*/api/portfolios/p1/orders", () => HttpResponse.json([{ ...openOrder, type: "trailing_stop" }])));
    const user = await openTab("Rischio");
    expect(screen.getByLabelText(/^Modalità: 🔴 Alpaca · reale · prova a secco\./).textContent).toBe("🔴 Alpaca · reale · prova a secco");
    expect(await screen.findByText("Anomalia del denaro reale: esito sconosciuto di un ordine", { selector: "dd" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sblocca" })).not.toBeInTheDocument();
    const panel = screen.getByRole("region", { name: "Denaro reale" });
    expect(within(panel).getByRole("button", { name: "Ho verificato, riprendi" })).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Chiudi tutte le posizioni reali" })).not.toBeInTheDocument(); // only when armed
    await user.click(screen.getByRole("tab", { name: "Ordini" }));
    expect(await screen.findByText("stop gestito da SMTrading, non dal broker")).toBeInTheDocument();
  });

  it("disarms from the portfolio in one click, without TOTP", async () => {
    current = { ...portfolio, mode: "paper", live_state: "dry_run", broker_link_id: "l1" };
    liveLinks = [{ ...liveLinkFixture, live_state: "dry_run" }];
    const warn = vi.spyOn(toast, "warning");
    const disarms: string[] = [];
    server.use(
      http.post("*/api/live-links/l1/disarm", async ({ request }) => {
        disarms.push(await request.text());
        current = { ...current, live_state: "configured" };
        liveLinks = [{ ...liveLinkFixture, live_state: "configured", armed_at: null, disarmed_at: "2026-11-14T11:00:00Z" }];
        return HttpResponse.json({ ...liveLinks[0], orders: null, settled: false });
      }),
    );
    const user = userEvent.setup();
    renderApp("/portafogli/p1");
    const panel = await screen.findByRole("region", { name: "Denaro reale" });
    await user.click(within(panel).getByRole("button", { name: "Disattiva denaro reale" }));
    await waitFor(() => expect(disarms).toHaveLength(1));
    expect(disarms[0]).not.toMatch(/totp/i);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await within(panel).findByRole("alert")).toHaveTextContent("Disattivato, ordini da sistemare: riprova");
    expect(await within(panel).findByRole("button", { name: "Ripeti disattivazione / sistema ordini" })).toBeInTheDocument();
    expect(warn).toHaveBeenCalledWith("Disattivato, ordini da sistemare: riprova");
    const success = vi.spyOn(toast, "success");
    server.use(
      http.post("*/api/live-links/l1/disarm", () => HttpResponse.json({ ...liveLinks[0], orders: { canceled: 0 }, settled: true })),
    );
    await user.click(within(panel).getByRole("button", { name: "Ripeti disattivazione / sistema ordini" }));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Denaro reale disattivato su Alpaca vero")); // the link's name
    warn.mockRestore();
    success.mockRestore();
  });

  it("a locked real portfolio with a live reason resumes with TOTP, has no Sblocca, and its rows are marked", async () => {
    current = { ...portfolio, mode: "reale", live_state: "armed", broker_link_id: "l1", status: "locked", reduce_only_reason: "live_fuse" };
    const user = await openTab("Rischio");
    expect(await screen.findByText("Anomalia del denaro reale: fusibile: troppi ordini reali nell'ultima ora", { selector: "dd" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sblocca" })).not.toBeInTheDocument();
    const panel = screen.getByRole("region", { name: "Denaro reale" });
    expect(within(panel).getByRole("button", { name: "Ho verificato, riprendi" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Ordini" }));
    const orders = await screen.findByRole("table", { name: "Ordini" });
    expect(await within(orders).findByText("🔴 REALE")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Posizioni" }));
    const positions = await screen.findByRole("table", { name: "Posizioni" });
    expect(await within(positions).findByText("🔴 REALE")).toBeInTheDocument();
  });

  it("keeps Sblocca on a portfolio locked by a broker liquidation", async () => {
    current = { ...portfolio, status: "locked", reduce_only_reason: "broker_liquidation" };
    await openTab("Rischio");
    expect(await screen.findByText("Liquidazione da parte del broker", { selector: "dd" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sblocca" })).toBeInTheDocument();
  });

  it("labels the known reduce-only reasons in Italian", async () => {
    current = { ...portfolio, status: "reduce_only", reduce_only_reason: "daily_loss" };
    await openTab("Rischio");
    expect(await screen.findByText("Perdita giornaliera massima raggiunta", { selector: "dd" })).toBeInTheDocument();
  });

  it("has no Sblocca button on an active portfolio", async () => {
    await openTab("Rischio");
    await screen.findByText("Prudente");
    expect(screen.queryByRole("button", { name: "Sblocca" })).not.toBeInTheDocument();
  });

  it("sends the movement date filters as day bounds in the portfolio timezone", async () => {
    current = { ...portfolio, timezone: "America/New_York" };
    const user = await openTab("Movimenti");
    await screen.findByRole("link", { name: "Esporta CSV" });
    await user.type(screen.getByLabelText("Dal"), "2026-11-12");
    await user.type(screen.getByLabelText("Al"), "2026-11-13");
    await waitFor(() => expect(txRequests.at(-1)!.searchParams.get("to")).toBe("2026-11-13T23:59:59.999-05:00"));
    expect(txRequests.at(-1)!.searchParams.get("from")).toBe("2026-11-12T00:00:00-05:00");
  });

  it("shows the description of the selected risk profile, updating on change", async () => {
    const user = await openTab("Impostazioni");
    expect(await screen.findByTestId("risk-profile-description")).toHaveTextContent("Uso normale con cautela.");
    await user.selectOptions(screen.getByLabelText("Profilo di rischio"), "r2");
    expect(screen.getByTestId("risk-profile-description")).toHaveTextContent("Nessun limite.");
    await user.selectOptions(screen.getByLabelText("Profilo di rischio"), "");
    expect(screen.queryByTestId("risk-profile-description")).not.toBeInTheDocument();
  });

  it("edits the portfolio settings, sending only the changed fields", async () => {
    const user = await openTab("Impostazioni");
    const name = await screen.findByLabelText("Nome");
    expect(name).toHaveValue("Alfa");
    expect(screen.getByLabelText("Leva")).toHaveValue("2");
    const save = screen.getByRole("button", { name: "Salva impostazioni" });
    expect(save).toBeDisabled();
    await user.clear(name);
    await user.type(name, "Beta");
    await user.clear(screen.getByLabelText("Leva"));
    await user.type(screen.getByLabelText("Leva"), "1.000");
    await user.click(save);
    expect(screen.getByText("Usa la virgola per i decimali (es. 10.000,00)")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Leva"));
    await user.type(screen.getByLabelText("Leva"), "12");
    await user.click(save);
    expect(screen.getByText("Leva: da 1 a 10")).toBeInTheDocument();
    expect(patches).toEqual([]);
    await user.clear(screen.getByLabelText("Leva"));
    await user.type(screen.getByLabelText("Leva"), "2,5");
    await user.clear(screen.getByLabelText("Slippage (bps)"));
    await user.type(screen.getByLabelText("Slippage (bps)"), "10");
    await user.selectOptions(screen.getByLabelText("Profilo di rischio"), "");
    await user.selectOptions(screen.getByLabelText("Fuso orario"), "Asia/Tokyo");
    await user.click(save);
    await waitFor(() =>
      expect(patches).toEqual([{ name: "Beta", leverage: "2.5", slippage_bps: 10, risk_profile_id: null, timezone: "Asia/Tokyo" }]),
    );
  });

  it("edits the agents' code: uppercased, validated, emptied to clear it", async () => {
    current = { ...portfolio, code: "ALF" };
    const user = await openTab("Impostazioni");
    const code = await screen.findByLabelText("Codice (per gli agenti)");
    expect(code).toHaveValue("ALF");
    const save = screen.getByRole("button", { name: "Salva impostazioni" });
    await user.clear(code);
    await user.type(code, "a");
    await user.click(save);
    expect(screen.getByText("Da 2 a 6 lettere maiuscole o cifre")).toBeInTheDocument();
    expect(patches).toEqual([]);
    await user.type(code, "b2");
    await user.click(save);
    await waitFor(() => expect(patches).toEqual([{ code: "AB2" }]));
  });

  it("clears the agents' code with an empty field", async () => {
    current = { ...portfolio, code: "ALF" };
    const user = await openTab("Impostazioni");
    await user.clear(await screen.findByLabelText("Codice (per gli agenti)"));
    await user.click(screen.getByRole("button", { name: "Salva impostazioni" }));
    await waitFor(() => expect(patches).toEqual([{ code: null }]));
  });

  it("shows the portfolio's fee rules and applies the linked broker's fees after confirmation", async () => {
    current = { ...portfolio, broker_kind: "etoro_demo", broker_link_id: "b1", mode: "paper", mode_label: "eToro · prova" };
    feeRules = [
      { ...feeRuleFixture, id: "fr0", name: "Globale", scope: "global", scope_ref: null },
      { ...feeRuleFixture, id: "fr1", name: "Alfa – zero commissioni", scope: "portfolio", scope_ref: "p1" },
      { ...feeRuleFixture, id: "fr9", name: "Altro", scope: "portfolio", scope_ref: "p9" },
    ];
    const user = await openTab("Impostazioni");
    const list = await screen.findByRole("list", { name: "Regole del portafoglio" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Alfa – zero commissioni · tutte le classi · commissione 0% · cambio valuta 0%",
    ]);
    expect(presetApplies).toEqual([]);
    const card = screen.getByRole("list", { name: "Commissioni di eToro" });
    expect(within(card).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Azioni ed ETF (e altre classi): commissione 0% · cambio valuta 0,75% (guida giugno 2026; fonti discordanti 0,5–1%, da verificare)",
      "Crypto: commissione 1% · cambio valuta 0,75% (guida giugno 2026; fonti discordanti 0,5–1%, da verificare)",
    ]);
    await user.click(screen.getByRole("button", { name: "Usa le commissioni di eToro" }));
    const dialog = await screen.findByRole("dialog", { name: "Usare le commissioni di eToro?" });
    expect(dialog).toHaveTextContent("«Alfa – eToro», «Alfa – eToro · Crypto» diventano le regole del portafoglio");
    expect(within(dialog).getByRole("list", { name: "Commissioni di eToro" })).toHaveTextContent("Crypto: commissione 1%");
    expect(presetApplies).toEqual([]); // nothing before the confirmation
    await user.click(within(dialog).getByRole("button", { name: "Applica" }));
    await waitFor(() => expect(presetApplies).toEqual([{ id: "p1", body: { broker_kind: "etoro_demo" } }]));
    await waitFor(() =>
      expect(within(screen.getByRole("list", { name: "Regole del portafoglio" })).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
        "Alfa – eToro · tutte le classi · commissione 0% · cambio valuta 0,75%",
        "Alfa – eToro · Crypto · classe Crypto · commissione 1% · cambio valuta 0,75%",
      ]),
    );
  });

  it("offers no broker fees to a portfolio without a broker", async () => {
    const user = await openTab("Impostazioni");
    expect(await screen.findByText(/Nessuna regola del portafoglio/)).toBeInTheDocument();
    await user.click(screen.getByLabelText("Nome")); // the tab is rendered
    expect(screen.queryByRole("button", { name: /Usa le commissioni di/ })).not.toBeInTheDocument();
  });

  it("«Conteggiato nei totali»: saved on change", async () => {
    const user = await openTab("Impostazioni");
    const box = await screen.findByRole("checkbox", { name: "Conteggiato nei totali e negli obiettivi complessivi" });
    expect(box).toBeChecked();
    await user.click(box);
    await waitFor(() => expect(patches).toEqual([{ in_totals: false }]));
  });

  it("«Conteggiato nei totali»: the API's refusal is shown", async () => {
    current = { ...portfolio, in_totals: false };
    server.use(
      http.patch("*/api/portfolios/p1", async ({ request }) => {
        patches.push(await request.json());
        return HttpResponse.json(
          { code: "INVALID_ORDER", message: "«Alfa» ha già il suo piano «mensile»: lo coprirebbero due piani", details: { field: "in_totals" } },
          { status: 400 },
        );
      }),
    );
    const user = await openTab("Impostazioni");
    const box = await screen.findByRole("checkbox", { name: "Conteggiato nei totali e negli obiettivi complessivi" });
    expect(box).not.toBeChecked();
    await user.click(box);
    expect(await screen.findByText("«Alfa» ha già il suo piano «mensile»: lo coprirebbero due piani")).toBeInTheDocument();
    expect(patches).toEqual([{ in_totals: true }]);
  });

  it("shows a server refusal next to its field", async () => {
    server.use(
      http.patch("*/api/portfolios/p1", async ({ request }) => {
        patches.push(await request.json());
        return HttpResponse.json(
          { code: "INVALID_ORDER", message: "serve 0 < maintenance_margin_pct < warning_margin_pct <= 100", details: { field: "warning_margin_pct" } },
          { status: 400 },
        );
      }),
    );
    const user = await openTab("Impostazioni");
    const warn = await screen.findByLabelText("Soglia di avviso (%)");
    expect(warn).toHaveValue("75");
    await user.clear(warn);
    await user.type(warn, "40,5");
    await user.click(screen.getByRole("button", { name: "Salva impostazioni" }));
    expect(await screen.findByText("serve 0 < maintenance_margin_pct < warning_margin_pct <= 100")).toBeInTheDocument();
    expect(warn).toHaveAttribute("aria-invalid", "true");
    expect(patches).toEqual([{ warning_margin_pct: "40.5" }]);
  });
});

describe("PortfolioDetail halt banner", () => {
  it("shows only the halts covering this portfolio", async () => {
    server.use(
      http.get("*/api/halts", () =>
        HttpResponse.json({
          active: [
            haltFixture({ id: 2, scope: "portfolio", portfolio_id: "p1", portfolio: "Alfa", reason: "Troppa volatilità" }),
            haltFixture({ id: 3, scope: "portfolio", portfolio_id: "p9", portfolio: "Altro", reason: "Non qui" }),
          ],
          history: [],
        }),
      ),
    );
    renderApp("/portafogli/p1");
    const banner = await screen.findByRole("alert", { name: "Blocco aperture" });
    expect(banner).toHaveTextContent("Aperture sospese dal CEO su Alfa — Troppa volatilità");
    expect(screen.getAllByRole("alert", { name: "Blocco aperture" })).toHaveLength(1);
    expect(screen.queryByText(/Non qui/)).not.toBeInTheDocument();
  });
});
