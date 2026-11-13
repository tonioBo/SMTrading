import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Portfolio } from "@/api/types";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));
// the detail page opened after a creation draws the equity chart
vi.mock("lightweight-charts", () => {
  const chart = { addAreaSeries: () => ({ setData: vi.fn() }), timeScale: () => ({ fitContent: vi.fn() }), remove: vi.fn() };
  return { createChart: vi.fn(() => chart), ColorType: { Solid: "solid" } };
});

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
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/portfolios", () =>
    HttpResponse.json([
      portfolio("p1", "Alfa", { equity_eur: "12345.6", day_pnl_eur: "-50", day_pnl_pct: "-0.4", leverage: "2.5" }),
      portfolio("p2", "Beta", { status: "reduce_only", equity_eur: null, day_pnl_eur: null, valuation_error: "NO_QUOTE" }),
    ]),
  ),
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
    return HttpResponse.json(portfolio("p3", body.name));
  }),
  http.get("*/api/portfolios/p3", () => HttpResponse.json(portfolio("p3", "Gamma"))),
  http.get("*/api/portfolios/p3/*", () => HttpResponse.json({}, { status: 404 })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  created = [];
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
    expect(within(table).getByText("Solo riduzione")).toBeInTheDocument();
    expect(within(table).getByText("valutazione non disponibile")).toBeInTheDocument();
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
    });
  });

  it("validates the form locally before posting", async () => {
    const user = userEvent.setup();
    renderApp("/portafogli");
    await user.click(await screen.findByRole("button", { name: "Nuovo portafoglio" }));
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Crea" }));
    expect(within(dialog).getByText("Il nome è obbligatorio")).toBeInTheDocument();
    expect(within(dialog).getByText("Importo non valido")).toBeInTheDocument();
    expect(created).toEqual([]);
  });
});
