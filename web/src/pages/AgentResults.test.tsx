import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { AgentResults } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const body: AgentResults = {
  as_of: "2026-11-18T10:00:00Z",
  period: "week",
  from: "2026-11-16",
  to: "2026-11-18",
  last_read_at: "2026-11-18T09:55:00Z",
  team: {
    pnl_eur: "12.3",
    costs_eur: "0.4",
    orders_executed: 3,
    proposals: 4,
    approvals: 2,
    vetoes: 1,
    approval_rate_pct: "66.7",
    lessons: 2,
    lessons_repeated: 1,
    rules_active: 3,
    cost_usd: "5",
    billed_usd: "3",
    unmatched_cost_usd: "1",
    pnl_per_usd: "2.46",
    cost_per_order_usd: "1.67",
  },
  agents: [
    {
      agent_id: "a-pm",
      name: "Portfolio Manager",
      active: true,
      decisions: { proposal: 4, lesson: 1, veto: 0 },
      decisions_total: 5,
      proposals: 4,
      approved: 2,
      vetoed: 1,
      pending: 1,
      approval_rate_pct: "66.7",
      lessons_written: 1,
      lessons_about: 2,
      lessons_repeated: 1,
      mcp_calls: 10,
      mcp_errors: 2,
      mcp_error_pct: "20.0",
      orders_executed: 0,
      paperclip_names: ["PM bot"],
      runs: 3,
      cost_usd: "4",
      billed_usd: "2",
      cost_per_order_usd: null,
    },
    {
      agent_id: "a-es",
      name: "Execution Supervisor",
      active: false,
      decisions: {},
      decisions_total: 0,
      proposals: 0,
      approved: 0,
      vetoed: 0,
      pending: 0,
      approval_rate_pct: null,
      lessons_written: 0,
      lessons_about: 0,
      lessons_repeated: 0,
      mcp_calls: 0,
      mcp_errors: 0,
      mcp_error_pct: null,
      orders_executed: 3,
      paperclip_names: [],
      runs: 0,
      cost_usd: "0",
      billed_usd: "0",
      cost_per_order_usd: "0",
    },
  ],
  strategies: [
    {
      ref: "STR-7",
      name: "rsi_reversion su AAPL",
      status: "approvata",
      portfolios: ["Portafoglio Aggressivo"],
      trades_opened: 2,
      trades_closed: 1,
      wins: 1,
      win_rate_pct: "100.0",
      realized_pnl_eur: "12.7",
      costs_eur: "0.4",
      net_pnl_eur: "12.3",
      avg_holding_s: 5400,
      open_positions: 1,
    },
    {
      ref: null,
      name: null,
      status: null,
      portfolios: ["Portafoglio Aggressivo"],
      trades_opened: 1,
      trades_closed: 0,
      wins: 0,
      win_rate_pct: null,
      realized_pnl_eur: "0",
      costs_eur: "0",
      net_pnl_eur: "0",
      avg_holding_s: null,
      open_positions: 1,
    },
  ],
};

const empty: AgentResults = {
  ...body,
  period: "all",
  from: null,
  last_read_at: null,
  team: { ...body.team, pnl_eur: "0", orders_executed: 0, pnl_per_usd: null, cost_per_order_usd: null, unmatched_cost_usd: "0" },
  agents: [],
  strategies: [],
};

const asked: string[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "v@example.com", role: "viewer", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/agent-results", ({ request }) => {
    const period = new URL(request.url).searchParams.get("period") ?? "";
    asked.push(period);
    return HttpResponse.json(period === "all" ? empty : body);
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  asked.length = 0;
});
afterAll(() => server.close());

describe("AgentResults", () => {
  it("shows the team summary, the agents and the strategies of the current week", async () => {
    renderApp("/risultati-agenti");
    expect(await screen.findByRole("heading", { name: "Risultati agenti" })).toBeInTheDocument();
    const team = await screen.findByRole("region", { name: "Riepilogo del team" });
    expect(within(team).getByText("+12,30 €")).toBeInTheDocument();
    expect(within(team).getByText("+2,46 €")).toBeInTheDocument(); // per 1 $ of cost
    expect(within(team).getByText("5,00 $")).toBeInTheDocument();
    expect(within(team).getByText("1 ripetute")).toBeInTheDocument();
    expect(within(team).getByText("Agenti Paperclip non abbinati")).toBeInTheDocument();
    expect(screen.getByText(/Dal 16\/11\/2026 al 18\/11\/2026/)).toBeInTheDocument();
    expect(asked).toEqual(["week"]);

    const agents = screen.getByRole("table", { name: "Risultati per agente" });
    const [pm, es] = within(agents).getAllByRole("row").slice(1);
    expect(within(pm).getByRole("link", { name: "Portfolio Manager" })).toHaveAttribute("href", "/agenti/a-pm");
    expect(pm).toHaveTextContent("Paperclip: PM bot");
    expect(pm).toHaveTextContent("proposte 4 · lezioni 1");
    expect(pm).not.toHaveTextContent("veti 0");
    expect(pm).toHaveTextContent("2 approvate · 1 veti · 1 in attesa");
    expect(pm).toHaveTextContent("66,7%");
    expect(pm).toHaveTextContent("(1 ripetute)");
    expect(pm).toHaveTextContent("2 errori (20,0%)");
    expect(pm).toHaveTextContent("4,00 $");
    expect(es).toHaveTextContent("(disattivato)");

    const strategies = screen.getByRole("table", { name: "Risultati per strategia" });
    const [str, none] = within(strategies).getAllByRole("row").slice(1);
    expect(str).toHaveTextContent("STR-7");
    expect(str).toHaveTextContent("approvata");
    expect(str).toHaveTextContent("100,0%(1/1)");
    expect(str).toHaveTextContent("1 h 30 min");
    expect(none).toHaveTextContent("Senza strategia");
    expect(none).toHaveTextContent("—");
  });

  it("changes period and shows the empty states", async () => {
    renderApp("/risultati-agenti");
    await screen.findByRole("region", { name: "Riepilogo del team" });
    await userEvent.click(screen.getByRole("button", { name: "Tutto" }));
    expect(await screen.findByText("Nessuna operazione nel periodo")).toBeInTheDocument();
    expect(screen.getByText("Nessun agente")).toBeInTheDocument();
    expect(screen.getByText(/Tutto lo storico fino al 18\/11\/2026/)).toBeInTheDocument();
    expect(screen.getByText(/Paperclip non è ancora stato letto/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Tutto" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText("Agenti Paperclip non abbinati")).not.toBeInTheDocument();
    expect(asked).toEqual(["week", "all"]);
  });
});
