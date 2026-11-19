import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Campaign, CampaignCloseCheck, CampaignTemplate } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const campaign: Campaign = {
  id: "c1", name: "Prova novembre", start_date: "2026-11-02", end_date: "2026-11-30", notes: "prima prova",
  status: "attiva", created_by: "a@example.com", created_at: "2026-11-02T08:00:00Z", closed_at: null, closed_by: null,
  portfolios: [
    { id: "p1", name: "Alfa", status: "active", base_currency: "EUR", created: true, broker_link_name: "Alpaca prova", broker_kind: "alpaca_paper" },
  ],
  goals: [
    {
      id: "g1", name: "campagna", horizon: "custom", start_date: "2026-11-02", end_date: "2026-11-30", recurring: false,
      scope: "portfolio", portfolio_id: "p1", target_pct: "3", kind: "min", benchmark: null, active: true,
      closes_positions: true, created_at: "2026-11-02T08:00:00Z", updated_at: "2026-11-02T08:00:00Z",
      portfolio_name: "Alfa", portfolio_archived: false,
    },
  ],
};

function tpl(key: string, portfolioKey: string, over: Partial<CampaignTemplate> = {}, body = {}): CampaignTemplate {
  return {
    key: `${key}:${portfolioKey}`, label: `${key} · ${portfolioKey}`, portfolio_key: portfolioKey, recommended: false,
    body: {
      name: key, horizon: "custom", start_date: "2026-11-20", end_date: "2026-12-17", recurring: false, scope: "portfolio",
      portfolio_id: null, target_pct: "8", kind: "min", benchmark: null, active: true, closes_positions: false, ...body,
    },
    ...over,
  };
}

const check: CampaignCloseCheck = {
  campaign_id: "c1", status: "attiva", open_positions: 1, open_orders: 0,
  portfolios: [
    {
      id: "p1", name: "Alfa", status: "active", archived: false, broker_link_name: "Alpaca prova",
      positions: [{ symbol: "AAPL", qty: "2" }], open_orders: [], can_archive: false, archive_blocker: "Scollega prima il broker",
    },
    {
      id: "p2", name: "Beta", status: "active", archived: false, broker_link_name: null, positions: [], open_orders: [],
      can_archive: true, archive_blocker: null,
    },
  ],
  plans: [],
};

let role: "admin" | "viewer" = "admin";
let calls: { path: string; body: unknown }[] = [];

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "a@example.com", role, totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/campaigns", () => HttpResponse.json([campaign])),
  http.get("*/api/portfolios", () =>
    HttpResponse.json([
      { id: "p2", name: "Beta", status: "active", mode: "simulazione", mode_label: "SMTrading (simulato)", broker_link_id: null },
      { id: "p9", name: "Reale", status: "active", mode: "reale", mode_label: "Alpaca (reale)", broker_link_id: "l9" },
    ]),
  ),
  http.get("*/api/risk-profiles", () => HttpResponse.json([{ id: "r1", name: "Prudente" }])),
  http.get("*/api/broker-links", () =>
    HttpResponse.json([
      { id: "l1", name: "Alpaca prova", kind: "alpaca_paper", enabled: true, live_state: null, status: "ok", portfolios: [] },
      { id: "l9", name: "Conto vero", kind: "alpaca_live", enabled: true, live_state: "armed", status: "ok", portfolios: [] },
    ]),
  ),
  http.post("*/api/campaigns/templates", async ({ request }) => {
    calls.push({ path: "templates", body: await request.json() });
    return HttpResponse.json([
      tpl("campagna", "new-1", { recommended: true }, { closes_positions: true }),
      tpl("rischio campagna", "new-1", { recommended: true }, { kind: "max_loss", target_pct: "10" }),
      tpl("campagna", "p2", { recommended: true }),
      tpl("mensile", "p2", {}, { horizon: "month", recurring: true }),
    ]);
  }),
  http.post("*/api/campaigns", async ({ request }) => {
    calls.push({ path: "create", body: await request.json() });
    return HttpResponse.json({ ...campaign, id: "c2", name: "Prova dicembre" });
  }),
  http.get("*/api/campaigns/:id/close-check", () => HttpResponse.json(check)),
  http.post("*/api/campaigns/:id/close", async ({ request }) => {
    calls.push({ path: "close", body: await request.json() });
    return HttpResponse.json({ ...campaign, status: "conclusa", closed_at: "2026-11-30T18:00:00Z" });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  role = "admin";
  calls = [];
});
afterAll(() => server.close());

