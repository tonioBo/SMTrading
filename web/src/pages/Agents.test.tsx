import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Agent } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const TOKEN = "smt_secret_token_value_123";
const agent: Agent = {
  id: "a1",
  name: "Bot uno",
  active: true,
  created_at: "2026-11-01T10:00:00Z",
  last_seen_at: null,
  portfolios: [{ portfolio_id: "p1", portfolio_name: "Alfa", permission: "trade" }],
};
const portfolios = [
  { id: "p1", name: "Alfa" },
  { id: "p2", name: "Beta" },
  { id: "p3", name: "Gamma" },
];

let puts: unknown[] = [];
let patches: unknown[] = [];
let auditUrls: string[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/agents", () => HttpResponse.json([agent])),
  http.get("*/api/portfolios", () => HttpResponse.json(portfolios)),
  http.post("*/api/agents", () => HttpResponse.json({ agent: { ...agent, id: "a2", name: "Nuovo" }, token: TOKEN })),
  http.post("*/api/agents/a1/rotate", () => HttpResponse.json({ token: TOKEN })),
  http.patch("*/api/agents/a1", async ({ request }) => {
    patches.push(await request.json());
    return HttpResponse.json({ ...agent, active: false });
  }),
  http.put("*/api/agents/a1/portfolios", async ({ request }) => {
    puts.push(await request.json());
    return HttpResponse.json(agent);
  }),
  http.get("*/api/mcp-audit", ({ request }) => {
    auditUrls.push(request.url);
    return HttpResponse.json([
      {
        id: 1,
        agent_id: "a1",
        method: "place_order",
        params: { symbol: "AAPL", qty: "3" },
        outcome: "ok",
        latency_ms: 42,
        created_at: "2026-11-02T10:00:00Z",
      },
    ]);
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  puts = [];
  patches = [];
  auditUrls = [];
});
afterAll(() => server.close());

describe("Agents", () => {
  it("lists agents with state, last access and portfolios", async () => {
    renderApp("/agenti");
    const table = await screen.findByRole("table", { name: "Agenti" });
    const row = (await within(table).findByText("Bot uno")).closest("tr")!;
    expect(within(row).getByText("Attivo")).toBeInTheDocument();
    expect(within(row).getByText("Mai")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Trade · 1" })).toBeInTheDocument();
    expect(within(row).queryByText("Alfa")).not.toBeInTheDocument(); // compact: names on demand
  });

  it("summarises many portfolios as chips (archived excluded) and expands a table", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/agents", () =>
        HttpResponse.json([
          {
            ...agent,
            portfolios: [
              { portfolio_id: "p1", portfolio_name: "Alfa", permission: "trade", archived: false, mode: "simulazione" },
              { portfolio_id: "p2", portfolio_name: "Beta", permission: "read", archived: false, mode: "paper" },
              { portfolio_id: "p3", portfolio_name: "Gamma", permission: "read", archived: false, mode: "simulazione" },
              { portfolio_id: "p9", portfolio_name: "Vecchio", permission: "trade", archived: true, mode: "simulazione" },
            ],
          },
        ]),
      ),
    );
    renderApp("/agenti");
    const row = (await screen.findByText("Bot uno")).closest("tr")!;
    expect(within(row).getByRole("button", { name: "Trade · 1" })).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: "Lettura · 2" }));
    const table = within(row).getByRole("table", { name: "Portafogli di Bot uno" });
    const names = within(table).getAllByRole("row").slice(1).map((r) => r.cells[0].textContent);
    expect(names).toEqual(["Alfa", "Beta", "Gamma"]);
    expect(within(table).getAllByText("Lettura", { selector: "td" })).toHaveLength(2);
    await user.click(within(row).getByRole("button", { name: "Nascondi" }));
    expect(within(row).queryByRole("table")).not.toBeInTheDocument();
  });

  it("manages the agent's watchlists and alerts from the detail page", async () => {
    const user = userEvent.setup();
    const calls: string[] = [];
    let symbols = ["AAPL", "MSFT"];
    server.use(
      http.get("*/api/agents/a1/watchlists", () =>
        HttpResponse.json([{ id: "w1", name: "tech", symbols, created_at: "2026-11-01T10:00:00Z" }]),
      ),
      http.get("*/api/agents/a1/alerts", () =>
        HttpResponse.json([
          { id: "al1", symbol: "NVDA", condition: "price_above", value: "150", ref_price: null, status: "active", note: null, created_at: "2026-11-01T10:00:00Z", triggered_at: null, triggered_price: null },
        ]),
      ),
      http.delete("*/api/agents/a1/watchlists/w1/items/:symbol", ({ params }) => {
        calls.push(`remove ${String(params.symbol)}`);
        symbols = symbols.filter((s) => s !== params.symbol);
        return HttpResponse.json({ id: "w1", name: "tech", symbols, created_at: "2026-11-01T10:00:00Z" });
      }),
      http.delete("*/api/agents/a1/watchlists/w1", () => {
        calls.push("delete w1");
        return HttpResponse.json({ id: "w1", deleted: true });
      }),
      http.post("*/api/agents/a1/alerts/al1/cancel", () => {
        calls.push("cancel al1");
        return HttpResponse.json({});
      }),
      http.get("*/api/marketdata/stream-status", () => HttpResponse.json({})),
    );
    renderApp("/agenti/a1");
    const section = await screen.findByLabelText("Watchlist e alert");
    await user.click(await within(section).findByRole("button", { name: "Rimuovi MSFT da tech" }));
    let dialog = await screen.findByRole("dialog", { name: "Rimuovere MSFT da tech?" });
    await user.click(within(dialog).getByRole("button", { name: "Rimuovi" }));
    await waitFor(() => expect(calls).toEqual(["remove MSFT"]));
    await waitFor(() => expect(within(section).queryByRole("button", { name: "Rimuovi MSFT da tech" })).not.toBeInTheDocument());

    await user.click(within(section).getByRole("button", { name: "Annulla alert NVDA" }));
    dialog = await screen.findByRole("dialog", { name: "Annullare l'alert su NVDA?" });
    await user.click(within(dialog).getByRole("button", { name: "Annulla alert" }));
    await waitFor(() => expect(calls).toContain("cancel al1"));

    await user.click(within(section).getByRole("button", { name: "Elimina watchlist tech" }));
    dialog = await screen.findByRole("dialog", { name: "Eliminare la watchlist tech?" });
    await user.click(within(dialog).getByRole("button", { name: "Elimina" }));
    await waitFor(() => expect(calls).toContain("delete w1"));
  });

  it("shows the new token once and removes it everywhere on close", async () => {
    const user = userEvent.setup();
    const { queryClient } = renderApp("/agenti");
    await screen.findByText("Bot uno");
    await user.click(screen.getByRole("button", { name: "Nuovo agente" }));
    await user.type(await screen.findByLabelText("Nome"), "Nuovo");
    await user.click(screen.getByRole("button", { name: "Crea" }));

    const dialog = await screen.findByRole("dialog", { name: "Token dell'agente" });
    expect(within(dialog).getByText(TOKEN)).toBeInTheDocument();
    expect(within(dialog).getByText(/Non sarà più visibile/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Copia" })).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "Fatto" }));
    await waitFor(() => expect(screen.queryByText(TOKEN)).not.toBeInTheDocument());
    await waitFor(() => {
      const cached = JSON.stringify([
        ...queryClient.getQueryCache().getAll().map((q) => q.state.data),
        ...queryClient.getMutationCache().getAll().map((m) => m.state.data),
      ]);
      expect(cached).not.toContain(TOKEN);
    });
  });

  it("rotates the token after a confirmation", async () => {
    const user = userEvent.setup();
    renderApp("/agenti");
    await screen.findByText("Bot uno");
    await user.click(screen.getByRole("button", { name: "Rigenera token Bot uno" }));
    await user.click(await screen.findByRole("button", { name: "Rigenera" }));
    expect(await screen.findByText(TOKEN)).toBeInTheDocument();
  });

  it("deactivates an agent", async () => {
    const user = userEvent.setup();
    renderApp("/agenti");
    await screen.findByText("Bot uno");
    await user.click(screen.getByRole("button", { name: "Disattiva Bot uno" }));
    await waitFor(() => expect(patches).toEqual([{ active: false }]));
  });

  it("PUTs the full set of portfolio assignments", async () => {
    const user = userEvent.setup();
    renderApp("/agenti");
    await screen.findByText("Bot uno");
    await user.click(screen.getByRole("button", { name: "Portafogli Bot uno" }));
    const dialog = await screen.findByRole("dialog", { name: /Portafogli assegnati/ });
    await user.selectOptions(await within(dialog).findByLabelText("Permesso Beta"), "read");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() =>
      expect(puts).toEqual([
        [
          { portfolio_id: "p1", permission: "trade" },
          { portfolio_id: "p2", permission: "read" },
        ],
      ]),
    );
  });

  it("keeps the assignments of archived portfolios (not listed) when saving", async () => {
    server.use(
      http.get("*/api/agents", () =>
        HttpResponse.json([
          { ...agent, portfolios: [...agent.portfolios, { portfolio_id: "p9", portfolio_name: "Vecchio", permission: "read" }] },
        ]),
      ),
    );
    const user = userEvent.setup();
    renderApp("/agenti");
    await screen.findByText("Bot uno");
    await user.click(screen.getByRole("button", { name: "Portafogli Bot uno" }));
    const dialog = await screen.findByRole("dialog", { name: /Portafogli assegnati/ });
    expect(await within(dialog).findByText(/1 assegnazione a portafogli archiviati/)).toBeInTheDocument();
    await user.selectOptions(await within(dialog).findByLabelText("Permesso Alfa"), "none");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(puts).toEqual([[{ portfolio_id: "p9", permission: "read" }]]));
  });
});

describe("McpLog", () => {
  it("lists calls, expands params and filters", async () => {
    const user = userEvent.setup();
    renderApp("/log-mcp");
    const table = await screen.findByRole("table", { name: "Log MCP" });
    expect(await within(table).findByText("place_order")).toBeInTheDocument();
    expect(within(table).getByText("42 ms")).toBeInTheDocument();
    expect(screen.queryByText(/"symbol": "AAPL"/)).not.toBeInTheDocument();
    await user.click(within(table).getByRole("button", { name: "Parametri" }));
    expect(screen.getByText(/"symbol": "AAPL"/)).toBeInTheDocument();

    await user.type(screen.getByLabelText("Metodo"), "get_quote");
    await waitFor(() => expect(auditUrls.at(-1)).toContain("method=get_quote"));
  });
});
