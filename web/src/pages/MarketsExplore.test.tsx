import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import { MARKETS_PREFS_KEY } from "@/lib/marketsPrefs";
import { instances } from "@/test/echarts";
import { charts, markers } from "@/test/lightweightCharts";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const base = { market: "NASDAQ", currency: "USD", lot_size: "1", tick_size: "0.01", shortable: true, active: true, provider_symbols: {}, source: "catalog", added_by: null, created_at: null };
const AAPL = { ...base, symbol: "AAPL", name: "Apple Inc.", asset_class: "us_equity", sector: "Tecnologia", industry: "Consumer Electronics", indices: ["SP500", "NDX"] };
const MSFT = { ...base, symbol: "MSFT", name: "Microsoft", asset_class: "us_equity", sector: "Tecnologia", industry: "Software", indices: ["SP500", "NDX"] };
const ENI = { ...base, symbol: "ENI.MI", name: "Eni S.p.A.", asset_class: "eu_equity", market: "MTA", currency: "EUR", sector: "Energia", industry: "Oil & Gas Integrated", indices: ["FTSEMIB"] };
const BTC = { ...base, symbol: "BTC-USD", name: "Bitcoin", asset_class: "crypto", market: "CRYPTO", sector: "Crypto", industry: null, indices: [] };

