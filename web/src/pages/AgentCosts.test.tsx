import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
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
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  periods = [];
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

  it("is listed in the sidebar under Agenti", async () => {
    renderApp("/costi-agenti");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const labels = within(nav).getAllByRole("link").map((a) => a.textContent);
    expect(labels.indexOf("Costi agenti")).toBe(labels.indexOf("Agenti") + 1);
  });
});
