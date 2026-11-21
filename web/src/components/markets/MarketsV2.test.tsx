import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { ReactNode } from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { Favorites } from "@/components/markets/Favorites";
import { FavoriteStar } from "@/components/markets/FavoriteStar";
import { FundamentalsSection } from "@/components/markets/Fundamentals";
import { MarketNews } from "@/components/markets/MarketNews";
import { MarketsToday } from "@/components/markets/MarketsToday";
import { PriceAlerts } from "@/components/markets/PriceAlerts";
import { Screener } from "@/components/markets/Screener";
import { WeekCalendar } from "@/components/markets/WeekCalendar";
import { createQueryClient } from "@/queryClient";

const base = { market: "NASDAQ", currency: "USD", lot_size: "1", tick_size: "0.01", shortable: true, active: true, provider_symbols: {}, source: "catalog", sector: "Tecnologia", industry: null, indices: [] };
const fav = (symbol: string, position: number) => ({ ...base, symbol, name: `${symbol} Inc.`, asset_class: "us_equity", position });

const SPX = {
  code: "SPX", symbol: "^GSPC", label: "S&P 500", group: "Stati Uniti", unit: "", decimals: 2, note: "", currency: "USD", market: "NYSE",
  value: "6910.39", prev_close: "7175.83", change_pct: "-3.70", ts: "2026-11-20T21:00:00Z", is_open: false,
  next_open: "2099-11-23T14:30:00Z", next_close: null, spark: [[1, 7100], [2, 7000], [3, 6910.39]],
};
const TNX = { ...SPX, code: "TNX", symbol: "^TNX", label: "Treasury 10 anni", unit: "%", decimals: 3, value: "4.9119997", change_pct: "-2.77" };

let favorites = [fav("AAPL", 0), fav("MSFT", 1)];
let putOrder: string[] | null = null;
let deleted: string[] = [];
let created: unknown[] = [];
let charts: string[] = [];
let screenerParams: string[] = [];
let newsParams: string[] = [];

