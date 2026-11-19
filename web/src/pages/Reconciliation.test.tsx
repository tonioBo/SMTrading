import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { ReconciliationCheck, ReconciliationPortfolio } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const mismatch: ReconciliationCheck = {
  id: 2,
  ts: "2026-11-19T10:15:00Z",
  status: "mismatch",
  trigger: "schedule",
  error: null,
  meta: { currency: "EUR", orders_checked: true, link_portfolios: 1 },
  counts: { ok: 1, warning: 0, mismatch: 1, unverifiable: 0 },
  items: [
    {
      key: "position:AAPL",
      kind: "position",
      label: "AAPL",
      symbol: "AAPL",
      ours: "10",
      broker: "7",
      diff: "3",
      status: "mismatch",
      note: "quantità diversa",
      streak: 2,
      since: "2026-11-19T10:00:00Z",
      alerted: true,
    },
    { key: "cash:EUR", kind: "cash", label: "Liquidità EUR", symbol: null, ours: "9196.2", broker: "9196.2", diff: "0", status: "ok", note: "uguale" },
  ],
};

const portfolios = (latest: ReconciliationCheck | null = mismatch): ReconciliationPortfolio[] => [
  {
    portfolio_id: "p1",
    portfolio_name: "Aggressivo",
    linked: true,
    link: { id: "l1", name: "Alpaca", kind: "alpaca_paper", live: false, enabled: true },
    latest,
    last_verified_at: latest?.ts ?? null,
  },
  { portfolio_id: "p2", portfolio_name: "Sim", linked: false, link: null, latest: null, last_verified_at: null },
];

let role = "admin";
let runs: unknown[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: true, role })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/reconciliation", () => HttpResponse.json({ portfolios: portfolios() })),
  http.get("*/api/reconciliation/p1", () =>
    HttpResponse.json({
      ...portfolios()[0],
      history: [
        { ...mismatch, items: undefined },
        { ...mismatch, id: 1, ts: "2026-11-19T10:00:00Z", status: "unverifiable", error: "HTTP 503", items: undefined },
      ],
    }),
  ),
  http.post("*/api/reconciliation/run", async ({ request }) => {
    runs.push(await request.json());
    return HttpResponse.json({ portfolios: portfolios({ ...mismatch, id: 3, status: "ok", trigger: "manual" }) });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  role = "admin";
  runs = [];
});
afterAll(() => server.close());

describe("Reconciliation", () => {
  it("is in the «Sistema» group right after «Broker»", async () => {
    renderApp("/riconciliazione");
    const nav = await screen.findByRole("navigation", { name: "Navigazione principale" });
    const links = within(within(nav).getByRole("group", { name: "Sistema" }))
      .getAllByRole("link")
      .map((a) => a.textContent);
    expect(links.slice(0, 2)).toEqual(["Broker", "Riconciliazione"]);
    expect(await screen.findByRole("heading", { name: "Riconciliazione" })).toBeInTheDocument();
  });

  it("shows the status cards, the items and the history", async () => {
    renderApp("/riconciliazione");
    const card = await screen.findByRole("region", { name: "Riconciliazione Aggressivo" });
    expect(within(card).getByText("Differenze")).toBeInTheDocument();
    expect(within(card).getByText("1 uguali · 0 da verificare · 1 diversi")).toBeInTheDocument();
    const sim = screen.getByRole("region", { name: "Riconciliazione Sim" });
    expect(within(sim).getByText("Non collegato")).toBeInTheDocument();

    const items = await screen.findByRole("table", { name: "Voci confrontate" });
    const aapl = within(items).getByText("AAPL").closest("tr")!;
    expect(within(aapl).getByText("Diverso")).toBeInTheDocument();
    expect(within(aapl).getByText("quantità diversa · da 2 controlli · avviso inviato")).toBeInTheDocument();
    expect(within(aapl).getByText("3")).toBeInTheDocument();
    const history = screen.getByRole("table", { name: "Storico dei controlli" });
    expect(within(history).getByText("Non verificabile")).toBeInTheDocument();
    expect(within(history).getByText("HTTP 503")).toBeInTheDocument();
  });

  it("runs a check now for one portfolio and for all", async () => {
    const user = userEvent.setup();
    renderApp("/riconciliazione");
    const card = await screen.findByRole("region", { name: "Riconciliazione Aggressivo" });
    server.use(
      http.get("*/api/reconciliation", () =>
        HttpResponse.json({ portfolios: portfolios({ ...mismatch, id: 3, status: "ok", trigger: "manual" }) }),
      ),
    );
    await user.click(within(card).getByRole("button", { name: "Riconcilia ora" }));
    await waitFor(() => expect(runs).toEqual([{ portfolio_id: "p1" }]));
    expect(await within(card).findByText("Allineato")).toBeInTheDocument();
    const header = screen.getByRole("heading", { name: "Riconciliazione" }).parentElement!;
    await user.click(within(header).getByRole("button", { name: "Riconcilia ora" }));
    await waitFor(() => expect(runs).toEqual([{ portfolio_id: "p1" }, {}]));
  });

  it("hides «Riconcilia ora» from a viewer", async () => {
    role = "viewer";
    renderApp("/riconciliazione");
    await screen.findByRole("region", { name: "Riconciliazione Aggressivo" });
    expect(screen.queryByRole("button", { name: "Riconcilia ora" })).not.toBeInTheDocument();
  });
});
