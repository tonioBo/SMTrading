import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { LiveLink } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const liveLink = (over: Partial<LiveLink> = {}): LiveLink => ({
  id: "l1",
  name: "T212 vero",
  kind: "t212_live",
  live_state: "armed",
  armed_at: "2026-11-14T10:00:00Z",
  armed_by: "admin@example.com",
  disarmed_at: null,
  disarmed_reason: null,
  key_masked: "••••0001",
  account_masked: "••••1234",
  snapshot: null,
  last_test_at: "2026-11-14T10:00:00Z",
  health_failures: 0,
  portfolios: [
    { id: "p1", name: "Alfa", status: "active", base_currency: "EUR", quota: "600.00", positions_count: 0 },
    { id: "p2", name: "Beta", status: "reduce_only", base_currency: "USD", quota: null, positions_count: 2 },
  ],
  currency: "EUR",
  broker_cash: "1000",
  quotas_total: "1100.00",
  unassigned: "-100.00",
  ...over,
});

const pf = (id: string, name: string, over: Record<string, unknown> = {}) => ({
  id,
  name,
  status: "active",
  mode: "simulazione",
  live_state: null,
  mode_label: "SMTrading (simulato)",
  broker_link_id: null,
  base_currency: "EUR",
  risk_profile_id: null,
  ...over,
});

let me = { email: "admin@example.com", totp_enabled: true, role: "admin", must_change_password: false };
let links: LiveLink[] = [];
let puts: { id: string; body: unknown }[] = [];
let putReply: () => Response = () => HttpResponse.json({});
let applies: { id: string; body: unknown }[] = [];
let feeRules: Record<string, unknown>[] = [];
const rule = (name: string, over: Record<string, unknown> = {}) => ({
  id: name, name, scope: "portfolio", scope_ref: "p3", asset_class: null, side: "both", currency: "EUR",
  fixed: "0", pct: "0", min_fee: null, max_fee: null, tiers: null, fx_pct: "0", priority: 0, active: true,
  created_at: "2026-11-01T10:00:00Z", ...over,
});

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json(me)),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/broker-links", () => HttpResponse.json([])),
  http.get("*/api/portfolios", () =>
    HttpResponse.json([
      pf("p1", "Alfa", { mode: "reale", live_state: "armed", mode_label: "🔴 Trading 212 · reale", broker_link_id: "l1" }),
      pf("p2", "Beta", { mode: "reale", live_state: "armed", mode_label: "🔴 Trading 212 · reale", broker_link_id: "l1" }),
      pf("p3", "Libero"),
      pf("p4", "Vecchio", { archived_at: "2026-11-01T10:00:00Z", status: "archived" }),
      pf("p5", "Paper", { broker_link_id: "b9", mode: "paper" }),
    ]),
  ),
  http.get("*/api/live-links", () => HttpResponse.json(links)),
  http.get("*/api/risk-profiles", () => HttpResponse.json([{ id: "r0", name: "Default" }])),
  http.put("*/api/portfolios/:id/live-link", async ({ request, params }) => {
    puts.push({ id: String(params.id), body: await request.json() });
    return putReply();
  }),
  http.get("*/api/fee-rules", () => HttpResponse.json(feeRules)),
  http.post("*/api/portfolios/:id/fee-rule/apply-preset", async ({ request, params }) => {
    applies.push({ id: String(params.id), body: await request.json() });
    return HttpResponse.json({ rules: [], created: [], deactivated: [], preset: {} });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  me = { email: "admin@example.com", totp_enabled: true, role: "admin", must_change_password: false };
  links = [];
  puts = [];
  putReply = () => HttpResponse.json({});
  applies = [];
  feeRules = [];
});
afterAll(() => server.close());

async function section() {
  const card = await screen.findByLabelText("Denaro reale T212 vero");
  return within(card).findByRole("region", { name: "Portafogli collegati" });
}

