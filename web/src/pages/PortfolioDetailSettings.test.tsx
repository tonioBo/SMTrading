import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { Agent, Portfolio } from "@/api/types";
import { PortfolioSettings } from "@/pages/PortfolioDetailSettings";
import { createQueryClient } from "@/queryClient";
import { commonHandlers } from "@/test/handlers";

const portfolio = {
  id: "p1", name: "Alfa", base_currency: "EUR", leverage: "1", maintenance_margin_pct: "25", warning_margin_pct: "35",
  slippage_bps: 5, status: "active", mode: "paper", timezone: "Europe/Rome", risk_profile_id: null, in_totals: true,
  intraday_enabled: false, auto_rules_enabled: false, intraday_timeframes: ["15m", "1h"], intraday_wake_agent_id: null,
  day_close_minutes: 10, max_day_trades_5d: null, intraday_max_wakes_per_hour: 6,
} as unknown as Portfolio;

const agent = (id: string, name: string, permission: "trade" | "read", active = true) =>
  ({ id, name, active, portfolios: [{ portfolio_id: "p1", portfolio_name: "Alfa", permission }] }) as unknown as Agent;
const agents = [agent("a1", "Trader uno", "trade"), agent("a2", "Solo lettura", "read"), agent("a3", "Spento", "trade", false)];

let role = "admin";
let patches: unknown[] = [];
let patchResponse: () => Response = () => HttpResponse.json(portfolio);
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "a@example.com", role, totp_enabled: false })),
  http.get("*/api/risk-profiles", () => HttpResponse.json([])),
  http.get("*/api/agents", () => HttpResponse.json(agents)),
  http.patch("*/api/portfolios/p1", async ({ request }) => {
    patches.push(await request.json());
    return patchResponse();
  }),
  ...commonHandlers,
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  role = "admin";
  patches = [];
  patchResponse = () => HttpResponse.json(portfolio);
});
afterAll(() => server.close());

function setup(p: Portfolio = portfolio) {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <PortfolioSettings portfolio={p} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { user };
}

describe("PortfolioSettings: lavoro in giornata", () => {
  it("sends the mode A switch as a PATCH", async () => {
    const { user } = setup();
    await user.click(await screen.findByRole("checkbox", { name: "Sveglia dell'agente (modo A)" }));
    await waitFor(() => expect(patches).toEqual([{ intraday_enabled: true }]));
    expect(screen.getByText(/In giornata la leva è al massimo 2 sui conti di prova e 1 sui conti reali/)).toBeInTheDocument();
  });

  it("is not shown to a viewer", async () => {
    role = "viewer";
    setup();
    await screen.findByText("Impostazioni del portafoglio");
    await waitFor(() => expect(screen.queryByText("Lavoro in giornata")).not.toBeInTheDocument());
    expect(screen.queryByRole("checkbox", { name: /modo A/ })).not.toBeInTheDocument();
  });

  it("shows the server refusal of the automatic rules under the switch and leaves it off", async () => {
    patchResponse = () =>
      HttpResponse.json({ code: "INVALID_ORDER", message: "Regole automatiche non attivabili: il conto è in contanti", details: { field: "auto_rules_enabled" } }, { status: 400 });
    const { user } = setup();
    const sw = await screen.findByRole("checkbox", { name: "Regole automatiche (modo B, prove)" });
    await user.click(sw);
    expect(await screen.findByText(/in contanti/)).toBeInTheDocument();
    expect(patches).toEqual([{ auto_rules_enabled: true }]);
    expect(sw).not.toBeChecked();
  });

  it("allows one mode only: the other switch is disabled with an explanation", async () => {
    setup({ ...portfolio, intraday_enabled: true } as Portfolio);
    const b = await screen.findByRole("checkbox", { name: "Regole automatiche (modo B, prove)" });
    expect(b).toBeDisabled();
    expect(screen.getByText(/un portafoglio lavora in un solo modo/i)).toBeInTheDocument();
  });

  it("lists as wake agents only the active ones with trade permission and saves the changed fields", async () => {
    const { user } = setup();
    const select = await screen.findByLabelText("Agente da svegliare");
    await waitFor(() => expect(within(select).getByRole("option", { name: "Trader uno" })).toBeInTheDocument());
    expect(within(select).queryByRole("option", { name: "Solo lettura" })).not.toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: "Spento" })).not.toBeInTheDocument();
    expect(within(select).getByRole("option", { name: "nessuno: solo registrati e avviso" })).toBeInTheDocument();
    await user.selectOptions(select, "a1");
    await user.click(screen.getByRole("checkbox", { name: "5m" }));
    await user.type(screen.getByLabelText("Tetto operazioni di giornata su 5 sedute"), "3");
    await user.click(screen.getByRole("button", { name: "Salva lavoro in giornata" }));
    await waitFor(() =>
      expect(patches).toEqual([{ intraday_timeframes: ["5m", "15m", "1h"], intraday_wake_agent_id: "a1", max_day_trades_5d: 3 }]),
    );
  });

  it("shows the automatic rules' brakes only with the switch on, and saves them as numeric strings (blank = off)", async () => {
    const on = { ...portfolio, auto_rules_enabled: true, auto_brakes: { max_trades_hour: 60, max_trades_day: 300, max_loss_day: null, max_loss_day_pct: "2", max_consecutive_losses: 5, max_open_positions: 5 } } as unknown as Portfolio;
    const { user } = setup(on);
    const section = await screen.findByRole("form", { name: "Freni delle regole automatiche" });
    expect(screen.getByText("Freni delle regole automatiche", { selector: "h3" })).toBeInTheDocument();
    const pct = within(section).getByLabelText("Perdita massima del giorno (%)");
    expect(pct).toHaveValue("2");
    await user.clear(pct);
    await user.type(pct, "1,5");
    await user.clear(within(section).getByLabelText("Posizioni aperte insieme"));
    await user.click(within(section).getByRole("button", { name: "Salva i freni" }));
    await waitFor(() =>
      expect(patches).toEqual([{ auto_brakes: { max_trades_hour: 60, max_trades_day: 300, max_loss_day: null, max_loss_day_pct: "1.5", max_consecutive_losses: 5, max_open_positions: null } }]),
    );
  });

  it("refuses an out-of-range brake in the page", async () => {
    const on = { ...portfolio, auto_rules_enabled: true } as unknown as Portfolio;
    const { user } = setup(on);
    const section = await screen.findByRole("form", { name: "Freni delle regole automatiche" });
    const hour = within(section).getByLabelText("Operazioni all'ora");
    await user.clear(hour);
    await user.type(hour, "500");
    await user.click(within(section).getByRole("button", { name: "Salva i freni" }));
    expect(await within(section).findByText(/Operazioni all'ora: un numero intero/)).toBeInTheDocument();
    expect(patches).toEqual([]);
  });

  it("does not show the brakes with the switch off", async () => {
    setup();
    await screen.findByText("Impostazioni del portafoglio");
    expect(screen.queryByRole("form", { name: "Freni delle regole automatiche" })).not.toBeInTheDocument();
  });
});
