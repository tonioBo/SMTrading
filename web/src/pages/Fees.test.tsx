import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { FeeRule } from "@/api/types";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

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
let patches: Record<string, unknown>[] = [];
let simulated: unknown[] = [];
let deleted: string[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/fee-rules", () => HttpResponse.json([rule])),
  http.get("*/api/portfolios", ({ request }) =>
    HttpResponse.json(
      new URL(request.url).searchParams.get("include_archived")
        ? [{ id: "p1", name: "Alfa", status: "active" }, { id: "p9", name: "Test 250 · Difensivo", status: "archived" }]
        : [{ id: "p1", name: "Alfa", status: "active" }],
    ),
  ),
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
  http.patch("*/api/fee-rules/:id", async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    patches.push(body);
    return HttpResponse.json({ ...rule, ...body });
  }),
  http.delete("*/api/fee-rules/r1", () => {
    deleted.push("r1");
    return HttpResponse.json(
      { code: "INVALID_ORDER", message: "Deve restare una regola globale attiva per entrambi i lati", details: { field: "active" } },
      { status: 400 },
    );
  }),
  http.post("*/api/fees/simulate", async ({ request }) => {
    simulated.push(await request.json());
    return HttpResponse.json({
      rule_id: "r1", rule_name: "Globale", scope: "global", fee: "2.75", currency: "EUR",
      fx_fee: "3.75", fx_currency: "EUR",
    });
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  posts = [];
  patches = [];
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
    expect(within(row).getByText("Tutti")).toBeInTheDocument();
    expect(within(row).getByText("(min 0,50 €)")).toBeInTheDocument();
    expect(within(row).getByText("Sì")).toBeInTheDocument();
    expect(within(row).queryByText("fino a 1.000: 0,2%; oltre: 0,1%")).not.toBeInTheDocument();
    await userEvent.setup().click(within(row).getByRole("button", { name: "scaglioni" }));
    expect(within(row).getByText("fino a 1.000: 0,2%; oltre: 0,1%")).toBeInTheDocument();
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual([
      "Nome", "Si applica a", "Lato", "Commissione", "Cambio %", "Priorità", "Attiva", "Azioni",
    ]);
  });

  it("names archived portfolios, writes simple fees in one string and hides inactive rules", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/fee-rules", () =>
        HttpResponse.json([
          { ...rule, id: "r3", name: "Zero", scope: "portfolio", scope_ref: "p9", fixed: "0", pct: "0", min_fee: null, tiers: null, fx_pct: "0.15" },
          { ...rule, id: "r4", name: "Std", scope: "asset_class", scope_ref: "crypto", fixed: "1", pct: "0.1", min_fee: "1", max_fee: "20", tiers: null },
          { ...rule, id: "r5", name: "Vecchia", active: false, tiers: null },
        ]),
      ),
    );
    renderApp("/commissioni");
    const table = await screen.findByRole("table", { name: "Regole di commissione" });
    const zero = (await within(table).findByText("Zero")).closest("tr")!;
    expect(await within(zero).findByText("Portafoglio: Test 250 · Difensivo (archiviato)")).toBeInTheDocument();
    expect(within(zero).getByText("0,00 €")).toBeInTheDocument();
    const std = within(table).getByText("Std").closest("tr")!;
    expect(within(std).getByText("Classe: Crypto")).toBeInTheDocument();
    expect(std).toHaveTextContent("1,00 € + 0,1%(min 1,00 €, max 20,00 €)");
    expect(within(table).queryByText("Vecchia")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("Mostra disattivate"));
    expect(within(table).getByText("Vecchia")).toBeInTheDocument();
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

  it("saves an edited rule unchanged: API decimals round-trip through the it-IT inputs", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/fee-rules", () =>
        HttpResponse.json([{ ...rule, pct: "1.125", tiers: [{ up_to: "10000", pct: "1.125" }, { up_to: null, pct: "0.1" }] }]),
      ),
    );
    renderApp("/commissioni");
    await user.click(await screen.findByRole("button", { name: "Modifica Globale" }));
    const dialog = await screen.findByRole("dialog", { name: "Regola di commissione" });
    expect(within(dialog).getByLabelText("Commissione %")).toHaveValue("1,125");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(patches).toHaveLength(1));
    expect(patches[0]).toMatchObject({
      pct: "1.125",
      fixed: "1",
      min_fee: "0.5",
      fx_pct: "0.25",
      tiers: [
        { up_to: "10000", pct: "1.125" },
        { up_to: null, pct: "0.1" },
      ],
    });
  });

  it("refuses an ambiguous tier limit with the it-IT hint", async () => {
    const user = userEvent.setup();
    const dialog = await openEditor(user);
    await user.type(within(dialog).getByLabelText("Nome"), "Nuova");
    await user.click(within(dialog).getByRole("button", { name: "Aggiungi scaglione" }));
    await user.click(within(dialog).getByRole("button", { name: "Aggiungi scaglione" }));
    await user.type(within(dialog).getByLabelText("Limite scaglione 1"), "10.000");
    await user.type(within(dialog).getByLabelText("Percentuale scaglione 1"), "0,2");
    await user.type(within(dialog).getByLabelText("Percentuale scaglione 2"), "0,1");
    expect(within(dialog).getAllByText("Usa la virgola per i decimali (es. 10.000,00)").length).toBeGreaterThan(0);
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    expect(posts).toEqual([]);
    await user.clear(within(dialog).getByLabelText("Limite scaglione 1"));
    await user.type(within(dialog).getByLabelText("Limite scaglione 1"), "10.000,00");
    expect(within(dialog).getByText("= 10.000,00 €")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ tiers: [{ up_to: "10000.00", pct: "0.2" }, { up_to: null, pct: "0.1" }] });
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
    const form = screen.getByRole("form", { name: "Simula commissione" });
    await waitFor(() => expect(within(within(form).getByLabelText("Portafoglio")).getAllByRole("option")).toHaveLength(2));
    await user.selectOptions(within(form).getByLabelText("Portafoglio"), "p1"); // active only
    await user.selectOptions(within(form).getByLabelText("Classe di asset"), "crypto");
    await user.type(within(form).getByLabelText("Controvalore"), "1.500,50");
    await user.selectOptions(within(form).getByLabelText("Valuta dello strumento"), "USD");
    await user.click(within(form).getByRole("button", { name: "Simula" }));
    const result = await screen.findByRole("region", { name: "Risultato simulazione" });
    expect(within(result).getByText("Globale")).toBeInTheDocument();
    expect(within(result).getByText("2,75 €")).toBeInTheDocument();
    expect(within(result).getByText("3,75 €")).toBeInTheDocument();
    expect(simulated).toEqual([
      { portfolio_id: "p1", asset_class: "crypto", side: "buy", notional: "1500.50", currency: "USD" },
    ]);
  });
});
