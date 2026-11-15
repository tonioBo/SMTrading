import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import type { Instrument, OrderPreview, Portfolio } from "@/api/types";
import { OrderForm } from "@/components/OrderForm";
import { createQueryClient } from "@/queryClient";
import { commonHandlers } from "@/test/handlers";

const portfolio = { id: "p1", name: "Alfa", base_currency: "EUR" } as Portfolio;

const aapl: Instrument = {
  symbol: "AAPL",
  name: "Apple Inc.",
  asset_class: "us_equity",
  market: "US",
  currency: "USD",
  lot_size: "1",
  tick_size: "0.01",
  shortable: true,
  active: true,
  provider_symbols: {},
  source: "catalog",
  added_by: null,
};

const okPreview: OrderPreview = {
  ok: true,
  code: null,
  message: null,
  estimated_price: "190.5",
  estimated_notional_eur: "525.3",
  estimated_fee: { rule_id: "f1", amount: "1", currency: "EUR" },
  estimated_fx_fee_eur: "0",
  buying_power_after_eur: "16473.7",
  real: false,
  currency: "EUR",
};

let preview: OrderPreview | (() => Response) = okPreview;
let previews: unknown[] = [];
let places: { key: string | null; body: unknown }[] = [];
let placeResponse: () => Response = () => HttpResponse.json({ id: "o9", status: "new" });
let searches: string[] = [];

const server = setupServer(
  ...commonHandlers,
  http.get("*/api/instruments", ({ request }) => {
    const q = new URL(request.url).searchParams.get("query") ?? "";
    searches.push(q);
    const items = "AAPL".startsWith(q.toUpperCase()) ? [aapl] : [];
    return HttpResponse.json({ items, total: items.length, offset: 0, limit: 10 });
  }),
  http.post("*/api/portfolios/p1/orders/preview", async ({ request }) => {
    previews.push(await request.json());
    return typeof preview === "function" ? preview() : HttpResponse.json(preview);
  }),
  http.post("*/api/portfolios/p1/orders", async ({ request }) => {
    places.push({ key: request.headers.get("Idempotency-Key"), body: await request.json() });
    return placeResponse();
  }),
  http.get("*/api/portfolios/p1*", () => HttpResponse.json({})),
  http.get("*/api/portfolios", () => HttpResponse.json([])),
  http.get("*/api/dashboard", () => HttpResponse.json({})),
);
beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  preview = okPreview;
  previews = [];
  places = [];
  placeResponse = () => HttpResponse.json({ id: "o9", status: "new" });
  searches = [];
});
afterAll(() => server.close());

function setup() {
  const onClose = vi.fn();
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <OrderForm portfolio={portfolio} onClose={onClose} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  const dialog = screen.getByRole("dialog", { name: "Nuovo ordine" });
  return { user, onClose, dialog };
}

async function pickAapl(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement) {
  await user.type(within(dialog).getByLabelText("Simbolo"), "aap");
  await user.click(await within(dialog).findByRole("button", { name: /AAPL/ }));
}