let searches: string[] = [];
let lists: string[] = [];
let quotes: string[] = [];
let snapshots: string[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "vera@example.com", totp_enabled: false, role: "viewer", must_change_password: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/marketdata/stream-status", () => HttpResponse.json({ streams: {}, polling: [], available: true, tracked: [], tracked_sources: {} })),
  http.get("*/api/instruments/auto-add", () => HttpResponse.json({ on: true, daily_limit: 25 })),
  http.get("*/api/markets/status", () => HttpResponse.json([{ market: "NASDAQ", tradable: true, is_open: true, next_open: null, next_close: null }])),
  http.get("*/api/market-closures", () => HttpResponse.json([])),
  http.get("*/api/instruments/facets", () =>
    HttpResponse.json({
      sectors: [
        { sector: "Tecnologia", count: 99 },
        { sector: "Energia", count: 25 },
      ],
      indices: [
        { code: "SP500", label: "S&P 500", count: 503 },
        { code: "FTSEMIB", label: "FTSE MIB", count: 40 },
      ],
      universes: [
        { code: "SP500", label: "S&P 500" },
        { code: "DAX", label: "DAX 40" },
      ],
      sector_list: [],
      etf_types: [],
    }),
  ),
  http.get("*/api/instruments/search", ({ request }) => {
    const q = new URL(request.url).searchParams.get("q") ?? "";
    searches.push(q);
    const items = [
      { ...AAPL, score: 80, match: "name" },
      { ...ENI, score: 40, match: "name" },
      { ...BTC, score: 30, match: "name" },
    ].filter((h) => h.name.toLowerCase().includes(q.toLowerCase()) || q === "i");
    return HttpResponse.json({ query: q, items });
  }),
  http.get("*/api/instruments", ({ request }) => {
    lists.push(new URL(request.url).search);
    return HttpResponse.json({ items: [AAPL, MSFT], total: 2, offset: 0, limit: 50 });
  }),
  http.get("*/api/quotes", ({ request }) => {
    quotes.push(new URL(request.url).searchParams.get("symbols") ?? "");
    return HttpResponse.json({
      AAPL: { symbol: "AAPL", bid: "199", ask: "201", last: "200.5", ts: "2026-11-19T15:00:00Z", provider: "alpaca" },
      MSFT: { error: "PROVIDER_UNAVAILABLE" },
    });
  }),
  http.get("*/api/markets/day-changes", () =>
    HttpResponse.json({
      AAPL: { last: "200", prev_close: "190", change_pct: "5.26", ts: "2026-11-19T15:00:00Z", provider: "alpaca" },
      MSFT: { last: "400", prev_close: "410", change_pct: "-2.44", ts: "2026-11-19T15:00:00Z", provider: "alpaca" },
    }),
  ),
  http.get("*/api/markets/snapshot", ({ request }) => {
    const universe = new URL(request.url).searchParams.get("universe") ?? "";
    snapshots.push(universe);
    const m = (symbol: string, name: string, sector: string, change_pct: string) => ({ symbol, name, sector, currency: "USD", last: "10", change_pct });
    return HttpResponse.json({
      universe,
      label: universe === "DAX" ? "DAX 40" : "S&P 500",
      as_of: "2026-11-19T15:00:00Z",
      total: 4,
      covered: 3,
      sectors: [
        { sector: "Tecnologia", count: 2, avg_change_pct: "1.5", up: 2, down: 0, members: [m("NVDA", "Nvidia", "Tecnologia", "2"), m("AAPL", "Apple Inc.", "Tecnologia", "1")] },
        { sector: "Energia", count: 1, avg_change_pct: "-3.1", up: 0, down: 1, members: [m("XOM", "Exxon", "Energia", "-3.1")] },
      ],
      gainers: [m("NVDA", "Nvidia", "Tecnologia", "2"), m("AAPL", "Apple Inc.", "Tecnologia", "1")],
      losers: [m("XOM", "Exxon", "Energia", "-3.1")],
    });
  }),
  http.get("*/api/instruments/:symbol/overview", ({ params }) =>
    HttpResponse.json({
      instrument: params.symbol === "AAPL" ? AAPL : { ...ENI, symbol: String(params.symbol) },
      market_status: { market: "NASDAQ", is_open: true, next_open: null, next_close: null },
      positions: [{ portfolio_id: "p1", portfolio_name: "Prudente", qty: "2", avg_price: "180", opened_at: null }],
      open_orders: [{ id: "o1", portfolio_id: "p1", portfolio_name: "Prudente", side: "sell", type: "stop", qty: "2", notional: null, limit_price: null, stop_price: "150", role: "stop_loss", status: "pending", created_at: "2026-11-19T10:00:00Z" }],
      fills: [
        { id: "f2", order_id: "o3", portfolio_id: "p1", portfolio_name: "Prudente", side: "sell", qty: "1", price: "195", role: null, ts: "2026-11-19T15:30:00Z" },
        { id: "f1", order_id: "o2", portfolio_id: "p1", portfolio_name: "Prudente", side: "buy", qty: "3", price: "181", role: "entry", ts: "2026-11-17T15:30:00Z" },
      ],
      watchlists: [{ agent: "CEO", watchlist: "Tech" }],
      strategies: [{ ref: "STR-007", name: "RSI veloce", type: "rsi_reversion", timeframe: "1d", portfolios: ["Prudente"] }],
      decisions: [],
      signals: [{ ref: "STR-007", name: "RSI veloce", portfolio_name: "Prudente", indicator: "RSI 2", direction: "below", threshold: "10", distance: "-1", triggered_now: true, position: null, as_of: null }],
    }),
  ),
  http.get("*/api/instruments/:symbol/market", ({ request }) =>
    HttpResponse.json({
      symbol: "AAPL",
      timeframe: new URL(request.url).searchParams.get("timeframe"),
      quote: { symbol: "AAPL", bid: "199", ask: "201", last: "200.5", ts: "2026-11-19T15:00:00Z", provider: "alpaca" },
      day_change: { last: "200", prev_close: "190", change_pct: "5.26", ts: "2026-11-19T15:00:00Z", provider: "alpaca" },
      bars: [
        { ts: "2026-11-17T00:00:00Z", open: "178", high: "182", low: "177", close: "180", volume: "1000" },
        { ts: "2026-11-18T00:00:00Z", open: "180", high: "191", low: "179", close: "190", volume: "1500" },
        { ts: "2026-11-19T00:00:00Z", open: "190", high: "201", low: "188", close: "200", volume: "1200" },
      ],
      indicators: { close: "200", ts: "2026-11-19T00:00:00Z", rsi14: "72.31", sma20: "185", sma50: "170", atr14: "4" },
      news: [{ id: "n1", ts: "2026-11-19T12:00:00Z", headline: "Apple presenta qualcosa", url: "https://example.com/a", source: "feed" }],
      next_earnings: { date: "2027-01-28", data: {} },
      next_dividend: null,
      errors: {},
      as_of: "2026-11-19T15:00:00Z",
    }),
  ),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  localStorage.clear();
  searches = [];
  lists = [];
  quotes = [];
  snapshots = [];
});
afterAll(() => server.close());

