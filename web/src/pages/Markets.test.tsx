import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let posts: unknown[] = [];
let tracks: unknown[] = [];
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/marketdata/stream-status", () => HttpResponse.json({ streams: {}, polling: [], available: true, tracked: ["AAPL"] })),
  http.get("*/api/markets/status", () =>
    HttpResponse.json([{ market: "XNAS", is_open: true, next_open: null, next_close: "2026-11-13T21:00:00Z" }]),
  ),
  http.get("*/api/quotes", () => HttpResponse.json({ AAPL: { symbol: "AAPL", bid: "189.1", ask: "189.2", last: "189.15", ts: null, provider: "alpaca" } })),
  http.get("*/api/instruments", () =>
    HttpResponse.json([
      { symbol: "AAPL", name: "Apple", asset_class: "us_equity", market: "XNAS", currency: "USD", lot_size: "1", tick_size: "0.01", shortable: true, active: true, provider_symbols: {} },
      { symbol: "MSFT", name: "Microsoft", asset_class: "us_equity", market: "XNAS", currency: "USD", lot_size: "1", tick_size: "0.01", shortable: true, active: true, provider_symbols: {} },
    ]),
  ),
  http.post("*/api/instruments", async ({ request }) => {
    const body = await request.json();
    posts.push(body);
    return HttpResponse.json(body);
  }),
  http.post("*/api/marketdata/:action", async ({ request, params }) => {
    tracks.push({ action: params.action, body: await request.json() });
    return HttpResponse.json({});
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  posts = [];
  tracks = [];
});
afterAll(() => server.close());

describe("Markets", () => {
  it("shows tracked quotes and market hours", async () => {
    renderApp("/mercati");
    const quote = await screen.findByLabelText("Quota AAPL");
    expect(await within(quote).findByText("189,15")).toBeInTheDocument();
    expect(await screen.findByText("Aperto")).toBeInTheDocument();
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
});
