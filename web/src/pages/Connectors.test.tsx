import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { reorder } from "@/lib/reorder";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

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
  config_fields: [],
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
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/connectors", () =>
    HttpResponse.json([
      connector({}),
      connector({ name: "revolutx", implemented: false, enabled: false, config_masked: {}, secret_keys: [] }),
      connector({
        name: "etoro",
        enabled: false,
        capabilities: ["bars", "quote"],
        asset_classes: ["crypto", "us_equity"],
        config_masked: { api_key: "••••0001" },
        config_fields: ["api_key", "user_key"],
        secret_keys: ["api_key", "user_key"],
      }),
      connector({
        name: "rssnews",
        requires_key: false,
        capabilities: ["news"],
        asset_classes: ["crypto"],
        config_masked: {},
        secret_keys: [],
      }),
      connector({
        name: "alpaca",
        enabled: false,
        capabilities: ["bars", "news", "quote", "stream"],
        asset_classes: ["crypto", "us_equity"],
        config_masked: {},
        config_fields: ["key_id", "secret_key"],
        secret_keys: ["key_id", "secret_key"],
      }),
    ]),
  ),
  http.patch("*/api/connectors/:name", async ({ request, params }) => {
    const body = (await request.json()) as Record<string, unknown>;
    patches.push({ name: String(params.name), body });
    return HttpResponse.json(connector({ name: params.name, rate_limit_per_min: body.rate_limit_per_min ?? 60 }));
  }),
  http.post("*/api/connectors/:name/test", ({ params }) =>
    params.name === "alpaca"
      ? HttpResponse.json(
          { code: "INVALID_ORDER", message: "configurazione incompleta: inserisci Key ID e Secret Key", details: { field: "config" } },
          { status: 400 },
        )
      : HttpResponse.json({ ok: false, error: "timeout", checked_at: "2026-11-13T10:00:00Z" }),
  ),
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

  it("keyless crypto news connector shows its description and no key field", async () => {
    renderApp("/connettori");
    const card = await screen.findByLabelText("Connettore rssnews");
    expect(within(card).getByText("Notizie crypto (CoinDesk, Cointelegraph)")).toBeInTheDocument();
    expect(within(card).queryByLabelText("api_key")).not.toBeInTheDocument();
    const other = await screen.findByLabelText("Connettore twelvedata");
    expect(within(other).queryByText(/Notizie crypto/)).not.toBeInTheDocument();
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

  it("etoro connector shows api and user key fields masked with guide", async () => {
    const user = userEvent.setup();
    renderApp("/connettori");
    const card = await screen.findByLabelText("Connettore etoro");
    const apiKey = within(card).getByLabelText("Chiave API (x-api-key)");
    const userKey = within(card).getByLabelText("Chiave utente (x-user-key)");
    expect(apiKey).toHaveAttribute("type", "password");
    expect(apiKey).toHaveAttribute("placeholder", "••••0001");
    expect(apiKey).toHaveValue("");
    expect(userKey).toHaveAttribute("type", "password");
    expect(userKey).toHaveValue("");
    expect(within(card).getByText(/chiave dedicata, ambiente Demo, permesso Read/)).toBeInTheDocument();
    expect(within(card).getByText(/non riusare le chiavi dei collegamenti/)).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: /autenticazione eToro/ })).toHaveAttribute(
      "href",
      "https://api-portal.etoro.com/core/getting-started/authentication.md",
    );
    expect(within(card).getByLabelText("Limite richieste al minuto")).toHaveValue("60");

    // the missing user key is sent; the untouched masked api key is kept by the server
    await user.type(userKey, "USERKEY123");
    await user.click(within(card).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ name: "etoro", body: { config: { user_key: "USERKEY123" } } });
  });

  it("alpaca connector shows Key ID and Secret Key (no api_key) and names missing keys on test", async () => {
    const user = userEvent.setup();
    renderApp("/connettori");
    const card = await screen.findByLabelText("Connettore alpaca");
    const keyId = within(card).getByLabelText("Key ID");
    const secret = within(card).getByLabelText("Secret Key");
    expect(keyId).toHaveAttribute("type", "password");
    expect(secret).toHaveAttribute("type", "password");
    expect(within(card).queryByLabelText("api_key")).not.toBeInTheDocument();
    expect(within(card).getByText("Le stesse chiavi del conto Alpaca (Paper va bene).")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { name: "Testa connessione" }));
    expect(await within(card).findByText(/configurazione incompleta: inserisci Key ID e Secret Key/)).toBeInTheDocument();

    await user.type(keyId, "PKTEST1");
    await user.type(secret, "SECRET1");
    await user.click(within(card).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toEqual({ name: "alpaca", body: { config: { key_id: "PKTEST1", secret_key: "SECRET1" } } });
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
