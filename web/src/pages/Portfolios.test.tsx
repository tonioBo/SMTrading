import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Portfolio } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));
// the detail page opened after a creation draws the equity chart

function portfolio(id: string, name: string, extra: Partial<Portfolio> = {}): Portfolio {
  return {
    id,
    name,
    base_currency: "EUR",
    leverage: "1",
    maintenance_margin_pct: "50",
    warning_margin_pct: "75",
    slippage_bps: 0,
    status: "active",
    mode: "simulazione",
    broker_kind: null,
    mode_label: "SMTrading (simulato)",
    timezone: "Europe/Rome",
    risk_profile_id: null,
    reduce_only_reason: null,
    reduce_only_until: null,
    created_at: "2026-11-01T10:00:00Z",
    equity_eur: "1000",
    day_pnl_eur: "0",
    day_pnl_pct: "0",
    valuation_error: null,
    ...extra,
  };
}

let created: unknown[] = [];
let actions: string[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/portfolios", ({ request }) =>
    HttpResponse.json([
      portfolio("p1", "Alfa", { code: "ALF", equity_eur: "12345.6", day_pnl_eur: "-50", day_pnl_pct: "-0.4", leverage: "2.5" }),
      portfolio("p2", "Beta", { status: "reduce_only", equity_eur: null, day_pnl_eur: null, valuation_error: "NO_QUOTE", in_totals: false }),
      ...(new URL(request.url).searchParams.get("include_archived") === "1"
        ? [portfolio("p9", "Vecchio", { status: "archived", archived_at: "2026-11-13T10:00:00Z", archived_by: "admin@example.com" })]
        : []),
    ]),
  ),
  http.post("*/api/portfolios/:id/archive", ({ params }) => {
    actions.push(`archive ${String(params.id)}`);
    if (params.id === "p2") {
      return HttpResponse.json(
        { code: "INVALID_ORDER", message: "Annulla prima gli ordini aperti", details: { portfolio_id: "p2" } },
        { status: 400 },
      );
    }
    return HttpResponse.json(portfolio(String(params.id), "Alfa", { status: "archived" }));
  }),
  http.post("*/api/portfolios/:id/unarchive", ({ params }) => {
    actions.push(`unarchive ${String(params.id)}`);
    return HttpResponse.json(portfolio(String(params.id), "Vecchio"));
  }),
  http.get("*/api/risk-profiles", () =>
    HttpResponse.json([{ id: "r1", name: "Prudente", allowed_symbols: [], allowed_asset_classes: [], allowed_markets: [], short_allowed: true }]),
  ),
  http.post("*/api/portfolios", async ({ request }) => {
    const body = (await request.json()) as { name: string };
    created.push(body);
    if (body.name === "Alfa") {
      return HttpResponse.json(
        { code: "INVALID_ORDER", message: "esiste già un portafoglio con questo nome", details: { field: "name" } },
        { status: 400 },
      );
    }
    const warned = /demo/i.test(body.name);
    return HttpResponse.json({
      ...portfolio("p3", body.name),
      name_warning: warned ? "gli agenti vedranno questo nome" : null,
    });
  }),
  http.get("*/api/portfolios/p3", () => HttpResponse.json(portfolio("p3", "Gamma"))),
  http.get("*/api/portfolios/p3/*", () => HttpResponse.json({}, { status: 404 })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  created = [];
  actions = [];
});
afterAll(() => server.close());