describe("Markets: search, sheet, filters, sector map", () => {
  it("searches instantly, groups by class, highlights and opens the sheet with the keyboard", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    const box = await screen.findByRole("combobox", { name: "Ricerca istantanea" });
    await user.type(box, "i");
    const list = await screen.findByRole("listbox", { name: "Risultati della ricerca" });
    const groups = await within(list).findAllByRole("group");
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual(["Azioni USA", "Azioni EU", "Crypto"]);
    expect(within(list).getAllByRole("option")).toHaveLength(3);
    // the matching text is highlighted
    expect(within(groups[0]).getAllByText("I", { selector: "mark" }).length).toBeGreaterThan(0);
    expect(searches).toContain("i");
    await user.keyboard("{ArrowDown}");
    expect(within(list).getAllByRole("option")[1]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowUp}{Enter}");
    const sheet = await screen.findByRole("dialog");
    expect(within(sheet).getByText("AAPL")).toBeInTheDocument();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("debounces the search: one request for a quickly typed word", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    await user.type(await screen.findByRole("combobox", { name: "Ricerca istantanea" }), "apple");
    await within(await screen.findByRole("listbox")).findByRole("option");
    expect(searches).toEqual(["apple"]);
  });

  it("shows the sheet: price, change, indicators, events, ours, strategies, news", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    const table = await screen.findByRole("table", { name: "Strumenti" });
    await user.click(await within(table).findByRole("button", { name: "Scheda AAPL" }));
    const sheet = await screen.findByRole("dialog");
    expect(await within(sheet).findByText("200,50 $")).toBeInTheDocument();
    expect(within(sheet).getByText("+5,26%")).toBeInTheDocument();
    // the candlesticks: volume pane, our average price and stop as lines, our fills as markers
    expect(await within(sheet).findByTestId("price-chart")).toBeInTheDocument();
    await waitFor(() => expect(charts.at(-1)?.series.length).toBeGreaterThan(0));
    const c = charts.at(-1)!;
    const candles = c.series.find((x) => x.type === "Candlestick")!;
    expect(candles.data).toHaveLength(3);
    expect(candles.data[2]).toEqual({ time: Date.parse("2026-11-19T00:00:00Z") / 1000, open: 190, high: 201, low: 188, close: 200 });
    expect(candles.priceLines.map((l) => [l.options.title, l.options.price])).toEqual([
      ["Prezzo medio", 180],
      ["Stop 150,00 $", 150],
    ]);
    const vol = c.series.find((x) => x.type === "Histogram")!;
    expect(vol.pane).toBe(1);
    expect(c.paneStretch[1] / c.paneStretch[0]).toBeLessThan(0.3); // the volumes stay a small pane
    expect(markers.at(-1)!.markers).toMatchObject([
      { time: Date.parse("2026-11-17T00:00:00Z") / 1000, position: "belowBar", shape: "arrowUp", text: "Acquisto 3" },
      { time: Date.parse("2026-11-19T00:00:00Z") / 1000, position: "aboveBar", shape: "arrowDown", text: "Vendita 1" },
    ]);
    expect(within(sheet).getByTestId("price-legend")).toHaveTextContent("Chius. 200,00");
    // the moving averages on request (3 bars: too few for either, so nothing is drawn)
    await user.click(within(sheet).getByRole("button", { name: "SMA 20" }));
    expect(within(sheet).getByRole("button", { name: "SMA 20" })).toHaveAttribute("aria-pressed", "true");
    expect(c.series.filter((x) => x.type === "Line")).toHaveLength(0);
    expect(within(sheet).getByText("72,3")).toBeInTheDocument();
    expect(within(sheet).getByText("ipercomprato")).toBeInTheDocument();
    expect(within(sheet).getAllByText("prezzo sopra")).toHaveLength(2);
    expect(within(sheet).getByText("Trimestrale: 28/01/2027")).toBeInTheDocument();
    expect(within(sheet).getByText("Tecnologia")).toBeInTheDocument();
    expect(within(sheet).getByText("NASDAQ-100")).toBeInTheDocument();
    const ours = within(sheet).getByRole("region", { name: "Le nostre posizioni" });
    expect(ours).toHaveTextContent("Prudente: 2 a 180,00 $");
    expect(within(sheet).getByRole("region", { name: "Ordini aperti" })).toHaveTextContent("stop 150,00 $");
    const strategies = within(sheet).getByRole("region", { name: "Strategie" });
    expect(strategies).toHaveTextContent("STR-007");
    expect(within(strategies).getByText("segnale attivo")).toBeInTheDocument();
    expect(within(sheet).getByText("CEO: Tech")).toBeInTheDocument();
    expect(within(sheet).getByRole("link", { name: /Apple presenta qualcosa/ })).toHaveAttribute("href", "https://example.com/a");
    await user.click(within(sheet).getByRole("button", { name: "Orario" }));
    expect(within(sheet).getByRole("button", { name: "Orario" })).toHaveAttribute("aria-pressed", "true");
  });

  it("shows the page's prices from one batch quote request and the day change", async () => {
    renderApp("/mercati");
    const table = await screen.findByRole("table", { name: "Strumenti" });
    const aapl = (await within(table).findByRole("button", { name: "Scheda AAPL" })).closest("tr")!;
    expect(await within(aapl).findByText("200,5")).toBeInTheDocument();
    expect(await within(aapl).findByText("+5,26%")).toBeInTheDocument();
    const msft = within(table).getByRole("button", { name: "Scheda MSFT" }).closest("tr")!;
    expect(await within(msft).findByText("400")).toBeInTheDocument(); // quote failed: the day change's price
    expect(within(msft).getByText("-2,44%")).toBeInTheDocument();
    expect(quotes).toEqual(["AAPL,MSFT"]);
  });

  it("filters by sector, index and quick filter, sorts, and remembers it all", async () => {
    const user = userEvent.setup();
    const first = renderApp("/mercati");
    await screen.findByRole("table", { name: "Strumenti" });
    await user.selectOptions(await screen.findByLabelText("Filtra per settore"), "Energia");
    await user.selectOptions(screen.getByLabelText("Filtra per indice"), "FTSEMIB");
    await user.click(screen.getByRole("button", { name: "In portafoglio" }));
    await waitFor(() => expect(lists.at(-1)).toContain("quick=portfolio"));
    expect(lists.at(-1)).toContain("sector=Energia");
    expect(lists.at(-1)).toContain("index=FTSEMIB");
    await user.click(screen.getByRole("button", { name: "Nome" }));
    await waitFor(() => expect(lists.at(-1)).toContain("sort=name&dir=asc"));
    await user.click(screen.getByRole("button", { name: "Nome" }));
    await waitFor(() => expect(lists.at(-1)).toContain("sort=name&dir=desc"));
    // sorting by the day change happens within the page: MSFT (-2,44%) first ascending
    await user.click(screen.getByRole("button", { name: "Var." }));
    await user.click(screen.getByRole("button", { name: "Var." }));
    const table = screen.getByRole("table", { name: "Strumenti" });
    await waitFor(() => expect(within(table).getAllByRole("button", { name: /^Scheda / }).map((b) => b.textContent)).toEqual(["MSFT", "AAPL"]));
    expect(JSON.parse(localStorage.getItem(MARKETS_PREFS_KEY)!)).toMatchObject({ sector: "Energia", index: "FTSEMIB", quick: "portfolio", sort: "change", dir: "asc" });
    first.unmount();
    lists = [];
    renderApp("/mercati");
    await waitFor(() => expect(lists.length).toBeGreaterThan(0));
    expect(lists[0]).toContain("sector=Energia");
    expect(screen.getByRole("button", { name: "In portafoglio" })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Azzera filtri" }));
    await waitFor(() => expect(lists.at(-1)).not.toContain("sector="));
  });

  it("survives a blocked storage", async () => {
    const get = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const set = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    const user = userEvent.setup();
    renderApp("/mercati");
    await user.click(await screen.findByRole("button", { name: "Con segnale attivo" }));
    await waitFor(() => expect(lists.at(-1)).toContain("quick=signal"));
    get.mockRestore();
    set.mockRestore();
  });

  it("shows the sector map, drills down, switches universe, lists the movers", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    const map = await screen.findByRole("list", { name: "Mappa dei settori" });
    expect(within(map).getByRole("button", { name: /^Tecnologia: media 1.5%, 2 titoli, 2 in rialzo/ })).toBeInTheDocument();
    expect(screen.getByText(/3 di 4 titoli/)).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Migliori 10" })).getAllByRole("listitem")).toHaveLength(2);
    expect(within(screen.getByRole("list", { name: "Peggiori 10" })).getByText("XOM")).toBeInTheDocument();
    // the treemap: a tile per sector sized by its count, coloured by its change
    await screen.findByTestId("sector-treemap");
    await waitFor(() => expect(instances.at(-1)?.option).not.toBeNull());
    const tm = instances.at(-1)!;
    const data = () => (tm.option!.series as { data: { id: string; value: number; itemStyle: { color: string } }[] }[])[0].data;
    expect(data().map((d) => [d.id, d.value])).toEqual([
      ["Tecnologia", 2],
      ["Energia", 1],
    ]);
    expect(data()[1].itemStyle.color).toBe("rgb(244, 63, 94)"); // -3.1%: full red
    // a click on a tile opens the sector: its members, the breadcrumb
    act(() => tm.click(data()[0]));
    expect(await screen.findByRole("list", { name: "Titoli del settore Tecnologia" })).toBeInTheDocument();
    await waitFor(() => expect(data().map((d) => d.id)).toEqual(["NVDA", "AAPL"]));
    expect(within(screen.getByRole("navigation", { name: "Percorso della mappa" })).getByText("Tecnologia")).toHaveAttribute("aria-current", "page");
    // a member's click opens its sheet
    act(() => tm.click(data()[0]));
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Tutti i settori" }));
    // keyboard: the same tiles as buttons
    await user.click(within(await screen.findByRole("list", { name: "Mappa dei settori" })).getByRole("button", { name: /^Energia/ }));
    expect(await screen.findByRole("list", { name: "Titoli del settore Energia" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Tutti i settori" }));
    await user.selectOptions(screen.getByLabelText("Universo"), "DAX");
    await waitFor(() => expect(snapshots).toContain("DAX"));
    expect(JSON.parse(localStorage.getItem(MARKETS_PREFS_KEY)!).universe).toBe("DAX");
    // hidden: no more snapshot requests
    await user.click(screen.getByRole("button", { name: "Nascondi" }));
    expect(screen.queryByRole("list", { name: "Mappa dei settori" })).not.toBeInTheDocument();
  });
});