describe("OrderForm", () => {
  it("preview required before send", async () => {
    const { user, onClose, dialog } = setup();
    await pickAapl(user, dialog);
    await user.type(within(dialog).getByLabelText("Quantità"), "2,5");
    const send = within(dialog).getByRole("button", { name: "Invia" });
    expect(send).toBeDisabled();
    expect(within(dialog).getByText("Fai prima l'anteprima")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    const shown = await within(dialog).findByLabelText("Anteprima dell'ordine");
    expect(shown).toHaveTextContent("Prezzo stimato190,50 $");
    expect(shown).toHaveTextContent("Controvalore525,30 €");
    expect(shown).toHaveTextContent("Commissioni1,00 €");
    expect(shown).toHaveTextContent("Liquidità dopo l'ordine16.473,70 €");
    expect(previews).toEqual([{ symbol: "AAPL", side: "buy", type: "market", tif: "gtc", qty: "2.5" }]);
    expect(send).toBeEnabled();
    expect(within(dialog).queryByText("Fai prima l'anteprima")).not.toBeInTheDocument();
    await user.click(send);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(places).toEqual([
      { key: expect.stringMatching(/^[0-9a-f-]{36}$/), body: { symbol: "AAPL", side: "buy", type: "market", tif: "gtc", qty: "2.5" } },
    ]);
    expect(searches).not.toContain(""); // no search before typing
  });

  it("shows the refusal of the preview and keeps Invia disabled", async () => {
    preview = { ...okPreview, ok: false, code: "RISK_LIMIT_EXCEEDED", message: "order notional exceeds 5000 EUR" };
    const { user, dialog } = setup();
    await pickAapl(user, dialog);
    await user.type(within(dialog).getByLabelText("Quantità"), "100");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("order notional exceeds 5000 EUR");
    expect(within(dialog).getByRole("button", { name: "Invia" })).toBeDisabled();
  });

  it("shows a 400 of the preview under its field", async () => {
    preview = () => HttpResponse.json({ code: "INVALID_ORDER", message: "limit_price obbligatorio", details: { field: "limit_price" } }, { status: 400 });
    const { user, dialog } = setup();
    await pickAapl(user, dialog);
    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "limit");
    await user.type(within(dialog).getByLabelText("Quantità"), "1");
    await user.type(within(dialog).getByLabelText("Prezzo limite"), "180");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await waitFor(() => expect(within(dialog).getByLabelText("Prezzo limite")).toHaveAccessibleDescription("limit_price obbligatorio"));
  });

  it("asks to pick a catalogue instrument before the preview", async () => {
    const { user, dialog } = setup();
    await user.type(within(dialog).getByLabelText("Simbolo"), "zzz");
    expect(await within(dialog).findByText("Nessuno strumento trovato")).toBeInTheDocument();
    expect(within(dialog).getByText("Nessuno strumento trovato").closest("[aria-live]")).toHaveAttribute("aria-live", "polite");
    await user.type(within(dialog).getByLabelText("Quantità"), "1");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    expect(within(dialog).getByLabelText("Simbolo")).toHaveAccessibleDescription("Scegli uno strumento dal catalogo");
    expect(previews).toEqual([]);
  });

  it("changing a field invalidates preview", async () => {
    const { user, dialog } = setup();
    await pickAapl(user, dialog);
    await user.type(within(dialog).getByLabelText("Quantità"), "2");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    expect(within(dialog).getByRole("button", { name: "Invia" })).toBeEnabled();
    await user.selectOptions(within(dialog).getByLabelText("Lato"), "sell");
    expect(within(dialog).queryByLabelText("Anteprima dell'ordine")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Invia" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    await user.type(within(dialog).getByLabelText("Quantità"), "5");
    expect(within(dialog).getByRole("button", { name: "Invia" })).toBeDisabled();
  });

  it("notional only for market", async () => {
    const { user, dialog } = setup();
    await pickAapl(user, dialog);
    await user.selectOptions(within(dialog).getByLabelText("Ordine per"), "notional");
    await user.type(within(dialog).getByLabelText("Controvalore"), "500");
    expect(within(dialog).getByText("= 500,00 $")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await waitFor(() => expect(previews).toEqual([{ symbol: "AAPL", side: "buy", type: "market", tif: "gtc", notional: "500" }]));

    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "stop_limit");
    expect(within(dialog).queryByLabelText("Ordine per")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Controvalore")).not.toBeInTheDocument();
    expect(within(dialog).getByLabelText("Quantità")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Prezzo stop")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Prezzo limite")).toBeInTheDocument();

    await user.selectOptions(within(dialog).getByLabelText("Tipo"), "trailing_stop");
    await user.type(within(dialog).getByLabelText("Quantità"), "3");
    await user.type(within(dialog).getByLabelText("Distanza trailing"), "2");
    await user.selectOptions(within(dialog).getByLabelText("Durata"), "day");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await waitFor(() => expect(previews).toHaveLength(2));
    expect(previews[1]).toEqual({ symbol: "AAPL", side: "buy", type: "trailing_stop", tif: "day", qty: "3", trail_pct: "2" });
  });

  it("real portfolio opens live confirm with totp and amount", async () => {
    preview = { ...okPreview, real: true };
    const { user, onClose, dialog } = setup();
    await pickAapl(user, dialog);
    await user.type(within(dialog).getByLabelText("Quantità"), "2");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    const confirm = await screen.findByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    const summary = within(confirm).getByLabelText("Riepilogo dell'ordine");
    expect(summary).toHaveTextContent("Importo stimato381,00 $≈ 525,30 € (stima)"); // 2 × 190,5 $
    expect(summary).toHaveTextContent("DurataFino a revoca (gtc)");
    const send = within(confirm).getByRole("button", { name: "Invia ordine reale" });
    expect(send).toBeDisabled();
    expect(places).toEqual([]);
    await user.type(within(confirm).getByLabelText("Codice TOTP"), "123456");
    await user.click(send);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(places).toEqual([
      { key: expect.stringMatching(/^[0-9a-f-]{36}$/), body: { symbol: "AAPL", side: "buy", type: "market", tif: "gtc", qty: "2", totp: "123456" } },
    ]);
  });

  it("totp error shown under field and the next code is sent with the same key", async () => {
    preview = { ...okPreview, real: true };
    placeResponse = () => HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP non valido", details: { field: "totp" } }, { status: 403 });
    const { user, onClose, dialog } = setup();
    await pickAapl(user, dialog);
    await user.type(within(dialog).getByLabelText("Quantità"), "2");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    const confirm = await screen.findByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    const field = within(confirm).getByLabelText("Codice TOTP");
    await user.type(field, "111111");
    await user.click(within(confirm).getByRole("button", { name: "Invia ordine reale" }));
    await waitFor(() => expect(field).toHaveAccessibleDescription("codice TOTP non valido"));
    expect(field).toHaveValue("");
    placeResponse = () => HttpResponse.json({ id: "o9", status: "new" });
    await user.type(field, "222222");
    await user.click(within(confirm).getByRole("button", { name: "Invia ordine reale" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(places.map((p) => (p.body as { totp: string }).totp)).toEqual(["111111", "222222"]);
    expect(places[0].key).toBe(places[1].key);
  });

  async function previewed(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, qty = "2") {
    await pickAapl(user, dialog);
    await user.type(within(dialog).getByLabelText("Quantità"), qty);
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
  }

  it("a real notional order shows the controvalore as entered", async () => {
    preview = { ...okPreview, real: true };
    const { user, dialog } = setup();
    await pickAapl(user, dialog);
    await user.selectOptions(within(dialog).getByLabelText("Ordine per"), "notional");
    await user.type(within(dialog).getByLabelText("Controvalore"), "500");
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    const confirm = await screen.findByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    const summary = within(confirm).getByLabelText("Riepilogo dell'ordine");
    expect(summary).toHaveTextContent("Controvalore500,00 $");
    expect(summary).toHaveTextContent("Importo stimato500,00 $≈ 525,30 € (stima)");
  });

  it("a send that turns out to need the TOTP asks for a new preview", async () => {
    placeResponse = () =>
      HttpResponse.json({ code: "FORBIDDEN", message: "codice TOTP obbligatorio", details: { field: "totp" } }, { status: 403 });
    const { user, onClose, dialog } = setup();
    await previewed(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Il portafoglio ora è collegato al denaro reale: rifai l'anteprima");
    expect(within(dialog).queryByText("codice TOTP obbligatorio")).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText("Anteprima dell'ordine")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Invia" })).toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it("a definitive refusal requires a fresh preview with a new key", async () => {
    placeResponse = () =>
      HttpResponse.json({ code: "INSUFFICIENT_BUYING_POWER", message: "insufficient buying power", details: { order_id: "o8" } }, { status: 400 });
    const { user, onClose, dialog } = setup();
    await previewed(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("insufficient buying power");
    expect(within(dialog).getByRole("button", { name: "Invia" })).toBeDisabled();
    placeResponse = () => HttpResponse.json({ id: "o9", status: "new" });
    await user.click(within(dialog).getByRole("button", { name: "Anteprima" }));
    await within(dialog).findByLabelText("Anteprima dell'ordine");
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(places).toHaveLength(2);
    expect(places[0].key).not.toBe(places[1].key);
  });

  it("a server error keeps the preview and the key for a retry", async () => {
    placeResponse = () => HttpResponse.json({ code: "INTERNAL", message: "errore temporaneo", details: null }, { status: 500 });
    const { user, onClose, dialog } = setup();
    await previewed(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("errore temporaneo");
    placeResponse = () => HttpResponse.json({ id: "o9", status: "new" });
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(places).toHaveLength(2);
    expect(places[0].key).toBe(places[1].key);
  });

  it("a real order refused for good goes back to the form for a fresh preview", async () => {
    preview = { ...okPreview, real: true };
    placeResponse = () => HttpResponse.json({ code: "RISK_LIMIT_EXCEEDED", message: "limite superato", details: { order_id: "o8" } }, { status: 400 });
    const { user, dialog } = setup();
    await previewed(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    const confirm = await screen.findByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    await user.type(within(confirm).getByLabelText("Codice TOTP"), "123456");
    await user.click(within(confirm).getByRole("button", { name: "Invia ordine reale" }));
    const form = await screen.findByRole("dialog", { name: "Nuovo ordine" });
    expect(within(form).getByRole("alert")).toHaveTextContent("limite superato");
    expect(within(form).getByRole("button", { name: "Invia" })).toBeDisabled();
  });

  it("does not close while the order is being sent", async () => {
    let release: () => void = () => undefined;
    let arrived = false;
    server.use(
      http.post("*/api/portfolios/p1/orders", async () => {
        arrived = true;
        await new Promise<void>((r) => (release = r));
        return HttpResponse.json({ id: "o9", status: "new" });
      }),
    );
    const { user, onClose, dialog } = setup();
    await previewed(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Invia" }));
    await waitFor(() => expect(arrived).toBe(true));
    await user.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Nuovo ordine" })).toBeInTheDocument();
    release();
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });
});