describe("LiveLinkPortfolios", () => {
  it("lists linked portfolios with quota and unassigned", async () => {
    links = [liveLink()];
    renderApp("/broker");
    const box = await section();
    const rows = within(box).getAllByRole("row");
    expect(within(rows[0]).getAllByRole("columnheader").map((c) => c.textContent)).toEqual(["Portafoglio", "Stato", "Quota", "Azioni"]);
    expect(rows[1]).toHaveTextContent("Alfa");
    await waitFor(() => expect(within(rows[1]).getByLabelText(/Modalità: 🔴 Trading 212 · reale/)).toBeInTheDocument());
    expect(rows[1]).toHaveTextContent("Attivo");
    expect(rows[1]).toHaveTextContent("600,00 €");
    expect(rows[2]).toHaveTextContent("Beta");
    expect(rows[2]).toHaveTextContent("Solo riduzione");
    expect(within(rows[2]).getByRole("cell", { name: "—" })).toBeInTheDocument();
    expect(box).toHaveTextContent("Contante al broker: 1.000,00 €");
    expect(box).toHaveTextContent("Assegnato ai portafogli: 1.100,00 €");
    const free = within(box).getByText(/Non assegnato:/);
    expect(free).toHaveTextContent("Non assegnato: -100,00 €");
    expect(free).toHaveTextContent("le quote superano il contante del conto");
    expect(free.className).toContain("text-loss");
  });

  it("shows a non-negative unassigned amount without the warning", async () => {
    links = [liveLink({ quotas_total: "600.00", unassigned: "400.00" })];
    renderApp("/broker");
    const box = await section();
    const free = within(box).getByText(/Non assegnato:/);
    expect(free).toHaveTextContent("Non assegnato: 400,00 €");
    expect(free).not.toHaveTextContent("superano");
    expect(free.className).not.toContain("text-loss");
  });

  it("link another portfolio asks totp", async () => {
    links = [liveLink()];
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    await user.click(within(box).getByRole("button", { name: "Collega un portafoglio" }));
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a T212 vero?" });
    const select = within(dialog).getByLabelText("Portafoglio da collegare");
    await waitFor(() => expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Seleziona…", "Libero"]));
    const confirm = within(dialog).getByRole("button", { name: "Collega" });
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    expect(confirm).toBeDisabled(); // no portfolio chosen yet
    await user.selectOptions(select, "p3");
    await user.click(confirm);
    await waitFor(() => expect(puts).toEqual([{ id: "p3", body: { link_id: "l1", totp: "123456" } }]));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // the broker's fee preset (checked by default): a separate request after the link
    await waitFor(() => expect(applies).toEqual([{ id: "p3", body: { broker_kind: "t212_live" } }]));
  });

  it("link keeps the current fee rule when the preset is unchecked", async () => {
    links = [liveLink()];
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    await user.click(within(box).getByRole("button", { name: "Collega un portafoglio" }));
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a T212 vero?" });
    const check = await within(dialog).findByRole("checkbox", { name: /Usa le commissioni di Trading 212/ });
    expect(check).toBeChecked();
    expect(dialog).toHaveTextContent("Tutte le classi: commissione 0% · cambio valuta 0,15% (più fonti concordi)");
    await user.click(check);
    await waitFor(() => expect(within(dialog).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(within(dialog).getByLabelText("Portafoglio da collegare"), "p3");
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Collega" }));
    await waitFor(() => expect(puts).toHaveLength(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(applies).toEqual([]);
  });

  it("fee preset off by default when the portfolio has its own rules; lists what it deactivates", async () => {
    links = [liveLink()];
    feeRules = [rule("Libero – mia regola"), rule("Libero – vecchia", { active: false }), rule("Altro", { scope_ref: "p9" })];
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    await user.click(within(box).getByRole("button", { name: "Collega un portafoglio" }));
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a T212 vero?" });
    await waitFor(() => expect(within(dialog).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(within(dialog).getByLabelText("Portafoglio da collegare"), "p3");
    const check = within(dialog).getByRole("checkbox", { name: /Usa le commissioni di Trading 212/ });
    await waitFor(() => expect(check).not.toBeChecked());
    expect(dialog).toHaveTextContent("Vengono disattivate: «Libero – mia regola»");
    expect(dialog).not.toHaveTextContent("vecchia");
  });

  it("fee preset on by default over the seeded zero-commission rule", async () => {
    links = [liveLink()];
    feeRules = [rule("Libero – zero commissioni")];
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    await user.click(within(box).getByRole("button", { name: "Collega un portafoglio" }));
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a T212 vero?" });
    await waitFor(() => expect(within(dialog).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(within(dialog).getByLabelText("Portafoglio da collegare"), "p3");
    expect(await within(dialog).findByText(/Vengono disattivate: «Libero – zero commissioni»/)).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: /Usa le commissioni di Trading 212/ })).toBeChecked();
  });

  it("a refused link applies no fee preset", async () => {
    links = [liveLink()];
    putReply = () => HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP non valido", details: { field: "totp" } }, { status: 403 });
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    await user.click(within(box).getByRole("button", { name: "Collega un portafoglio" }));
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a T212 vero?" });
    await waitFor(() => expect(within(dialog).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(within(dialog).getByLabelText("Portafoglio da collegare"), "p3");
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Collega" }));
    expect(await within(dialog).findByText("codice TOTP non valido")).toBeInTheDocument();
    expect(applies).toEqual([]);
  });

  it("quota error shown in dialog", async () => {
    links = [liveLink({ quotas_total: "600.00", unassigned: "400.00" })];
    putReply = () =>
      HttpResponse.json(
        {
          code: "VALIDATION_ERROR",
          message: "la somma delle quote dei portafogli supera la liquidità disponibile al broker",
          details: { field: "cash", quotas_total: "1100.00", broker_cash: "1000", unassigned: "-100.00" },
        },
        { status: 422 },
      );
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    await user.click(within(box).getByRole("button", { name: "Collega un portafoglio" }));
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a T212 vero?" });
    await waitFor(() => expect(within(dialog).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(within(dialog).getByLabelText("Portafoglio da collegare"), "p3");
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Collega" }));
    expect(await within(dialog).findByText("la somma delle quote dei portafogli supera la liquidità disponibile al broker")).toBeInTheDocument();
    expect(within(dialog).getByText(/Quote con questo portafoglio: 1\.100,00 € · contante al broker: 1\.000,00 € · non assegnato: -100,00 €/)).toBeInTheDocument();
  });

  it("reconcile error shown in dialog", async () => {
    links = [liveLink()];
    putReply = () =>
      HttpResponse.json(
        {
          code: "VALIDATION_ERROR",
          message: "riconciliazione non in regola: verifica il conto prima di collegare un altro portafoglio",
          details: { field: "reconcile" },
        },
        { status: 422 },
      );
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    await user.click(within(box).getByRole("button", { name: "Collega un portafoglio" }));
    const dialog = await screen.findByRole("dialog", { name: "Collegare un portafoglio a T212 vero?" });
    await waitFor(() => expect(within(dialog).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(within(dialog).getByLabelText("Portafoglio da collegare"), "p3");
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    await user.click(within(dialog).getByRole("button", { name: "Collega" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("riconciliazione non in regola");
  });

  it("unlink disabled with open positions", async () => {
    links = [liveLink()];
    const user = userEvent.setup();
    renderApp("/broker");
    const box = await section();
    const beta = within(box).getByRole("button", { name: "Scollega Beta" });
    expect(beta).toBeDisabled();
    expect(within(box).getByText("Chiudi prima le posizioni reali")).toBeInTheDocument();
    await user.click(within(box).getByRole("button", { name: "Scollega Alfa" }));
    const dialog = await screen.findByRole("dialog", { name: "Scollegare Alfa da T212 vero?" });
    expect(puts).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Scollega" }));
    await waitFor(() => expect(puts).toEqual([{ id: "p1", body: { link_id: null } }]));
  });

  it("allows linking only in dry run or armed", async () => {
    links = [liveLink({ live_state: "configured", portfolios: [], quotas_total: null, unassigned: null, broker_cash: null })];
    renderApp("/broker");
    const box = await section();
    expect(within(box).getByRole("button", { name: "Collega un portafoglio" })).toBeDisabled();
    expect(within(box).getByText("Nessun portafoglio collegato")).toBeInTheDocument();
    expect(box).toHaveTextContent("Contante al broker: —");
  });

  it("viewer sees the portfolios without link or unlink", async () => {
    me = { email: "vera@example.com", totp_enabled: false, role: "viewer", must_change_password: false };
    links = [liveLink()];
    renderApp("/broker");
    const box = await section();
    expect(box).toHaveTextContent("Alfa");
    expect(box).toHaveTextContent("Non assegnato: -100,00 €");
    expect(within(box).queryByRole("button", { name: "Collega un portafoglio" })).not.toBeInTheDocument();
    expect(within(box).queryByRole("button", { name: /Scollega/ })).not.toBeInTheDocument();
    // disarm stays available to viewers
    const card = screen.getByLabelText("Denaro reale T212 vero");
    expect(within(card).getByRole("button", { name: "Disattiva denaro reale" })).toBeInTheDocument();
  });

  it("arm dialog of a link with two portfolios shows their total quota", async () => {
    links = [liveLink({ live_state: "dry_run", armed_at: null })];
    const user = userEvent.setup();
    renderApp("/broker");
    const card = await screen.findByLabelText("Denaro reale T212 vero");
    await user.click(within(card).getByRole("button", { name: "Attiva denaro reale" }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(dialog).toHaveTextContent("liquidità 1.100,00 € (quote di 2 portafogli)"));
    await waitFor(() => expect(dialog).toHaveTextContent("«Alfa: Default · Beta: Default»"));
  });
});