const server = setupServer(
  http.get("*/api/markets/overview", () => HttpResponse.json({ as_of: "2026-11-21T12:00:00Z", ttl_s: 300, stale: false, items: [SPX, TNX] })),
  http.get("*/api/markets/overview/:code/chart", ({ params, request }) => {
    const range = new URL(request.url).searchParams.get("range");
    charts.push(`${params.code}:${range}`);
    return HttpResponse.json({ code: params.code, range, label: "S&P 500", points: [[1, 1], [2, 2]], error: null });
  }),
  http.get("*/api/markets/favorites", () => HttpResponse.json({ items: favorites })),
  http.put("*/api/markets/favorites/:symbol", ({ params }) => {
    favorites = [...favorites, fav(String(params.symbol), favorites.length)];
    return HttpResponse.json({ items: favorites });
  }),
  http.delete("*/api/markets/favorites/:symbol", ({ params }) => {
    deleted.push(String(params.symbol));
    favorites = favorites.filter((f) => f.symbol !== params.symbol);
    return HttpResponse.json({ items: favorites });
  }),
  http.put("*/api/markets/favorites", async ({ request }) => {
    putOrder = ((await request.json()) as { symbols: string[] }).symbols;
    favorites = putOrder.map((s, i) => fav(s, i));
    return HttpResponse.json({ items: favorites });
  }),
  http.get("*/api/quotes", () => HttpResponse.json({ AAPL: { symbol: "AAPL", bid: null, ask: null, last: "200.5", ts: "2026-11-21T12:00:00Z", provider: "yahoo" } })),
  http.get("*/api/markets/day-changes", () => HttpResponse.json({ AAPL: { last: "200.5", prev_close: "190", change_pct: "5.53", ts: "2026-11-21T12:00:00Z", provider: "yahoo" } })),
  http.get("*/api/markets/screener", ({ request }) => {
    const p = new URL(request.url).searchParams;
    screenerParams.push(`${p.get("preset")}:${p.get("universe")}`);
    return HttpResponse.json({
      preset: p.get("preset"), label: "RSI(14) sotto 30", universe: p.get("universe"),
      presets: [], universes: [{ code: "ALL", label: "Tutto il catalogo" }, { code: "CRYPTO", label: "Crypto" }],
      items: [
        { symbol: "INTC", name: "Intel", sector: "Tecnologia", market: "NASDAQ", currency: "USD", asset_class: "us_equity", as_of: "2026-11-20", close: "20.5", rsi14: "24.31", sma200: "25", vs_sma200_pct: "-18.00", high_52w: "40", low_52w: "19", from_low_pct: "7.89", from_high_pct: "-48.75", volume_ratio: "2.4", chg_1d: "-3.10", chg_1w: null, chg_1m: null, chg_ytd: null },
      ],
      matched: 1, computed: 900, total: 919, as_of: "2026-11-20T22:40:00Z",
    });
  }),
  http.get("*/api/markets/calendar", () =>
    HttpResponse.json({
      from: "2026-11-16", to: "2026-11-29", today: "2026-11-24",
      events: [
        { date: "2026-11-24", kind: "earnings", symbol: "NVDA", name: "NVIDIA", sector: "Tecnologia", held: true, portfolios: ["Portafoglio Alpaca"], estimate: false, label: null },
        { date: "2026-11-24", kind: "earnings", symbol: "DELL", name: "Dell", sector: "Tecnologia", held: false, portfolios: [], estimate: true, label: null },
        { date: "2026-11-26", kind: "ex_dividend", symbol: "ENI.MI", name: "Eni", sector: "Energia", held: false, portfolios: [], estimate: false, label: null },
        { date: "2026-12-09", kind: "macro", symbol: null, name: "Fed", sector: null, held: false, portfolios: [], estimate: false, label: "Riunione FOMC (8-9 dicembre)" },
      ],
      covered: 800, total: 826,
      macro: { as_of: "2026-11-21", sources: { Fed: "https://www.federalreserve.gov/x" }, note: "Riunioni delle banche centrali." },
    }),
  ),
  http.get("*/api/markets/news", ({ request }) => {
    newsParams.push(new URL(request.url).searchParams.toString());
    return HttpResponse.json({ symbols: ["SPY"], error: null, items: [{ id: "n1", ts: "2026-11-21T11:00:00Z", headline: "Wall Street giù", summary: null, url: "https://x/1", source: "Reuters", symbols: ["SPY"] }] });
  }),
  http.get("*/api/instruments/facets", () => HttpResponse.json({ sectors: [{ sector: "Energia", count: 3 }], indices: [], universes: [], sector_list: [], etf_types: [] })),
  http.get("*/api/markets/price-alerts", () =>
    HttpResponse.json([
      { id: 1, symbol: "SPX", label: "S&P 500", source: "overview", direction: "below", threshold: "6500", note: "", status: "triggered", created_at: "2026-11-20T10:00:00Z", triggered_at: "2026-11-20T15:00:00Z", triggered_price: "6499" },
    ]),
  ),
  http.post("*/api/markets/price-alerts", async ({ request }) => {
    created.push(await request.json());
    return HttpResponse.json({ id: 2 });
  }),
  http.get("*/api/instruments/:symbol/fundamentals", ({ params }) =>
    HttpResponse.json({
      symbol: params.symbol, market_cap: "4978144444416", pe_trailing: "38.15", pe_forward: null, dividend_yield: "0.31", low_52w: "243.42", high_52w: "360.83",
      beta: "1.089", next_earnings: "2027-01-28", earnings_estimate: true, ex_dividend: null, dividend_date: null, currency: "USD", source: "yahoo",
    }),
  ),
);

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  favorites = [fav("AAPL", 0), fav("MSFT", 1)];
  putOrder = null;
  deleted = [];
  created = [];
  charts = [];
  screenerParams = [];
  newsParams = [];
});
afterAll(() => server.close());

function wrap(node: ReactNode) {
  return render(<QueryClientProvider client={createQueryClient()}>{node}</QueryClientProvider>);
}

