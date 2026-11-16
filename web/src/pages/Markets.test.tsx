import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let posts: unknown[] = [];
let tracks: unknown[] = [];
let autoAdd: unknown[] = [];
let instrumentQueries: string[] = [];
let closurePosts: unknown[] = [];
let closureDeletes: string[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/marketdata/stream-status", () =>
    HttpResponse.json({
      streams: {},
      polling: [],
      available: true,
      tracked: ["AAPL", "SPY"],
      tracked_sources: { AAPL: ["admin", "watchlists"], SPY: ["watchlists"] },
    }),
  ),
  http.get("*/api/instruments/auto-add", () => HttpResponse.json({ on: true, daily_limit: 25 })),
  http.put("*/api/instruments/auto-add", async ({ request }) => {
    const body = (await request.json()) as { on: boolean };
    autoAdd.push(body);
    return HttpResponse.json({ ...body, daily_limit: 25 });
  }),
  http.get("*/api/markets/status", () =>
    HttpResponse.json([
      { market: "XNAS", tradable: true, is_open: true, next_open: null, next_close: "2026-11-13T21:00:00Z" },
      { market: "CRYPTO", tradable: true, is_open: true, next_open: null, next_close: null },
      { market: "LSE", tradable: false, is_open: false, next_open: "2026-11-16T08:00:00Z", next_close: "2026-11-16T16:30:00Z" },
    ]),
  ),
  http.get("*/api/market-closures", () =>
    HttpResponse.json([
      { id: 7, market: "XNAS", day: "2026-11-27", kind: "early_close", close_time: "13:00", note: "Dopo il Ringraziamento", created_by: "admin:admin@example.com", created_at: null },
      { id: 8, market: "LSE", day: "2026-12-01", kind: "closed", close_time: null, note: "", created_by: "admin:admin@example.com", created_at: null },
    ]),
  ),
  http.post("*/api/market-closures", async ({ request }) => {
    const body = await request.json();
    closurePosts.push(body);
    return HttpResponse.json({ id: 9, ...(body as object) });
  }),
  http.delete("*/api/market-closures/:id", ({ params }) => {
    closureDeletes.push(String(params.id));
    return HttpResponse.json({ deleted: Number(params.id) });
  }),
  http.get("*/api/quotes", () => HttpResponse.json({ AAPL: { symbol: "AAPL", bid: "189.1", ask: "189.2", last: "189.15", ts: null, provider: "alpaca" } })),
  http.get("*/api/instruments", ({ request }) => {
    instrumentQueries.push(new URL(request.url).search);
    const base = { asset_class: "us_equity", market: "XNAS", currency: "USD", lot_size: "1", tick_size: "0.01", shortable: true, active: true, provider_symbols: {} };
    return HttpResponse.json({
      items: [
        { ...base, symbol: "AAPL", name: "Apple", source: "catalog", added_by: null },
        { ...base, symbol: "MSFT", name: "Microsoft", source: "manual", added_by: null },
        { ...base, symbol: "SPY", name: "SPDR", source: "auto", added_by: "auto:alpha" },
      ],
      total: 250,
      offset: 0,
      limit: 100,
    });
  }),
  http.post("*/api/instruments", async ({ request }) => {
    const body = await request.json();
    posts.push(body);
    return HttpResponse.json(body);
  }),
  http.post("*/api/marketdata/:action", async ({ request, params }) => {
    const body = (await request.json()) as { symbols: string[] };
    tracks.push({ action: params.action, body });
    if (params.action === "untrack") return HttpResponse.json({ untracked: body.symbols, still_tracked: { AAPL: ["watchlists"] } });
    return HttpResponse.json({ tracked: body.symbols });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  posts = [];
  tracks = [];
  autoAdd = [];
  instrumentQueries = [];
  closurePosts = [];
  closureDeletes = [];
});
afterAll(() => server.close());