describe("Portfolios", () => {
  it("lists portfolios with equity, day P&L, status and leverage", async () => {
    renderApp("/portafogli");
    const table = await screen.findByRole("table", { name: "Portafogli" });
    expect(await within(table).findByRole("link", { name: "Alfa" })).toHaveAttribute("href", "/portafogli/p1");
    expect(within(table).getByText("12.345,60 €")).toBeInTheDocument();
    expect(within(table).getByText("-50,00 €")).toHaveClass("loss");
    expect(within(table).getByText("2,50×")).toBeInTheDocument();
    expect(within(table).getByText("ALF").closest("tr")).toHaveTextContent("Alfa");
    expect(within(table).getByText("Solo riduzione")).toBeInTheDocument();
    expect(within(table).getByText("valutazione non disponibile")).toBeInTheDocument();
    // only «Beta» is left out of the totals
    expect(within(table).getAllByText("fuori dai totali")).toHaveLength(1);
    expect(within(table).getByText("fuori dai totali").closest("tr")).toHaveTextContent("Beta");
  });

  it("sorts by name when the header is clicked", async () => {
    const user = userEvent.setup();
    renderApp("/portafogli");
    const table = await screen.findByRole("table", { name: "Portafogli" });
    await within(table).findByText("Alfa");
    await user.click(within(table).getByRole("button", { name: "Nome" }));
    await user.click(within(table).getByRole("button", { name: "Nome" }));
    const names = within(table).getAllByRole("link").map((a) => a.textContent);
    expect(names).toEqual(["Beta", "Alfa"]);
  });

  it("shows a duplicate name error next to the field, then creates the portfolio", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/portafogli");
    await user.click(await screen.findByRole("button", { name: "Nuovo portafoglio" }));
    const dialog = screen.getByRole("dialog");
    await within(dialog).findByRole("option", { name: "Prudente" });
    const name = within(dialog).getByLabelText("Nome");
    await user.type(name, "Alfa");
    await user.type(within(dialog).getByLabelText("Saldo iniziale"), "10.000");
    expect(within(dialog).getByText("Usa la virgola per i decimali (es. 10.000,00)")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Saldo iniziale"), ",00");
    expect(within(dialog).getByText("= 10.000,00 €")).toBeInTheDocument();
    await user.selectOptions(within(dialog).getByLabelText("Profilo di rischio"), "r1");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(await within(dialog).findByText("esiste già un portafoglio con questo nome")).toBeInTheDocument();
    expect(name).toHaveAttribute("aria-invalid", "true");

    await user.clear(name);
    await user.type(name, "Gamma");
    await user.type(within(dialog).getByLabelText("Codice (per gli agenti)"), "gm1");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/portafogli/p3"));
    expect(created.at(-1)).toEqual({
      name: "Gamma",
      initial_cash: "10000.00",
      base_currency: "EUR",
      leverage: "1",
      slippage_bps: 0,
      timezone: "Europe/Rome",
      risk_profile_id: "r1",
      code: "GM1",
    });
  });

  it("shows name warning after save", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/portafogli");
    await user.click(await screen.findByRole("button", { name: "Nuovo portafoglio" }));
    const dialog = screen.getByRole("dialog");
    await user.type(within(dialog).getByLabelText("Nome"), "Demo 2");
    await user.type(within(dialog).getByLabelText("Saldo iniziale"), "1000");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    // saved (never blocked), the warning stays under the name until the admin moves on
    expect(await within(dialog).findByText("gli agenti vedranno questo nome")).toHaveClass("text-warn");
    expect(created).toHaveLength(1);
    expect(router.state.location.pathname).toBe("/portafogli");
    expect(within(dialog).queryByRole("button", { name: "Crea" })).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Apri portafoglio" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/portafogli/p3"));
  });

  it("validates the form locally before posting", async () => {
    const user = userEvent.setup();
    renderApp("/portafogli");
    await user.click(await screen.findByRole("button", { name: "Nuovo portafoglio" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(within(dialog).getByText("Il nome è obbligatorio")).toBeInTheDocument();
    expect(within(dialog).getByText("Importo non valido")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Codice (per gli agenti)"), "A-B");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(within(dialog).getByText("Da 2 a 6 lettere maiuscole o cifre")).toBeInTheDocument();
    expect(created).toEqual([]);
  });

  it("hides archived portfolios until 'Mostra archiviati' is ticked", async () => {
    const user = userEvent.setup();
    renderApp("/portafogli");
    const table = await screen.findByRole("table", { name: "Portafogli" });
    await within(table).findByText("Alfa");
    expect(within(table).queryByText("Vecchio")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Mostra archiviati"));
    expect(await within(table).findByText("Vecchio")).toBeInTheDocument();
    expect(within(table).getByText("Archiviato")).toBeInTheDocument();
  });

  it("archives after confirmation and shows the server refusal", async () => {
    const user = userEvent.setup();
    renderApp("/portafogli");
    const table = await screen.findByRole("table", { name: "Portafogli" });
    await user.click(await within(table).findByRole("button", { name: "Archivia Beta" }));
    let dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Archiviare Beta?")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Archivia" }));
    expect(await within(dialog).findByText("Annulla prima gli ordini aperti")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Annulla" }));

    await user.click(within(table).getByRole("button", { name: "Archivia Alfa" }));
    dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Archivia" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(actions).toEqual(["archive p2", "archive p1"]);
  });

  it("restores an archived portfolio after confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/portafogli");
    await user.click(await screen.findByLabelText("Mostra archiviati"));
    const table = screen.getByRole("table", { name: "Portafogli" });
    await user.click(await within(table).findByRole("button", { name: "Ripristina Vecchio" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Ripristina" }));
    await waitFor(() => expect(actions).toEqual(["unarchive p9"]));
  });
});
