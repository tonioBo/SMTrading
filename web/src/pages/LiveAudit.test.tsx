import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { LiveAuditRow } from "@/api/types";
import { LIVE_AUDIT_PAGE } from "@/api/hooks/live";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const row = (id: number, over: Partial<LiveAuditRow> = {}): LiveAuditRow => ({
  id,
  ts: "2026-11-14T10:00:00Z",
  link_id: "l1",
  link_name: "Alpaca vero",
  portfolio_id: "p1",
  portfolio_name: "Reale",
  order_id: null,
  actor: "admin@example.com",
  action: "armed",
  ip: "127.0.0.1",
  user_agent: "test",
  details: { from: "dry_run" },
  ...over,
});

let requests: URL[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: true })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/live-links", () => HttpResponse.json([{ id: "l1", name: "Alpaca vero" }])),
  http.get("*/api/portfolios", () => HttpResponse.json([{ id: "p1", name: "Reale" }])),
  http.get("*/api/live-audit", ({ request }) => {
    const url = new URL(request.url);
    requests.push(url);
    if (url.searchParams.get("before_id")) return HttpResponse.json([row(1, { action: "keys_saved", details: {} })]);
    // a full page: there may be more
    return HttpResponse.json(
      Array.from({ length: LIVE_AUDIT_PAGE }, (_, i) => row(LIVE_AUDIT_PAGE + 1 - i, i === 0 ? { action: "disarmed" } : {})),
    );
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  requests = [];
});
afterAll(() => server.close());

describe("LiveAudit", () => {
  it("is in the sidebar in the «Sistema» group", async () => {
    renderApp("/registro-denaro-reale");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const sistema = within(nav).getByRole("group", { name: "Sistema" });
    expect(within(sistema).getByRole("link", { name: "Registro denaro reale" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Registro denaro reale" })).toBeInTheDocument();
  });

  it("live audit filters and csv link", async () => {
    const user = userEvent.setup();
    renderApp("/registro-denaro-reale");
    const table = await screen.findByRole("table", { name: "Registro denaro reale" });
    expect(await within(table).findByText("Denaro reale disattivato")).toBeInTheDocument();
    expect(within(table).getAllByText("Denaro reale attivato").length).toBe(LIVE_AUDIT_PAGE - 1);
    const csv = screen.getByRole("link", { name: "Esporta CSV" });
    expect(csv).toHaveAttribute("href", "/api/live-audit.csv");

    await user.click(screen.getByRole("button", { name: "Carica altri" }));
    expect(await within(table).findByText("Chiavi salvate")).toBeInTheDocument();
    expect(requests.at(-1)!.searchParams.get("before_id")).toBe("2");
    expect(screen.queryByRole("button", { name: "Carica altri" })).not.toBeInTheDocument();

    await waitFor(() => expect(within(screen.getByLabelText("Collegamento")).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(screen.getByLabelText("Collegamento"), "l1");
    await waitFor(() => expect(within(screen.getByLabelText("Portafoglio")).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(screen.getByLabelText("Portafoglio"), "p1");
    const action = screen.getByLabelText("Azione");
    expect(within(action).getByRole("option", { name: "Reduce-only automatico" })).toHaveValue("reduce_only_auto");
    await user.selectOptions(action, "reduce_only_auto");
    await waitFor(() => expect(requests.at(-1)!.searchParams.get("action")).toBe("reduce_only_auto"));
    const last = requests.at(-1)!;
    expect(last.searchParams.get("link_id")).toBe("l1");
    expect(last.searchParams.get("portfolio_id")).toBe("p1");
    expect(last.searchParams.get("limit")).toBe(String(LIVE_AUDIT_PAGE));
    expect(csv).toHaveAttribute("href", "/api/live-audit.csv?link_id=l1&portfolio_id=p1&action=reduce_only_auto");
  });
});