describe("Mercati v2", () => {
  it("«Mercati oggi»: real index values, change, state and countdown; a tile opens its chart", async () => {
    const user = userEvent.setup();
    wrap(<MarketsToday />);
    const tile = await screen.findByRole("button", { name: "S&P 500: 6.910,39, -3.70%" });
    expect(tile).toHaveTextContent("-3,70%");
    expect(tile).toHaveTextContent(/Chiuso · apre tra \d+ g/);
    expect(tile.querySelector("polyline")).not.toBeNull();
    expect(screen.getByRole("button", { name: /^Treasury 10 anni: 4,912%/ })).toBeInTheDocument();
    await user.click(tile);
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(charts).toEqual(["SPX:1d"]));
    await user.click(within(dialog).getByRole("button", { name: "1a" }));
    await waitFor(() => expect(charts).toEqual(["SPX:1d", "SPX:1y"]));
  });

  it("«Preferiti»: live prices, reorder and the star", async () => {
    const user = userEvent.setup();
    const opened: string[] = [];
    wrap(<Favorites onOpen={(s) => opened.push(s)} />);
    const list = await screen.findByRole("list", { name: "I miei preferiti" });
    await waitFor(() => expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("200,50 $"));
    expect(within(list).getAllByRole("listitem")[0]).toHaveTextContent("+5,53%");
    await user.click(screen.getByRole("button", { name: "Sposta MSFT su" }));
    await waitFor(() => expect(putOrder).toEqual(["MSFT", "AAPL"]));
    await user.click(screen.getByRole("button", { name: "Scheda AAPL" }));
    expect(opened).toEqual(["AAPL"]);
    await user.click(screen.getByRole("button", { name: "Togli AAPL dai preferiti" }));
    await waitFor(() => expect(deleted).toEqual(["AAPL"]));
  });

  it("the star adds an instrument", async () => {
    const user = userEvent.setup();
    wrap(<FavoriteStar symbol="NVDA" />);
    const star = await screen.findByRole("button", { name: "Aggiungi NVDA ai preferiti" });
    await waitFor(() => expect(star).toBeEnabled());
    await user.click(star);
    expect(await screen.findByRole("button", { name: "Togli NVDA dai preferiti" })).toHaveAttribute("aria-pressed", "true");
  });

  it("«Screener»: presets and universe, the precomputed rows", async () => {
    const user = userEvent.setup();
    wrap(<Screener onOpen={() => {}} />);
    expect(await screen.findByText("INTC")).toBeInTheDocument();
    expect(screen.getByText(/1 titoli · calcolati 900 di 919/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Volume > 2× media" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Universo dello screener" }), "CRYPTO");
    await waitFor(() => expect(screenerParams).toEqual(["rsi_oversold:ALL", "volume_spike:ALL", "volume_spike:CRYPTO"]));
  });

  it("«Calendario»: by day, held first and highlighted, central banks dated", async () => {
    wrap(<WeekCalendar onOpen={() => {}} />);
    const day = await screen.findByRole("region", { name: "mar 24/11" });
    const rows = within(day).getAllByRole("listitem");
    expect(rows[0]).toHaveTextContent("NVDA");
    expect(rows[0]).toHaveTextContent("in portafoglio: Portafoglio Alpaca");
    expect(rows[1]).toHaveTextContent("data stimata");
    expect(screen.getByRole("heading", { name: "mar 24/11 · oggi" })).toBeInTheDocument();
    expect(screen.getByText("Riunione FOMC (8-9 dicembre)")).toBeInTheDocument();
    expect(screen.getByText(/aggiornato al 21\/11\/2026/)).toBeInTheDocument();
  });

  it("«Notizie»: headlines and the «solo portafogli» filter", async () => {
    const user = userEvent.setup();
    wrap(<MarketNews onOpen={() => {}} />);
    expect(await screen.findByRole("link", { name: /Wall Street giù/ })).toHaveAttribute("href", "https://x/1");
    await user.click(screen.getByRole("switch", { name: "Solo portafogli" }));
    await waitFor(() => expect(newsParams).toContain("portfolios=true"));
    await user.type(screen.getByRole("textbox", { name: "Notizie di un titolo" }), "aapl{Enter}");
    await waitFor(() => expect(newsParams).toContain("symbol=AAPL"));
  });

  it("«Avvisi di prezzo»: list and create (it-IT amounts)", async () => {
    const user = userEvent.setup();
    wrap(<PriceAlerts />);
    const list = await screen.findByRole("list", { name: "Avvisi di prezzo" });
    expect(list).toHaveTextContent("Scattato");
    expect(screen.getByRole("button", { name: "Riattiva l'avviso su S&P 500" })).toBeInTheDocument();
    const form = screen.getByRole("form", { name: "Nuovo avviso di prezzo" });
    await user.type(within(form).getByRole("textbox", { name: "Simbolo" }), "aapl");
    await user.type(within(form).getByRole("textbox", { name: "Soglia" }), "1.250,5");
    await user.click(within(form).getByRole("button", { name: "Crea avviso" }));
    await waitFor(() => expect(created).toEqual([{ symbol: "AAPL", source: "catalog", direction: "above", threshold: "1250.5", note: "" }]));
  });

  it("«Dati fondamentali»: figures and «non disponibile»", async () => {
    wrap(<FundamentalsSection symbol="AAPL" currency="USD" />);
    expect(await screen.findByText("4,98 T USD")).toBeInTheDocument();
    expect(screen.getByText("38,2")).toBeInTheDocument();
    expect(screen.getByText("0,31%")).toBeInTheDocument();
    expect(screen.getByText("28/01/2027")).toBeInTheDocument();
    expect(screen.getByText("data stimata")).toBeInTheDocument();
    expect(screen.getAllByText("non disponibile")).toHaveLength(1); // P/E stimato
  });
});

