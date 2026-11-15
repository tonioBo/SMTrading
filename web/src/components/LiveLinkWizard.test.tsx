import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { LiveLink, LiveTestResult } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const CANARY = "CANARYSECRET-do-not-use";

const liveLink = (over: Partial<LiveLink> = {}): LiveLink => ({
  id: "l1",
  name: "Alpaca vero",
  kind: "alpaca_live",
  live_state: "configured",
  armed_at: null,
  armed_by: null,
  disarmed_at: null,
  disarmed_reason: null,
  key_masked: "••••0001",
  account_masked: "••••1234",
  snapshot: null,
  last_test_at: null,
  health_failures: 0,
  portfolios: [],
  currency: null,
  broker_cash: null,
  quotas_total: null,
  unassigned: null,
  ...over,
});

const testResult = (over: Partial<LiveTestResult> = {}): LiveTestResult => ({
  account_masked: "••••1234",
  account_type: "cash",
  currency: "USD",
  cash_available: "1000",
  positions_count: 0,
  open_orders_count: 0,
  crypto_enabled: false,
  order_types: ["limit", "market"],
  empty: true,
  account_changed: false,
  ...over,
});

let links: LiveLink[] = [];
let result: LiveTestResult = testResult();
let creates: unknown[] = [];
let disarms: { body: string; headers: Record<string, string> }[] = [];
let settled = true;
let linkPuts: unknown[] = [];

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: true })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/broker-links", () =>
    // the paper list also returns the real-money links: they are shown only in their section
    HttpResponse.json([
      {
        id: "l1",
        name: "Alpaca vero",
        kind: "alpaca_live",
        enabled: true,
        status: "unknown",
        last_error: null,
        base_url: "",
        config_masked: { key: "••••0001", secret: "••••" },
        secret_keys: [],
        portfolios: [],
        created_at: "2026-11-14T10:00:00Z",
        live_state: "configured",
      },
    ]),
  ),
  http.get("*/api/broker-links/:id/reconciliations", () => HttpResponse.json([])),
  http.get("*/api/portfolios", () =>
    HttpResponse.json([
      { id: "p1", name: "Reale", broker_link_id: null, base_currency: "USD", risk_profile_id: "r1" },
      { id: "p2", name: "Paper", broker_link_id: "b9" },
    ]),
  ),
  http.get("*/api/live-links", () => HttpResponse.json(links)),
  http.post("*/api/live-links", async ({ request }) => {
    const body = (await request.json()) as { name: string };
    creates.push(body);
    links = [liveLink({ name: body.name })];
    return HttpResponse.json(links[0]);
  }),
  http.post("*/api/live-links/:id/test", () => {
    links = [{ ...links[0], snapshot: result, last_test_at: "2026-11-14T10:00:00Z" }];
    return HttpResponse.json(result);
  }),
  http.post("*/api/live-links/:id/dry-run", async ({ request }) => {
    const { on } = (await request.json()) as { on: boolean };
    links = [{ ...links[0], live_state: on ? "dry_run" : "configured" }];
    return HttpResponse.json(links[0]);
  }),
  http.put("*/api/portfolios/:id/live-link", async ({ request }) => {
    linkPuts.push(await request.json());
    return HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP non valido", details: { field: "totp" } }, { status: 403 });
  }),
  http.post("*/api/live-links/:id/disarm", async ({ request }) => {
    disarms.push({ body: await request.text(), headers: Object.fromEntries(request.headers.entries()) });
    links = [{ ...links[0], live_state: "configured", armed_at: null, disarmed_at: "2026-11-14T11:00:00Z", disarmed_reason: "disattivato dall'admin" }];
    return HttpResponse.json({ ...links[0], orders: settled ? { canceled: 0 } : null, settled });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  links = [];
  result = testResult();
  creates = [];
  disarms = [];
  settled = true;
  linkPuts = [];
});
afterAll(() => server.close());

