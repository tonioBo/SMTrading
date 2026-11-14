import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import { MemoryRouter } from "react-router-dom";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import type { LiveLink } from "@/api/types";
import { LiveArmDialog } from "@/components/LiveArmDialog";
import { createQueryClient } from "@/queryClient";
import { providerFuture, routerFuture } from "@/routes";

const link: LiveLink = {
  id: "l1",
  name: "T212 vero",
  kind: "t212_live",
  live_state: "dry_run",
  armed_at: null,
  armed_by: null,
  disarmed_at: null,
  disarmed_reason: null,
  key_masked: "••••0001",
  account_masked: "••••1234",
  snapshot: null,
  last_test_at: "2026-11-14T10:00:00Z",
  health_failures: 0,
  portfolio: { id: "p1", name: "Reale" },
};

let arms: unknown[] = [];
let armReply: () => Response = () => HttpResponse.json({ ...link, live_state: "armed" });
const server = setupServer(
  http.post("*/api/live-links/:id/arm", async ({ request }) => {
    arms.push(await request.json());
    return armReply();
  }),
  http.get("*/api/live-links", () => HttpResponse.json([link])),
  http.get("*/api/live/status", () => HttpResponse.json({ armed_count: 1, dry_run_count: 0, portfolios: [], telegram_ok: true })),
);
beforeAll(() => server.listen({ onUnhandledRequest: "bypass" }));
afterEach(() => {
  server.resetHandlers();
  arms = [];
  armReply = () => HttpResponse.json({ ...link, live_state: "armed" });
});
afterAll(() => server.close());

let closed = 0;
function renderDialog() {
  closed = 0;
  return render(
    <QueryClientProvider client={createQueryClient()}>
      <MemoryRouter future={{ ...routerFuture, ...providerFuture }}>
        <LiveArmDialog link={link} cash="2.000,00 €" profile="Prudente" onClose={() => (closed += 1)} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function fillAll(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, name = "T212 vero") {
  for (const label of ["Ho letto e accetto i rischi", "Il conto è dedicato a SMTrading", "Ho impostato i limiti del profilo di rischio"]) {
    await user.click(within(dialog).getByLabelText(label));
  }
  await user.type(within(dialog).getByLabelText("Nome del collegamento"), name);
  await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
}

describe("LiveArmDialog", () => {
  it("arm dialog renders warning text with placeholders", () => {
    renderDialog();
    const dialog = screen.getByRole("dialog", { name: "Attiva denaro reale · T212 vero" });
    const note = within(dialog).getByRole("note", { name: "Avviso di attivazione" });
    expect(note).toHaveTextContent("Stai per attivare il trading con denaro reale.");
    expect(note).toHaveTextContent(
      "Da questo momento gli agenti potranno inviare ordini veri al tuo conto Trading 212 (••••1234), senza chiederti conferma per ogni ordine, entro i limiti del portafoglio collegato: liquidità 2.000,00 € e profilo di rischio «Prudente».",
    );
    expect(note).toHaveTextContent(
      "Puoi perdere in tutto o in parte il denaro investito. I risultati in simulazione o in paper non garantiscono risultati reali. Prezzi, commissioni e cambi reali possono differire dalle stime.",
    );
    expect(note).toHaveTextContent(
      "Riceverai una notifica Telegram per ogni ordine. Puoi fermare tutto in qualsiasi momento con «Disattiva denaro reale» o con il kill switch.",
    );
    expect(within(dialog).getByText("Scrivi il nome del collegamento e inserisci il codice TOTP per confermare.")).toBeInTheDocument();
    expect(note.textContent).not.toMatch(/\{|\}/);
  });

  it("arm button disabled until checks, name and totp", async () => {
    const user = userEvent.setup();
    renderDialog();
    const dialog = screen.getByRole("dialog");
    const arm = within(dialog).getByRole("button", { name: "Attiva" });
    expect(arm).toBeDisabled();
    await user.click(within(dialog).getByLabelText("Ho letto e accetto i rischi"));
    await user.click(within(dialog).getByLabelText("Il conto è dedicato a SMTrading"));
    expect(arm).toBeDisabled();
    await user.click(within(dialog).getByLabelText("Ho impostato i limiti del profilo di rischio"));
    await user.type(within(dialog).getByLabelText("Nome del collegamento"), "T212 ver");
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "123456");
    expect(arm).toBeDisabled(); // the name is not exact yet
    await user.type(within(dialog).getByLabelText("Nome del collegamento"), "o");
    expect(arm).toBeEnabled();
    await user.clear(within(dialog).getByLabelText("Codice TOTP"));
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "12a34");
    expect(within(dialog).getByLabelText("Codice TOTP")).toHaveValue("1234");
    expect(arm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Codice TOTP"), "56");
    await user.click(arm);
    await waitFor(() =>
      expect(arms).toEqual([{ totp: "123456", confirm_name: "T212 vero", accept_risks: true, dedicated_account: true, limits_set: true }]),
    );
    await waitFor(() => expect(closed).toBe(1));
  });

  it("without the admin TOTP says to enable it, with a link to Impostazioni", async () => {
    armReply = () =>
      HttpResponse.json(
        { code: "FORBIDDEN", message: "Attiva prima l'autenticazione a due fattori", details: { field: "totp" } },
        { status: 403 },
      );
    const user = userEvent.setup();
    renderDialog();
    const dialog = screen.getByRole("dialog");
    await fillAll(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Attiva" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Attiva prima l'autenticazione a due fattori");
    expect(within(alert).getByRole("link", { name: "vai a Impostazioni" })).toHaveAttribute("href", "/impostazioni");
    expect(closed).toBe(0);
  });

  it("shows a refusal of the server (e.g. Telegram not working) and keeps the dialog open", async () => {
    armReply = () =>
      HttpResponse.json(
        { code: "INVALID_ORDER", message: "Telegram deve funzionare: configura il bot", details: { field: "telegram" } },
        { status: 400 },
      );
    const user = userEvent.setup();
    renderDialog();
    const dialog = screen.getByRole("dialog");
    await fillAll(user, dialog);
    await user.click(within(dialog).getByRole("button", { name: "Attiva" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Telegram deve funzionare");
    expect(within(dialog).getByLabelText("Codice TOTP")).toHaveValue(""); // a code is used once
    expect(closed).toBe(0);
  });
});