describe("Campagne", () => {
  it("lists the campaigns with their portfolios, goals and report", async () => {
    renderApp("/campagne");
    expect(await screen.findByText("Prova novembre")).toBeInTheDocument();
    expect(screen.getByText("02/11/2026 – 30/11/2026")).toBeInTheDocument();
    expect(screen.getByText("Attiva")).toBeInTheDocument();
    expect(screen.getByText(/conto di prova Alpaca prova/)).toBeInTheDocument();
    expect(screen.getByText("campagna · Alfa · 3,00% · piano")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Scarica resoconto/ })).toHaveAttribute("href", "/api/campaigns/c1/report.pdf");
  });

  it("guides the creation step by step and sends one request", async () => {
    const user = userEvent.setup();
    renderApp("/campagne");
    await user.click(await screen.findByRole("button", { name: "Nuova campagna" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/Passo 1 di 4/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Avanti" }));
    expect(within(dialog).getByText("Il nome è obbligatorio")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Nome della campagna"), "Prova dicembre");
    await user.click(within(dialog).getByRole("button", { name: "Avanti" }));

    // step 2: a new portfolio on a paper account, an existing one; real-money ones never offered
    expect(within(dialog).getByText(/Passo 2 di 4/)).toBeInTheDocument();
    expect(within(dialog).queryByText("Reale")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Avanti" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("Crea o scegli almeno un portafoglio");
    await user.click(within(dialog).getByRole("button", { name: /Aggiungi portafoglio/ }));
    const fresh = within(dialog).getByRole("group", { name: "Nuovo portafoglio 1" });
    await user.type(within(fresh).getByLabelText("Nome"), "Campagna Alfa");
    await user.type(within(fresh).getByLabelText("Capitale iniziale"), "10000");
    const picker = within(fresh).getByLabelText("Conto di prova (facoltativo)");
    expect(within(picker).queryByText(/Conto vero/)).not.toBeInTheDocument();
    await user.selectOptions(picker, "l1");
    expect(within(fresh).getByRole("button", { name: "Prova collegamento" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("checkbox", { name: /Beta/ }));
    await user.click(within(dialog).getByRole("button", { name: "Avanti" }));

    // step 3: the recommended templates are ticked; one piano per portfolio
    await waitFor(() => expect(within(dialog).getByText(/Passo 3 di 4/)).toBeInTheDocument());
    expect(calls[0].body).toMatchObject({
      portfolios: [
        { key: "new-1", name: "Campagna Alfa", initial_cash: "10000", base_currency: "EUR", broker_link_id: "l1" },
        { key: "p2", portfolio_id: "p2", broker_link_id: null },
      ],
    });
    const alfa = within(dialog).getByRole("region", { name: "Obiettivi di Campagna Alfa" });
    expect(within(alfa).getByRole("checkbox", { name: "campagna · new-1" })).toBeChecked();
    const beta = within(dialog).getByRole("region", { name: "Obiettivi di Beta" });
    await user.click(within(beta).getByRole("checkbox", { name: "mensile · p2" }));
    await user.click(within(dialog).getByRole("button", { name: "Avanti" }));

    // step 4: summary, then one create request
    expect(within(dialog).getByText(/Passo 4 di 4/)).toBeInTheDocument();
    const goals = within(dialog).getByRole("list", { name: "Obiettivi della campagna" });
    expect(within(goals).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "campagna · Campagna Alfa · 8,00% · piano",
      "rischio campagna · Campagna Alfa · −10,00%",
      "campagna · Beta · 8,00%",
      "mensile · Beta · 8,00%",
    ]);
    await user.click(within(dialog).getByRole("button", { name: "Crea e avvia" }));
    await waitFor(() => expect(calls.find((c) => c.path === "create")).toBeTruthy());
    const body = calls.find((c) => c.path === "create")!.body as Record<string, unknown>;
    expect(body).toMatchObject({ name: "Prova dicembre", status: "attiva", notes: null });
    expect((body.goals as { portfolio_key: string; name: string; closes_positions: boolean }[]).map(
      (g) => [g.portfolio_key, g.name, g.closes_positions],
    )).toEqual([
      ["new-1", "campagna", true],
      ["new-1", "rischio campagna", false],
      ["p2", "campagna", false],
      ["p2", "mensile", false],
    ]);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("closes a campaign: blockers shown, archive choices sent, final report offered", async () => {
    const user = userEvent.setup();
    renderApp("/campagne");
    await user.click(await screen.findByRole("button", { name: /Chiudi campagna/ }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("Non archiviabile: Scollega prima il broker")).toBeInTheDocument();
    expect(within(dialog).getByRole("checkbox", { name: /Archivia Alfa/ })).toBeDisabled();
    expect(within(dialog).getByText("Posizioni: AAPL 2")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("checkbox", { name: /Archivia Beta/ }));
    await user.click(within(dialog).getByRole("button", { name: "Concludi la campagna" }));
    expect(await within(dialog).findByText("Campagna conclusa.")).toBeInTheDocument();
    expect(calls.find((c) => c.path === "close")!.body).toEqual({ archive_portfolio_ids: ["p2"], notes: "prima prova" });
    expect(within(dialog).getByRole("link", { name: /resoconto finale/ })).toHaveAttribute("href", "/api/campaigns/c1/report.pdf");
  });

  it("offers the period report and hides the wizard from a viewer", async () => {
    role = "viewer";
    const user = userEvent.setup();
    renderApp("/campagne");
    expect(await screen.findByText("Prova novembre")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Nuova campagna" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Chiudi campagna/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resoconto" }));
    const dialog = await screen.findByRole("dialog");
    const from = within(dialog).getByLabelText("Dal");
    await user.clear(from);
    await user.type(from, "2026-11-01");
    const to = within(dialog).getByLabelText("Al");
    await user.clear(to);
    await user.type(to, "2026-11-19");
    await user.click(within(dialog).getByRole("checkbox", { name: "Beta" }));
    expect(within(dialog).getByRole("link", { name: /Scarica PDF/ })).toHaveAttribute(
      "href",
      "/api/reports/period.pdf?from=2026-11-01&to=2026-11-19&portfolio_ids=p2",
    );
  });
});