describe("LiveLinkWizard", () => {
  it("creates a real-money link with the red kinds and the T212 permission guide, never keeping the keys", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderApp("/broker");
    await user.click(await screen.findByRole("button", { name: "Nuovo collegamento reale" }));
    const form = screen.getByRole("form", { name: "Nuovo collegamento denaro reale" });
    const alpaca = within(form).getByLabelText("Alpaca — denaro reale");
    const t212 = within(form).getByLabelText("Trading 212 — denaro reale");
    expect(alpaca.closest("label")).toHaveClass("border-loss");
    expect(t212.closest("label")).toHaveClass("border-loss");
    await user.click(t212);
    expect(within(form).getByText("Pies - Write", { exact: false })).toBeInTheDocument();
    expect(within(form).getByText(/Restrizione IP consigliata/)).toBeInTheDocument();
    expect(within(form).getByText("Ordini: esecuzione (Orders - Execute)")).toBeInTheDocument();
    await user.type(within(form).getByLabelText("Nome"), "T212 vero");
    const key = within(form).getByLabelText("API key");
    expect(key).toHaveAttribute("type", "password");
    await user.type(key, "CANARYKEYID0001");
    await user.type(within(form).getByLabelText("API secret"), CANARY);
    await user.click(within(form).getByRole("button", { name: "Salva chiavi" }));
    await waitFor(() => expect(creates).toEqual([{ name: "T212 vero", kind: "t212_live", api_key: "CANARYKEYID0001", api_secret: CANARY }]));
    const card = await screen.findByLabelText("Denaro reale T212 vero");
    expect(within(card).getByText("••••0001")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(CANARY);
    await waitFor(() => expect(JSON.stringify(queryClient.getMutationCache().getAll().map((m) => m.state.variables))).not.toContain(CANARY));
    // the paper list does not show the real-money link as a paper card
    expect(screen.queryByLabelText("Broker Alpaca vero")).not.toBeInTheDocument();
  });

  it("wizard offers etoro live with key guide", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    await user.click(await screen.findByRole("button", { name: "Nuovo collegamento reale" }));
    const form = screen.getByRole("form", { name: "Nuovo collegamento denaro reale" });
    const etoro = within(form).getByLabelText("eToro — denaro reale");
    expect(etoro.closest("label")).toHaveClass("border-loss");
    await user.click(etoro);
    expect(within(form).getByText("Real", { selector: "strong" })).toBeInTheDocument();
    expect(within(form).getByText("Write", { selector: "strong" })).toBeInTheDocument();
    expect(within(form).getByText(/Non riusare la chiave del collegamento demo/)).toBeInTheDocument();
    expect(within(form).getByText(/senza posizioni, ordini o copy trading/)).toBeInTheDocument();
    expect(within(form).getByRole("link", { name: "portale API di eToro" })).toHaveAttribute(
      "href",
      "https://api-portal.etoro.com/core/getting-started/authentication.md",
    );
    expect(within(form).queryByLabelText(/Cripto/)).not.toBeInTheDocument();
    await user.type(within(form).getByLabelText("Nome"), "eToro vero");
    const api = within(form).getByLabelText("Chiave API (x-api-key)");
    const userKey = within(form).getByLabelText("Chiave utente reale (x-user-key)");
    expect(api).toHaveAttribute("type", "password");
    expect(userKey).toHaveAttribute("type", "password");
    await user.type(api, "CANARYKEYID0001");
    await user.type(userKey, CANARY);
    await user.click(within(form).getByRole("button", { name: "Salva chiavi" }));
    await waitFor(() =>
      expect(creates).toEqual([{ name: "eToro vero", kind: "etoro_live", api_key: "CANARYKEYID0001", user_key: CANARY }]),
    );
    expect(document.body.textContent).not.toContain(CANARY);
  });

  it("wizard shows read-only test result and requires empty account", async () => {
    links = [liveLink()];
    result = testResult({ positions_count: 2, open_orders_count: 1, empty: false });
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    expect(within(card).getByText("Test non ancora eseguito")).toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Esegui il test in sola lettura" }));
    const view = await within(card).findByLabelText("Risultato del test");
    expect(view).toHaveTextContent("Conto••••1234");
    expect(view).toHaveTextContent("Tipo di contocash");
    expect(view).toHaveTextContent("ValutaUSD");
    expect(view).toHaveTextContent("Liquidità disponibile1.000,00 $");
    expect(view).toHaveTextContent("Posizioni aperte2");
    expect(view).toHaveTextContent("Ordini aperti1");
    expect(view).toHaveTextContent("Criptono");
    expect(view).toHaveTextContent("Tipi d'ordinelimit, market");
    expect(within(card).getByText(/zero posizioni e zero ordini aperti/, { selector: "p[role=alert]" })).toBeInTheDocument();

    result = testResult();
    await user.click(within(card).getByRole("button", { name: "Esegui il test in sola lettura" }));
    expect(await within(card).findByText("Conto vuoto: test superato")).toBeInTheDocument();
    expect(within(card).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("starts the dry run and asks the TOTP to link a portfolio", async () => {
    links = [liveLink({ snapshot: testResult(), last_test_at: "2026-11-14T10:00:00Z" })];
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    expect(within(card).getByRole("button", { name: "Collega un portafoglio" })).toBeDisabled();
    await user.click(within(card).getByRole("button", { name: "Avvia la prova a secco" }));
    expect(await within(card).findByRole("button", { name: "Termina la prova a secco" })).toBeInTheDocument();
    const open = within(card).getByRole("button", { name: "Collega un portafoglio" });
    await waitFor(() => expect(open).toBeEnabled());
    await user.click(open);
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a Alpaca vero?" });
    const select = within(dialog).getByLabelText("Portafoglio da collegare");
    await waitFor(() => expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Seleziona…", "Reale"]));
    await user.selectOptions(select, "p1");
    const confirm = within(dialog).getByRole("button", { name: "Collega" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    await user.click(confirm);
    await waitFor(() => expect(linkPuts).toEqual([{ link_id: "l1", totp: "123456" }]));
    expect(await within(dialog).findByText("codice TOTP non valido")).toBeInTheDocument();
  });

  it("disarm is one click without totp", async () => {
    links = [liveLink({ live_state: "armed", armed_at: "2026-11-14T10:00:00Z", armed_by: "admin@example.com" })];
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    const button = within(card).getByRole("button", { name: "Disattiva denaro reale" });
    expect(button.className).toContain("bg-loss");
    expect(within(card).getByRole("button", { name: "Attiva denaro reale" })).toBeDisabled();
    await user.click(button);
    await waitFor(() => expect(disarms).toHaveLength(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); // no confirmation step
    expect(disarms[0].body).not.toMatch(/totp/i);
    expect(disarms[0].headers["content-type"]).toBe("application/json");
    await waitFor(() => expect(within(card).queryByRole("button", { name: "Disattiva denaro reale" })).not.toBeInTheDocument());
    expect(within(card).getByText(/disattivato dall'admin/)).toBeInTheDocument();
  });

  it("keeps a retry button and warns when the orders could not be settled", async () => {
    links = [liveLink({ live_state: "armed" })];
    settled = false;
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    await user.click(within(card).getByRole("button", { name: "Disattiva denaro reale" }));
    expect(await within(card).findByRole("alert")).toHaveTextContent("Disattivato, ordini da sistemare: riprova");
    settled = true;
    await user.click(await within(card).findByRole("button", { name: "Ripeti disattivazione / sistema ordini" }));
    await waitFor(() => expect(disarms).toHaveLength(2));
    await waitFor(() => expect(within(card).queryByText("Disattivato, ordini da sistemare: riprova")).not.toBeInTheDocument());
  });

  it("offers the idempotent retry on a configured link, e.g. after a reload", async () => {
    links = [liveLink({ live_state: "configured", disarmed_at: "2026-11-14T11:00:00Z", disarmed_reason: "disattivato dall'admin" })];
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    expect(within(card).queryByRole("button", { name: "Disattiva denaro reale" })).not.toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Ripeti disattivazione / sistema ordini" }));
    await waitFor(() => expect(disarms).toHaveLength(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows no disarm or retry on a link that was never armed", async () => {
    links = [liveLink({ live_state: "configured", armed_at: null, disarmed_at: null })];
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    expect(within(card).queryByRole("button", { name: "Disattiva denaro reale" })).not.toBeInTheDocument();
    expect(within(card).queryByRole("button", { name: "Ripeti disattivazione / sistema ordini" })).not.toBeInTheDocument();
  });

  it("shows the one-click disarm on a dry-run link", async () => {
    links = [liveLink({ live_state: "dry_run" })];
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale Alpaca vero");
    await user.click(within(card).getByRole("button", { name: "Disattiva denaro reale" }));
    await waitFor(() => expect(disarms).toHaveLength(1));
    expect(disarms[0].body).not.toMatch(/totp/i);
  });

  it("uses new-password autocomplete on the key fields", async () => {
    const user = userEvent.setup();
    renderApp("/broker");
    await user.click(await screen.findByRole("button", { name: "Nuovo collegamento reale" }));
    const form = screen.getByRole("form", { name: "Nuovo collegamento denaro reale" });
    expect(within(form).getByLabelText("Key ID")).toHaveAttribute("autocomplete", "new-password");
    expect(within(form).getByLabelText("Secret key")).toHaveAttribute("autocomplete", "new-password");
  });

  it("gives the real-money section an anchor for the banner link", async () => {
    links = [liveLink()];
    renderApp("/broker#denaro-reale");
    await screen.findByLabelText("Denaro reale Alpaca vero");
    expect(document.getElementById("denaro-reale")).toHaveAccessibleName("Denaro reale");
  });
});
