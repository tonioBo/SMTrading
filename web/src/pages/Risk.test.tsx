import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";

vi.mock("@/lib/ws", async (orig) => ({
  ...(await orig<typeof import("@/lib/ws")>()),
  useLiveSocket: () => ({ socket: null, status: "open" }),
}));

let profilePosts: unknown[] = [];
let ratePuts: unknown[] = [];
const server = setupServer(
  ...commonHandlers,
  http.get("*/api/auth/me", () => HttpResponse.json({ email: "admin@example.com", totp_enabled: false })),
  http.get("*/api/kill-switch", () => HttpResponse.json({ on: false })),
  http.get("*/api/risk-profiles", () => HttpResponse.json([])),
  http.get("*/api/interest-rates", () =>
    HttpResponse.json([{ kind: "loan", key: "*", annual_pct: "5" }]),
  ),
  http.get("*/api/settings/app", () =>
    HttpResponse.json({
      stale_quote_s: { us_equity: 120, eu_equity: 1200, crypto: 60, forex: 600 },
      default_spread_bps: { us_equity: 2, eu_equity: 5, crypto: 5, forex: 1 },
      timezone: "Europe/Rome",
    }),
  ),
  http.post("*/api/risk-profiles", async ({ request }) => {
    const body = await request.json();
    profilePosts.push(body);
    return HttpResponse.json({ id: "r1", ...(body as object) });
  }),
  http.put("*/api/interest-rates", async ({ request }) => {
    const body = await request.json();
    ratePuts.push(body);
    return HttpResponse.json(body);
  }),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  profilePosts = [];
  ratePuts = [];
});
afterAll(() => server.close());

describe("Risk", () => {
  it("creates a profile with whitelist tags", async () => {
    const user = userEvent.setup();
    renderApp("/rischio");
    await user.click(await screen.findByRole("button", { name: "Nuovo profilo" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuovo profilo di rischio" });
    await user.type(within(dialog).getByLabelText("Nome"), "Prudente");
    await user.type(within(dialog).getByLabelText("Simboli ammessi"), "AAPL{Enter}MSFT,");
    await user.type(within(dialog).getByLabelText("Perdita giornaliera massima (%)"), "2,5");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(profilePosts).toHaveLength(1));
    expect(profilePosts[0]).toMatchObject({
      name: "Prudente",
      allowed_symbols: ["AAPL", "MSFT"],
      max_daily_loss_pct: "2.5",
      max_order_notional_eur: null,
      short_allowed: true,
    });
  });

  it("edits the \"Quando usarlo\" description", async () => {
    const user = userEvent.setup();
    renderApp("/rischio");
    await user.click(await screen.findByRole("button", { name: "Nuovo profilo" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuovo profilo di rischio" });
    await user.type(within(dialog).getByLabelText("Nome"), "Calmo");
    await user.type(within(dialog).getByLabelText("Quando usarlo"), "  Mercati calmi  ");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(profilePosts).toHaveLength(1));
    expect(profilePosts[0]).toMatchObject({ name: "Calmo", description: "Mercati calmi" });
  });

  it("saves the whole interest-rate table and requires the loan fallback", async () => {
    const user = userEvent.setup();
    renderApp("/rischio");
    const rate = await screen.findByLabelText("Tasso annuo % 1");
    await user.clear(rate);
    await user.type(rate, "4,5");
    await user.click(screen.getByRole("button", { name: "Aggiungi tasso" }));
    await user.type(screen.getByLabelText("Chiave tasso 2"), "USD");
    await user.click(screen.getByRole("button", { name: "Salva tassi" }));
    await waitFor(() =>
      expect(ratePuts).toEqual([
        [
          { kind: "loan", key: "*", annual_pct: "4.5" },
          { kind: "loan", key: "USD", annual_pct: "0" },
        ],
      ]),
    );
    await user.click(screen.getByRole("button", { name: "Rimuovi tasso 1" }));
    await user.click(screen.getByRole("button", { name: "Salva tassi" }));
    expect(await screen.findByText(/tasso di ripiego/)).toBeInTheDocument();
    expect(ratePuts).toHaveLength(1);
  });

  it("saves API decimals unchanged after prefilling them as it-IT input", async () => {
    const user = userEvent.setup();
    server.use(http.get("*/api/interest-rates", () => HttpResponse.json([{ kind: "loan", key: "*", annual_pct: "1.125" }])));
    renderApp("/rischio");
    expect(await screen.findByLabelText("Tasso annuo % 1")).toHaveValue("1,125");
    await user.click(screen.getByRole("button", { name: "Salva tassi" }));
    await waitFor(() => expect(ratePuts).toEqual([[{ kind: "loan", key: "*", annual_pct: "1.125" }]]));
    expect(await screen.findByLabelText("Tasso annuo % 1")).toHaveValue("1,125");
  });
});
