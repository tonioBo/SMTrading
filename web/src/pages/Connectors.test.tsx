import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { reorder } from "@/lib/reorder";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const connector = (over: Record<string, unknown>) => ({
  name: "twelvedata",
  enabled: true,
  requires_key: true,
  capabilities: ["quote", "bars"],
  asset_classes: ["forex"],
  implemented: true,
  config_masked: { api_key: "••••abcd", ws_url: "wss://example.test" },
  secret_keys: ["api_key"],
  rate_limit_per_min: 60,
  health_status: "ok",
  last_error: null,
  last_checked_at: null,
  ...over,
});

let patches: { name: string; body: Record<string, unknown> }[] = [];
let puts: unknown[] = [];
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/connectors", () =>
    HttpResponse.json([connector({}), connector({ name: "revolutx", implemented: false, enabled: false, config_masked: {}, secret_keys: [] })]),
  ),
  http.patch("*/api/connectors/:name", async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    patches.push({ name: String(params.name), body });
    return HttpResponse.json(connector({ name: params.name, rate_limit_per_min: body.rate_limit_per_min ?? 60 }));
  }),
  http.post("*/api/connectors/:name/test", () => HttpResponse.json({ ok: false, error: "timeout", checked_at: "2026-11-13T10:00:00Z" })),
  http.get("*/api/provider-priorities", () =>
    HttpResponse.json([
      { asset_class: "us_equity", capability: "quote", providers: ["alpaca", "finnhub", "yahoo"] },
      { asset_class: "us_equity", capability: "fx", providers: [] },
    ]),
  ),
  http.put("*/api/provider-priorities", async ({ request }) => {
    const body = await request.json();
    puts.push(body);
    return HttpResponse.json(body);
  }),
  http.get("*/api/marketdata/stream-status", () =>
    HttpResponse.json({
      streams: { "kraken:crypto": { symbols: ["BTC-USD"], state: "backoff" } },
      polling: ["AAPL"],
      available: true,
      tracked: ["BTC-USD", "AAPL"],
    }),
  ),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  patches = [];
  puts = [];
});
afterAll(() => server.close());

describe("reorder", () => {
  it("moves an item and never mutates the input", () => {
    const list = ["a", "b", "c"];
    expect(reorder(list, 0, 2)).toEqual(["b", "c", "a"]);
    expect(reorder(list, 2, 0)).toEqual(["c", "a", "b"]);
    expect(list).toEqual(["a", "b", "c"]);
  });
  it("ignores out-of-range or no-op moves", () => {
    expect(reorder(["a", "b"], 0, 5)).toEqual(["a", "b"]);
    expect(reorder(["a", "b"], 1, 1)).toEqual(["a", "b"]);
  });
});

describe("Connectors", () => {
  it("does not send an untouched masked key, and sends a typed one", async () => {
    const user = userEvent.setup();
    renderApp("/connettori");
    const card = await screen.findByLabelText("Connettore twelvedata");
    expect(within(card).getByLabelText("api_key")).toHaveAttribute("placeholder", "••••abcd");
    const rate = within(card).getByLabelText("Limite richieste al minuto");
    await user.clear(rate);
    await user.type(rate, "30");
    await user.click(within(card).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0].body).toEqual({ rate_limit_per_min: 30 });

    await user.type(within(card).getByLabelText("api_key"), "NEWSECRET");
    await user.click(within(card).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(patches).toHaveLength(2));
    expect(patches[1].body).toEqual({ config: { api_key: "NEWSECRET", ws_url: "wss://example.test" } });
  });

  it("sends null to remove a key", async () => {
    const user = userEvent.setup();
    renderApp("/connettori");
    const card = await screen.findByLabelText("Connettore twelvedata");
    await user.click(within(card).getByRole("button", { name: "Rimuovi api_key" }));
    await user.click(within(card).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0].body).toEqual({ config: { api_key: null, ws_url: "wss://example.test" } });
  });

  it("marks unimplemented connectors and disables the switch", async () => {
    renderApp("/connettori");
    const card = await screen.findByLabelText("Connettore revolutx");
    expect(within(card).getByText("Disponibile prossimamente")).toBeInTheDocument();
    expect(within(card).getByRole("switch", { name: "Attiva revolutx" })).toBeDisabled();
  });

  it("shows the connection test outcome", async () => {
    const user = userEvent.setup();
    renderApp("/connettori");
    const card = await screen.findByLabelText("Connettore twelvedata");
    await user.click(within(card).getByRole("button", { name: "Testa connessione" }));
    expect(await within(card).findByText("Test fallito: timeout")).toBeInTheDocument();
  });

  it("reorders a priority list and PUTs that cell", async () => {
    const user = userEvent.setup();
    renderApp("/connettori");
    await user.click(await screen.findByRole("button", { name: "Sposta giù alpaca" }));
    const list = screen.getByRole("list", { name: "Priorità us_equity quote" });
    expect(within(list).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["finnhub", "alpaca", "yahoo"]);
    await user.click(screen.getByRole("button", { name: "Salva priorità us_equity quote" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({ asset_class: "us_equity", capability: "quote", providers: ["finnhub", "alpaca", "yahoo"] });
    expect(screen.queryByText("fx")).not.toBeInTheDocument();
  });

  it("shows stream state and polling symbols", async () => {
    renderApp("/connettori");
    expect(await screen.findByText("kraken:crypto")).toBeInTheDocument();
    expect(screen.getByText("In attesa di riconnessione")).toBeInTheDocument();
    expect(screen.getByText("In polling: AAPL")).toBeInTheDocument();
  });
});
