import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import type { AgentCosts } from "@/api/types";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let periods: string[] = [];

function costs(period: string): AgentCosts {
  const big = period === "month";
  return {
    currency: "USD",
    from: "2026-11-15",
    to: "2026-11-15",
    last_read_at: "2026-11-15T10:00:00Z",
    agents: [
      {
        agent_id: "a1",
        name: "Analista",
        paperclip_name: "analista",
        api_usd: big ? "1234.5" : "1.5",
        subscription_usd: "0.004",
        other_usd: "0.1234",
        runs: 7,
        orders: 2,
        portfolios: [{ id: "p1", name: "Crescita", permission: "trade", return_pct: "1.25" }],
      },
    ],
    unmatched: [{ paperclip_agent_id: "pc-9", name: "Sconosciuto", api_usd: "3", subscription_usd: "0", other_usd: "0.1" }],
    month,
    models: {
      rows: [modelRow("claude-opus", "4.00", 2, 1), modelRow("claude-sonnet", "0.90", 3, 0)],
      agents: [{ paperclip_agent_id: "pc-1", name: "analista", model: "claude-sonnet", since: "2026-11-19T08:38:00Z" }],
      changes: [{ agent_id: "pc-1", name: "analista", from: "claude-opus", to: "claude-sonnet", at: "2026-11-19T08:38:00Z" }],
      updated_at: "2026-11-19T10:00:00Z",
    },
  };
}

function modelRow(model: string, billed: string, runs: number, derived: number) {
  const per = (Number(billed) / runs).toFixed(4);
  return { model, billed_usd: billed, api_usd: billed, subscription_usd: "0", other_usd: "0", runs, derived_runs: derived, billed_per_run_usd: per, full_per_run_usd: per, agents: ["analista"] };
}

let compares: string[] = [];

