import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { toast } from "sonner";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { renderApp } from "@/test/render";
import { commonHandlers } from "@/test/handlers";
import { haltFixture } from "@/test/halts";

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
  http.get("*/api/portfolios", () => HttpResponse.json([{ id: "p1", name: "Alfa" }])),
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

  it("shows each profile as a compact card: limit chips, clamped text, symbol count", async () => {
    const user = userEvent.setup();
    const long = "Strategia passiva su ETF ampi: ".repeat(8).trim();
    const symbols = Array.from({ length: 20 }, (_, i) => `ETF${i}`);
    server.use(
      http.get("*/api/risk-profiles", () =>
        HttpResponse.json([
          {
            id: "p5", name: "5 · Passivo ETF", max_order_notional_eur: "2500", max_position_pct: "30",
            max_daily_loss_pct: "3", max_daily_loss_eur: null, allowed_symbols: symbols,
            allowed_asset_classes: ["us_equity", "eu_equity"], allowed_markets: [], short_allowed: false,
            description: long, portfolios_count: 2,
          },
        ]),
      ),
    );
    renderApp("/rischio");
    const card = await screen.findByRole("listitem", { name: "Profilo 5 · Passivo ETF" });
    expect(within(card).getByText("Ordine max: 2.500 €")).toBeInTheDocument();
    expect(within(card).getByText("Posizione max: 30%")).toBeInTheDocument();
    expect(within(card).getByText("Perdita giorno: 3%")).toBeInTheDocument();
    expect(within(card).getByText("Scoperto: no")).toBeInTheDocument();
    expect(within(card).getByText("Classi: Azioni USA, Azioni EU")).toBeInTheDocument();
    expect(within(card).getByText("Mercati: tutti")).toBeInTheDocument();
    expect(within(card).getByText("2 portafogli attivi")).toBeInTheDocument();
    const text = within(card).getByText(long);
    expect(text).toHaveAttribute("title", long);
    expect(text.className).toContain("line-clamp-2");
    await user.click(within(card).getByRole("button", { name: "Mostra tutto" }));
    expect(text.className).not.toContain("line-clamp-2");

    expect(within(card).queryByRole("list", { name: /Simboli ammessi/ })).not.toBeInTheDocument();
    await user.click(within(card).getByRole("button", { name: "Simboli: 20" }));
    const list = within(card).getByRole("list", { name: "Simboli ammessi da 5 · Passivo ETF" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(20);
  });

  it("risk fuse field validates 1-1000", async () => {
    const user = userEvent.setup();
    renderApp("/rischio");
    await user.click(await screen.findByRole("button", { name: "Nuovo profilo" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuovo profilo di rischio" });
    await user.type(within(dialog).getByLabelText("Nome"), "Reale");
    const fuse = within(dialog).getByLabelText("Ordini reali massimi all'ora (fusibile)");
    expect(fuse).toHaveValue("30"); // the default
    for (const bad of ["0", "1001", "2,5", "abc"]) {
      await user.clear(fuse);
      await user.type(fuse, bad);
      await user.click(within(dialog).getByRole("button", { name: "Salva" }));
      expect(within(dialog).getByText("Inserisci un numero intero tra 1 e 1000")).toBeInTheDocument();
    }
    expect(profilePosts).toEqual([]);
    await user.clear(fuse);
    await user.type(fuse, "1000");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(profilePosts).toHaveLength(1));
    expect(profilePosts[0]).toMatchObject({ name: "Reale", max_live_orders_per_hour: 1000 });
  });

  it("shows the server refusal of the fuse next to its field", async () => {
    server.use(
      http.post("*/api/risk-profiles", () =>
        HttpResponse.json(
          {
            code: "INVALID_ORDER",
            message: "max_live_orders_per_hour deve essere un intero tra 1 e 1000",
            details: { field: "max_live_orders_per_hour" },
          },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    renderApp("/rischio");
    await user.click(await screen.findByRole("button", { name: "Nuovo profilo" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuovo profilo di rischio" });
    await user.type(within(dialog).getByLabelText("Nome"), "Reale");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    const fuse = within(dialog).getByLabelText("Ordini reali massimi all'ora (fusibile)");
    await waitFor(() => expect(fuse).toHaveAttribute("aria-invalid", "true"));
    expect(within(dialog).getByText("max_live_orders_per_hour deve essere un intero tra 1 e 1000")).toBeInTheDocument();
  });

  it("adds every symbol of a pasted list", async () => {
    const user = userEvent.setup();
    renderApp("/rischio");
    await user.click(await screen.findByRole("button", { name: "Nuovo profilo" }));
    const dialog = await screen.findByRole("dialog", { name: "Nuovo profilo di rischio" });
    await user.type(within(dialog).getByLabelText("Nome"), "Lista");
    await user.click(within(dialog).getByLabelText("Simboli ammessi"));
    await user.paste("AAPL, MSFT NVDA");
    await user.click(within(dialog).getByRole("button", { name: "Salva" }));
    await waitFor(() => expect(profilePosts).toHaveLength(1));
    expect(profilePosts[0]).toMatchObject({ allowed_symbols: ["AAPL", "MSFT", "NVDA"] });
  });
});

describe("Risk «Blocco aperture»", () => {
  it("sets a halt on one portfolio and shows active halts and history", async () => {
    const user = userEvent.setup();
    const success = vi.spyOn(toast, "success");
    const posts: unknown[] = [];
    server.use(
      http.get("*/api/halts", () =>
        HttpResponse.json({
          active: [haltFixture()],
          history: [
            haltFixture({
              id: 7, active: false, reason: "Vecchio blocco", set_by: "admin:admin@example.com", set_by_name: "admin@example.com",
              cleared_by: "agent:a9", cleared_by_name: "CEO SMTrading", cleared_at: "2026-11-13T12:00:00Z",
            }),
          ],
        }),
      ),
      http.post("*/api/halts", async ({ request }) => {
        posts.push(await request.json());
        return HttpResponse.json({ halt: haltFixture({ id: 8 }), changed: true, canceled: 2 });
      }),
    );
    server.use(
      http.get("*/api/portfolios", () =>
        HttpResponse.json([
          { id: "p1", name: "Alfa", status: "active" },
          { id: "p9", name: "Vecchio", status: "archived" },
        ]),
      ),
    );
    renderApp("/rischio");
    const active = await screen.findByRole("list", { name: "Blocchi attivi" });
    expect(active).toHaveTextContent("Tutti i portafogli — Drawdown oltre il 5%");
    expect(within(active).getByRole("button", { name: "Revoca" })).toBeInTheDocument();
    const history = screen.getByRole("table", { name: "Storico blocchi" });
    expect(history).toHaveTextContent("Vecchio blocco");
    expect(history).toHaveTextContent("CEO SMTrading");

    await user.click(screen.getByRole("button", { name: "Sospendi aperture" }));
    expect(await screen.findByText("Il motivo è obbligatorio")).toBeInTheDocument();
    expect(posts).toEqual([]);

    const scope = screen.getByLabelText("Ambito");
    await waitFor(() => expect(within(scope).getByRole("option", { name: "Alfa" })).toBeInTheDocument());
    expect(within(scope).queryByRole("option", { name: "Vecchio" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Motivo")).toHaveAttribute("maxlength", "500");
    await user.selectOptions(scope, "p1");
    await user.type(screen.getByLabelText("Motivo"), "  Earnings  ");
    await user.click(screen.getByRole("button", { name: "Sospendi aperture" }));
    await waitFor(() => expect(posts).toEqual([{ scope: "portfolio", portfolio_id: "p1", reason: "Earnings" }]));
    await waitFor(() => expect(success).toHaveBeenCalledWith("Blocco aperture attivato (2 ordini annullati)"));
  });
});
