import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { BrokerLink, Reconciliation } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const link = (over: Partial<BrokerLink> = {}): BrokerLink => ({
  id: "b1",
  name: "Alpaca prova",
  kind: "alpaca_paper",
  enabled: true,
  status: "ok",
  last_error: null,
  base_url: "https://paper-api.alpaca.markets",
  config_masked: { key_id: "••••5678", secret_key: "••••9999" },
  secret_keys: ["key_id", "secret_key"],
  portfolios: [{ id: "p1", name: "Alfa" }],
  created_at: "2026-11-12T10:00:00Z",
  ...over,
});

const reconciliations: Reconciliation[] = [
  {
    id: "r2",
    broker_link_id: "b1",
    portfolio_id: "p1",
    portfolio_name: "Alfa",
    ts: "2026-11-13T09:00:00Z",
    ok: false,
    diffs: [{ symbol: "AAPL", ours: "10", broker: "7" }],
  },
  { id: "r1", broker_link_id: "b1", portfolio_id: "p1", portfolio_name: "Alfa", ts: "2026-11-12T09:00:00Z", ok: true, diffs: [] },
];

let patches: unknown[] = [];
let creates: { body: unknown; key: string | null }[] = [];
let puts: { id: string; body: unknown }[] = [];
let reconciles = 0;
let applies: { id: string; body: unknown }[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/fee-rules", () => HttpResponse.json([])),
  http.get("*/api/broker-links", () => HttpResponse.json([link()])),
  http.get("*/api/live-links", () => HttpResponse.json([])),
  http.get("*/api/portfolios", () =>
    HttpResponse.json([
      { id: "p1", name: "Alfa", broker_link_id: "b1" },
      { id: "p2", name: "Beta", broker_link_id: null },
    ]),
  ),
  http.post("*/api/broker-links", async ({ request }) => {
    const body = (await request.json()) as { name: string };
    creates.push({ body, key: request.headers.get("Idempotency-Key") });
    return HttpResponse.json(link({ id: "b2", name: body.name, portfolios: [] }));
  }),
  http.patch("*/api/broker-links/:id", async ({ request }) => {
    patches.push(await request.json());
    return HttpResponse.json(link({ config_masked: { key_id: "••••5678", secret_key: "••••7777" } }));
  }),
  http.post("*/api/broker-links/:id/test", () =>
    HttpResponse.json({ ok: false, error: "RuntimeError: alpaca_paper: HTTP 401: error", checked_at: "2026-11-13T10:00:00Z" }),
  ),
  http.put("*/api/portfolios/:id/broker-link", async ({ request, params }) => {
    const body = (await request.json()) as { broker_link_id: string | null };
    puts.push({ id: String(params.id), body });
    if (body.broker_link_id === null) {
      return HttpResponse.json(
        {
          code: "INVALID_ORDER",
          message: "ordini in corso presso il broker: annullali e attendi la conferma",
          details: { field: "broker_link_id" },
        },
        { status: 400 },
      );
    }
    return HttpResponse.json({ id: params.id, broker_link_id: body.broker_link_id });
  }),
  http.post("*/api/portfolios/:id/fee-rule/apply-preset", async ({ request, params }) => {
    applies.push({ id: String(params.id), body: await request.json() });
    return HttpResponse.json({ rules: [], created: [], deactivated: [], preset: {} });
  }),
  http.get("*/api/broker-links/:id/reconciliations", () => HttpResponse.json(reconciliations)),
  http.post("*/api/broker-links/:id/reconcile", () => {
    reconciles += 1;
    return HttpResponse.json({ reconciliations: [] });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  patches = [];
  creates = [];
  puts = [];
  reconciles = 0;
  applies = [];
});
afterAll(() => server.close());

describe("Brokers", () => {
  it("shows the paper-only note and the masked keys, never the secrets", async () => {
    renderApp("/broker");
    expect(await screen.findByRole("note")).toHaveTextContent("Solo conti paper/demo: nessun denaro reale");
    const card = await screen.findByLabelText("Broker Alpaca prova");
    expect(within(card).getByText("Alpaca Paper", { exact: false })).toBeInTheDocument();
    expect(within(card).getByText("Salvata: ••••9999")).toBeInTheDocument();
    expect(within(card).getByLabelText("Secret key")).toHaveValue("");
    expect(within(card).getByText("Operativo")).toBeInTheDocument();
  });

  it("sends only the typed secret", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Broker Alpaca prova");
    await user.type(within(card).getByLabelText("Secret key"), "NEW-SECRET-7777");
    await user.click(within(card).getByRole("button", { name: "Salva chiavi" }));
    await waitFor(() => expect(patches).toEqual([{ config: { secret_key: "NEW-SECRET-7777" } }]));
    await waitFor(() => expect(within(card).getByLabelText("Secret key")).toHaveValue(""));
  });

  it("creates a Trading 212 Demo link with an Idempotency-Key", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderApp("/broker");
    await user.click(await screen.findByRole("button", { name: "Nuovo collegamento" }));
    const form = screen.getByRole("form", { name: "Nuovo collegamento broker" });
    await user.type(within(form).getByLabelText("Nome"), "T212");
    await user.selectOptions(within(form).getByLabelText("Broker"), "t212_demo");
    await user.type(within(form).getByLabelText("API key"), "T212KEY");
    await user.click(within(form).getByRole("button", { name: "Crea collegamento" }));
    await waitFor(() => expect(creates).toHaveLength(1));
    expect(creates[0].body).toEqual({ name: "T212", kind: "t212_demo", config: { api_key: "T212KEY" } });
    expect(creates[0].key).toBeTruthy();
    await waitFor(() => expect(screen.queryByRole("form", { name: "Nuovo collegamento broker" })).not.toBeInTheDocument());
    // M6: the typed key is not kept in the mutation cache
    await waitFor(() => expect(JSON.stringify(queryClient.getMutationCache().getAll().map((m) => m.state.variables))).not.toContain("T212KEY"));
  });

  it("etoro demo kind with two keys and guide", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    await user.click(await screen.findByRole("button", { name: "Nuovo collegamento" }));
    const form = screen.getByRole("form", { name: "Nuovo collegamento broker" });
    expect(within(form).queryByRole("note", { name: "Guida eToro" })).not.toBeInTheDocument();
    await user.type(within(form).getByLabelText("Nome"), "eToro");
    await user.selectOptions(within(form).getByLabelText("Broker"), "etoro_demo");
    expect(within(form).getByRole("option", { name: "eToro — prova" })).toBeInTheDocument();
    const guide = within(form).getByRole("note", { name: "Guida eToro" });
    expect(guide).toHaveTextContent("Demo");
    expect(guide).toHaveTextContent("azzera il portafoglio virtuale dall'app eToro");
    expect(guide).toHaveTextContent("le regole di commissione del portale non si applicano");
    expect(within(form).queryByLabelText("API secret")).not.toBeInTheDocument();
    await user.type(within(form).getByLabelText("API key"), "ETKEY");
    await user.type(within(form).getByLabelText("User key"), "ETUSER");
    await user.click(within(form).getByRole("button", { name: "Crea collegamento" }));
    await waitFor(() => expect(creates).toHaveLength(1));
    expect(creates[0].body).toEqual({
      name: "eToro",
      kind: "etoro_demo",
      config: { api_key: "ETKEY", user_key: "ETUSER" },
    });
  });

  it("tests the connection and shows the failure", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Broker Alpaca prova");
    await user.click(within(card).getByRole("button", { name: "Prova connessione" }));
    expect(await within(card).findByRole("status")).toHaveTextContent("Test fallito: RuntimeError: alpaca_paper: HTTP 401: error");
  });

  it("links a free portfolio and shows the refusal to unlink", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Broker Alpaca prova");
    const select = within(card).getByLabelText("Collega un portafoglio");
    await waitFor(() => expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Seleziona…", "Beta"]));
    await user.selectOptions(select, "p2");
    await user.click(within(card).getByRole("button", { name: "Collega" }));
    await waitFor(() => expect(puts).toEqual([{ id: "p2", body: { broker_link_id: "b1" } }]));
    // the broker's fee preset, checked by default, applied after the link (a separate request)
    await waitFor(() => expect(applies).toEqual([{ id: "p2", body: { broker_kind: "alpaca_paper" } }]));

    await user.click(within(card).getByRole("button", { name: "Scollega Alfa" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent("ordini in corso presso il broker");
  });

  it("offers the broker's fees on link and keeps the current rule when unchecked", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Broker Alpaca prova");
    const box = await within(card).findByRole("checkbox", { name: /Usa le commissioni di Alpaca/ });
    expect(box).toBeChecked();
    const entries = within(card).getByRole("list", { name: "Commissioni di Alpaca" });
    expect(within(entries).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "Azioni ed ETF (e altre classi): commissione 0% · cambio valuta 0% (listino pubblico, da verificare)",
      "Crypto: commissione 0,25% · cambio valuta 0% (fonte non ufficiale, da verificare)",
    ]);
    await user.click(box);
    const select = within(card).getByLabelText("Collega un portafoglio");
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(select, "p2");
    await user.click(within(card).getByRole("button", { name: "Collega" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(applies).toEqual([]);
  });

  it("highlights the reconciliation differences and reconciles on demand", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    const table = await screen.findByRole("table", { name: "Riconciliazioni Alpaca prova" });
    const bad = (await within(table).findByText("Differenze")).closest("tr") as HTMLElement;
    const diff = within(bad).getByText("AAPL", { exact: false }).closest("li") as HTMLElement;
    expect(diff).toHaveAttribute("data-diff");
    expect(diff).toHaveClass("text-loss");
    expect(diff).toHaveTextContent("AAPL: 10 / 7");
    expect(within(table).getByText("Allineato")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Riconcilia ora" }));
    await waitFor(() => expect(reconciles).toBe(1));
  });
});