let month: AgentCosts["month"];
function baseMonth(): AgentCosts["month"] {
  return {
    month: "2026-11",
    from: "2026-11-01",
    to: "2026-11-15",
    days_elapsed: 15,
    days_in_month: 30,
    spent_usd: "90.00",
    daily_avg_usd: "6.00",
    projected_usd: "180.00",
    budget_usd: "100.00",
    budget_source: "paperclip",
    spent_pct: "90.0",
    projected_pct: "180.0",
    shares: [
      { agent_id: "a1", name: "Analista", usd: "60.00", pct: "66.7" },
      { agent_id: null, name: "Sconosciuto", usd: "30.00", pct: "33.3" },
    ],
  };
}

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "a@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/agent-costs", ({ request }) => {
    const period = new URL(request.url).searchParams.get("period") ?? "";
    periods.push(period);
    return HttpResponse.json(costs(period));
  }),
  http.get("*/api/agent-costs/compare", ({ request }) => {
    const q = new URL(request.url).searchParams;
    compares.push(`${q.get("at")}|${q.get("days")}`);
    return HttpResponse.json({
      at: "2026-11-19T08:38:00+00:00",
      days: Number(q.get("days")),
      before: { from: "2026-11-12", to: "2026-11-19T08:38:00+00:00", models: [modelRow("claude-opus", "4.00", 2, 0)] },
      after: { from: "2026-11-19T08:38:00+00:00", to: "2026-11-19T10:00:00+00:00", models: [modelRow("claude-sonnet", "1.00", 4, 0)] },
      straddling: [],
    });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => {
  month = baseMonth();
});
afterEach(() => {
  server.resetHandlers();
  periods = [];
  compares = [];
});
afterAll(() => server.close());

describe("AgentCosts", () => {
  it("costs page switches period and shows usd", async () => {
    const user = userEvent.setup();
    renderApp("/costi-agenti");
    expect(await screen.findByRole("heading", { name: "Costi agenti" })).toBeInTheDocument();
    const table = await screen.findByRole("table", { name: "Costi per agente" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Agente",
      "Modello",
      "A consumo API ($)",
      "Incluso nell'abbonamento ($)",
      "Altro ($)",
      "Esecuzioni",
      "Ordini",
      "Portafogli",
    ]);
    const row = await within(table).findByRole("row", { name: /Analista/ });
    expect(row).toHaveTextContent("1,50 $");
    // sub-cent and below-1-dollar amounts stay readable; the raw value is in the title
    expect(within(row).getByText("< 0,01 $")).toHaveAttribute("title", "0.004 $");
    expect(within(row).getByText("0,123 $")).toHaveAttribute("title", "0.1234 $");
    expect(row).toHaveTextContent("Crescita");
    expect(row).toHaveTextContent("+1,25%");
    expect(periods).toEqual(["day"]);

    const unmatched = screen.getByRole("region", { name: "Agenti Paperclip non abbinati" });
    expect(unmatched).toHaveTextContent("Sconosciuto");
    expect(unmatched).toHaveTextContent("3,00 $");

    await user.click(screen.getByRole("button", { name: "Mese" }));
    await waitFor(() => expect(periods).toContain("month"));
    expect(await within(table).findByText("1.234,50 $")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mese" })).toHaveAttribute("aria-pressed", "true");
  });

  it("month card: spend, daily average, projection over the budget, shares", async () => {
    renderApp("/costi-agenti");
    const card = await screen.findByRole("region", { name: "Mese in corso" });
    expect(card).toHaveTextContent("Mese in corso · novembre 2026");
    expect(card).toHaveTextContent("Speso finora90,00 $(90,0% del budget)");
    expect(card).toHaveTextContent("Media giornaliera6,00 $(15 di 30 giorni)");
    expect(card).toHaveTextContent("Proiezione a fine mese180,00 $(180,0% del budget)");
    expect(card).toHaveTextContent("Budget mensile100,00 $(da Paperclip)");
    expect(within(card).getByRole("alert")).toHaveTextContent("A questo ritmo il mese chiude oltre il budget.");
    const shares = within(card).getByRole("list", { name: "Quota per agente" });
    expect(within(shares).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Analista60,00 $ · 66,7%", "Sconosciuto30,00 $ · 33,3%"]);
  });

  it("month card without a budget: no percentages, no warning", async () => {
    month = { ...baseMonth(), budget_usd: null, budget_source: null, spent_pct: null, projected_pct: null };
    renderApp("/costi-agenti");
    const card = await screen.findByRole("region", { name: "Mese in corso" });
    expect(card).toHaveTextContent("Budget mensile—(non impostato)");
    expect(card).not.toHaveTextContent("del budget");
    expect(within(card).queryByRole("alert")).toBeNull();
  });

  it("is listed in the sidebar in the «Agenti» group", async () => {
    renderApp("/costi-agenti");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const agenti = within(nav).getByRole("group", { name: "Agenti" });
    const labels = within(agenti).getAllByRole("link").map((a) => a.textContent);
    expect(labels.slice(0, 3)).toEqual(["Agenti", "Istruzioni agenti", "Costi agenti"]);
  });

  it("per model: spend, runs, cost per run, each agent's model now and before/after the last change", async () => {
    const user = userEvent.setup();
    renderApp("/costi-agenti");
    const region = await screen.findByRole("region", { name: "Per modello" });
    const table = within(region).getByRole("table", { name: "Costi per modello" });
    const opus = within(table).getByRole("row", { name: /claude-opus/ });
    expect(opus).toHaveTextContent("4,00 $");
    expect(opus).toHaveTextContent("2*"); // a run with the model derived from the configuration
    expect(within(table).getByRole("row", { name: /claude-sonnet/ })).toHaveTextContent("0,300 $");
    expect(within(region).getByRole("list", { name: "Modello di ogni agente" })).toHaveTextContent("analistaclaude-sonnet");
    const agents = screen.getByRole("table", { name: "Costi per agente" });
    expect(within(agents).getAllByRole("columnheader")[1]).toHaveTextContent("Modello");

    // the comparison starts at the last change (19/11 09:38 Rome time), 7 days
    const cmp = screen.getByRole("region", { name: "Prima e dopo un cambio" });
    await waitFor(() => expect(compares).toContain("2026-11-19T09:38|7"));
    expect(await within(cmp).findByRole("status")).toHaveTextContent("Costo per esecuzione dopo il cambio: -87,50%");
    expect(within(cmp).getByRole("group", { name: "Prima" })).toHaveTextContent("2,00 $ per esecuzione");
    expect(within(cmp).getByRole("group", { name: "Dopo" })).toHaveTextContent("0,250 $ per esecuzione");
    await user.selectOptions(within(cmp).getByLabelText("Giorni da confrontare"), "14");
    await waitFor(() => expect(compares).toContain("2026-11-19T09:38|14"));
  });
});
