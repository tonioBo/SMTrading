import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { FeeRule } from "@/api/types";
import { renderApp } from "@/test/render";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

const rule: FeeRule = {
  id: "r1",
  name: "Globale",
  scope: "global",
  scope_ref: null,
  side: "both",
  currency: "EUR",
  fixed: "1",
  pct: "0.1",
  min_fee: "0.5",
  max_fee: null,
  tiers: [
    { up_to: "1000", pct: "0.2" },
    { up_to: null, pct: "0.1" },
  ],
  fx_pct: "0.25",
  priority: 0,
  active: true,
  created_at: "2026-11-01T10:00:00Z",
};

let posts: Record<string, unknown>[] = [];
let simulated: unknown[] = [];
let deleted: string[] = [];
const server = setupServer(
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/fee-rules", () => HttpResponse.json([rule])),
  http.get("*/api/portfolios", () => HttpResponse.json([{ id: "p1", name: "Alfa" }])),
  http.post("*/api/fee-rules", async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    posts.push(body);
    if (body.name === "Globale") {
      return HttpResponse.json(
        { code: "INVALID_ORDER", message: "esiste già una regola con questo nome", details: { field: "name" } },
        { status: 400 },
      );
    }
    return HttpResponse.json({ ...rule, id: "r2", ...body });
  }),
  http.delete("*/api/fee-rules/r1", () => {
    deleted.push("r1");
    return HttpResponse.json(
      { code: "INVALID_ORDER", message: "Deve restare una regola globale attiva per entrambi i lati", details: { field: "active" } },
      { status: 400 },
    );
  }),
  http.post("*/api/fee-rules/simulate", async ({ request }) => {
    simulated.push(await request.json());
    return HttpResponse.json({
      rule_id: "r1",
      rule_name: "Globale",
      notional_eur: "1500",
      fee: "2.75",
      fee_currency: "EUR",
      fx_fee_eur: "3.75",
    });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  posts = [];
  simulated = [];
  deleted = [];
});
afterAll(() => server.close());

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  renderApp("/commissioni");
  await screen.findByText("Globale");
  await user.click(screen.getByRole("button", { name: "Nuova regola" }));
  return screen.findByRole("dialog", { name: "Regola di commissione" });
}

describe("Fees", () => {
  it("lists rules with their values", async () => {
    renderApp("/commissioni");
    const table = await screen.findByRole("table", { name: "Regole di commissione" });
    const row = (await within(table).findByText("Globale")).closest("tr")!;
    expect(within(row).getByText("globale")).toBeInTheDocument();
    expect(within(row).getByText("0,1%")).toBeInTheDocument();
    expect(within(row).getByText("fino a 1.000: 0,2%; oltre: 0,1%")).toBeInTheDocument();
    expect(within(row).getByText("Sì")).toBeInTheDocument();
  });

  it("forces the last tier to 'oltre' and rejects a missing limit client-side", async () => {
    const user = userEvent.setup();
    const dialog = await openEditor(user);
    await user.type(within(dialog).getByLabelText("Nome"), "Nuova");
    await user.click(within(dialog).getByRole("button", { name: "Aggiungi scaglione" }));
    await user.click(within(dialog).getByRole("button", { name: "Aggiungi scaglione" }));

    const last = within(dialog).getByLabelText("Limite scaglione 2");
    expect(last).toBeDisabled();
    expect(last).toHaveValue("oltre");
    expect(within(dialog).getByLabelText("Limite scaglione 1")).toBeEnabled();

    await user.type(within(dialog).getByLabelText("Percentuale scaglione 1"), "0,2");
    await user.type(within(dialog).getByLabelText("Percentuale scaglione 2"), "0,1");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    expect(await within(dialog).findByText("Il limite deve essere un numero positivo")).toBeInTheDocument();
    expect(posts).toEqual([]);

    await user.type(within(dialog).getByLabelText("Limite scaglione 1"), "1000");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({
      name: "Nuova",
      tiers: [
        { up_to: "1000", pct: "0.2" },
        { up_to: null, pct: "0.1" },
      ],
    });
  });

  it("shows a server validation error inline", async () => {
    const user = userEvent.setup();
    const dialog = await openEditor(user);
    await user.type(within(dialog).getByLabelText("Nome"), "Globale");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    expect(await within(dialog).findByText("esiste già una regola con questo nome")).toBeInTheDocument();
  });

  it("shows the server refusal when deactivating the last global rule", async () => {
    const user = userEvent.setup();
    renderApp("/commissioni");
    await screen.findByText("Globale");
    await user.click(screen.getByRole("button", { name: "Disattiva Globale" }));
    expect(await screen.findByText("Deve restare una regola globale attiva per entrambi i lati")).toBeInTheDocument();
    expect(deleted).toEqual(["r1"]);
  });

  it("shows the simulator result returned by the API", async () => {
    const user = userEvent.setup();
    renderApp("/commissioni");
    await screen.findByText("Globale");
    await user.selectOptions(await screen.findByLabelText("Portafoglio"), "p1");
    await user.type(screen.getByLabelText("Simbolo"), "AAPL");
    await user.type(screen.getByLabelText("Quantità"), "10");
    await user.click(screen.getByRole("button", { name: "Simula" }));
    const result = await screen.findByRole("region", { name: "Risultato simulazione" });
    expect(within(result).getByText("Globale")).toBeInTheDocument();
    expect(within(result).getByText("2,75 €")).toBeInTheDocument();
    expect(within(result).getByText("3,75 €")).toBeInTheDocument();
    expect(simulated).toEqual([{ portfolio_id: "p1", symbol: "AAPL", side: "buy", qty: "10" }]);
  });
});