describe("Markets", () => {
  it("shows tracked quotes and market hours", async () => {
    renderApp("/mercati");
    const quote = await screen.findByLabelText("Quota AAPL");
    expect(await within(quote).findByText("189,15")).toBeInTheDocument();
    const hours = await screen.findByRole("table", { name: "Orari dei mercati" });
    const lse = (await within(hours).findByText("LSE")).closest("tr")!;
    expect(within(lse).getByText("Chiuso")).toBeInTheDocument();
    expect(within(hours).getAllByText("Aperto")).toHaveLength(2);
  });

  it("shows the bid/ask mid when the quote has no last trade", async () => {
    server.use(
      http.get("*/api/quotes", () =>
        HttpResponse.json({ AAPL: { symbol: "AAPL", bid: "741.01", ask: "741.09", last: null, ts: null, provider: "alpaca" } }),
      ),
    );
    renderApp("/mercati");
    const quote = await screen.findByLabelText("Quota AAPL");
    expect(await within(quote).findByText("741,05")).toBeInTheDocument();
    expect(within(quote).getByText("medio")).toBeInTheDocument();
  });

  it("follows and unfollows symbols", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    await user.click(await screen.findByRole("button", { name: "Segui MSFT" }));
    await waitFor(() => expect(tracks).toHaveLength(1));
    expect(tracks[0]).toEqual({ action: "track", body: { symbols: ["MSFT"] } });
    await user.click(await screen.findByRole("button", { name: "Smetti di seguire AAPL" }));
    await waitFor(() => expect(tracks).toHaveLength(2));
    expect(tracks[1]).toEqual({ action: "untrack", body: { symbols: ["AAPL"] } });
    expect(await screen.findByText("AAPL: resta seguito perché usato da: watchlist agenti")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows who follows each symbol; only the admin's own follow can be stopped", async () => {
    renderApp("/mercati");
    const table = await screen.findByRole("table", { name: "Strumenti" });
    const spy = (await within(table).findByText("SPY")).closest("tr")!;
    expect(within(spy).getByText("watchlist agenti")).toBeInTheDocument();
    expect(within(spy).getByRole("button", { name: "Segui SPY" })).toBeInTheDocument();
    const aapl = within(table).getByText("AAPL").closest("tr")!;
    expect(within(aapl).getByText("admin, watchlist agenti")).toBeInTheDocument();
    expect(within(table).getByText("catalogo")).toBeInTheDocument();
    expect(within(table).getByText("manuale")).toBeInTheDocument();
    expect(within(table).getByText("auto: agente alpha")).toBeInTheDocument();
  });

  it("toggles auto-add, filters by market and pages", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    const toggle = await screen.findByRole("switch", { name: "Aggiunta automatica degli strumenti" });
    expect(toggle).toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(autoAdd).toEqual([{ on: false }]));
    await user.selectOptions(screen.getByLabelText("Filtra per mercato"), "XNAS");
    await waitFor(() => expect(instrumentQueries.some((q) => q.includes("market=XNAS"))).toBe(true));
    expect(await screen.findByText("1–100 di 250")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Successivi" }));
    await waitFor(() => expect(instrumentQueries.some((q) => q.includes("offset=100"))).toBe(true));
  });

  it("validates the new instrument client-side, then posts normalized values", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    await user.click(await screen.findByRole("button", { name: "Aggiungi strumento" }));
    const dialog = await screen.findByRole("dialog", { name: "Aggiungi strumento" });
    await user.click(within(dialog).getByRole("button", { name: "Aggiungi" }));
    expect(await within(dialog).findByText("Simbolo non valido")).toBeInTheDocument();
    expect(posts).toHaveLength(0);

    await user.type(within(dialog).getByLabelText("Simbolo"), "nvda");
    await user.type(within(dialog).getByLabelText("Nome"), "Nvidia");
    const lot = within(dialog).getByLabelText("Lotto minimo");
    await user.clear(lot);
    await user.type(lot, "0");
    await user.click(within(dialog).getByRole("button", { name: "Aggiungi" }));
    expect(await within(dialog).findByText("Deve essere maggiore di zero")).toBeInTheDocument();
    await user.clear(lot);
    await user.type(lot, "1");
    await user.click(within(dialog).getByRole("button", { name: "Aggiungi" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ symbol: "NVDA", market: "XNAS", currency: "USD", lot_size: "1", tick_size: "0.01" });
  });

  it("offers only tradable markets for a new instrument", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    await screen.findByRole("table", { name: "Orari dei mercati" });
    await user.click(await screen.findByRole("button", { name: "Aggiungi strumento" }));
    const dialog = await screen.findByRole("dialog", { name: "Aggiungi strumento" });
    const options = within(within(dialog).getByLabelText("Mercato")).getAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["XNAS", "CRYPTO"]);
  });

  it("lists closures and adds an early close", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    const table = await screen.findByRole("table", { name: "Chiusure straordinarie" });
    const row = (await within(table).findByText("Dopo il Ringraziamento")).closest("tr")!;
    expect(within(row).getByText("27/11/2026")).toBeInTheDocument();
    expect(within(row).getByText("Chiusura anticipata alle 13:00")).toBeInTheDocument();
    expect(within(row).getByText("admin@example.com")).toBeInTheDocument();
    expect(within(table).getByText("Chiusa")).toBeInTheDocument();

    const form = screen.getByRole("form", { name: "Nuova chiusura" });
    const market = within(form).getByLabelText("Mercato");
    expect(within(market).getAllByRole("option").map((o) => o.textContent)).toEqual(["XNAS", "LSE"]);
    await user.selectOptions(market, "LSE");
    await user.type(within(form).getByLabelText("Giorno"), "2026-12-24");
    await user.selectOptions(within(form).getByLabelText("Tipo"), "early_close");
    await user.click(within(form).getByRole("button", { name: "Aggiungi chiusura" }));
    expect(await within(form).findByText("Indica l'ora della chiusura")).toBeInTheDocument();
    expect(closurePosts).toHaveLength(0);
    await user.type(within(form).getByLabelText("Ora di chiusura (locale)"), "12:30");
    await user.type(within(form).getByLabelText("Nota"), " Vigilia ");
    await user.click(within(form).getByRole("button", { name: "Aggiungi chiusura" }));
    await waitFor(() => expect(closurePosts).toHaveLength(1));
    expect(closurePosts[0]).toEqual({ market: "LSE", day: "2026-12-24", kind: "early_close", close_time: "12:30", note: "Vigilia" });
  });

  it("deletes a closure after confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/mercati");
    const table = await screen.findByRole("table", { name: "Chiusure straordinarie" });
    await user.click(await within(table).findByRole("button", { name: "Elimina la chiusura LSE del 01/12/2026" }));
    const dialog = await screen.findByRole("dialog");
    expect(closureDeletes).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Elimina" }));
    await waitFor(() => expect(closureDeletes).toEqual(["8"]));
  });

  it("shows the closures to a viewer without the form and the delete buttons", async () => {
    server.use(
      http.get("*/api/auth/me", () =>
        HttpResponse.json({ email: "vera@example.com", totp_enabled: false, role: "viewer", must_change_password: false }),
      ),
    );
    renderApp("/mercati");
    const table = await screen.findByRole("table", { name: "Chiusure straordinarie" });
    expect(await within(table).findByText("Dopo il Ringraziamento")).toBeInTheDocument();
    expect(within(table).queryByRole("button", { name: /Elimina la chiusura/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Nuova chiusura" })).not.toBeInTheDocument();
  });
});
