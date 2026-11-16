import { expect, test, type Locator, type Page } from "@playwright/test";
import { freshTotp, loginWithTotp, uniqueName } from "./helpers";

/**
 * «Denaro reale», the whole procedure against the in-memory FakeLiveBroker of the e2e stack
 * (SMT_LIVE_FAKE_BROKER=1 there only; the live gate stays on). scripts/e2e.sh runs it after
 * the other specs and after `python -m smtrading.devtools.live_e2e_seed`, which enables the
 * admin's TOTP with SMT_E2E_TOTP_SECRET, makes Telegram «ready» and creates the empty EUR
 * portfolios «Reale E2E» (Trading 212) and «Reale E2E eToro A/B» (eToro, two portfolios on
 * one account). Canary keys only: nothing real is ever used or reachable.
 */
const PORTFOLIO = "Reale E2E";
const ETORO_A = "Reale E2E eToro A";
const ETORO_B = "Reale E2E eToro B";
const CANARY_KEY = "CANARYKEYID0001";
const CANARY_SECRET = "CANARYSECRET-do-not-use";
const ETORO_API_KEY = "CANARYETOROKEY0002";
const ETORO_USER_KEY = "CANARYETOROUSER-do-not-use";

async function liveCard(page: Page, name: string) {
  const card = page.locator(`[aria-label="Denaro reale ${name}"]`);
  await expect(card).toBeVisible();
  return card;
}

/** «Collega un portafoglio» on the card of a link in dry run or armed (TOTP). */
async function linkPortfolio(page: Page, card: Locator, portfolio: string) {
  await card.getByRole("button", { name: "Collega un portafoglio" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Portafoglio da collegare").selectOption({ label: portfolio });
  await dialog.getByLabel("Codice TOTP").fill(await freshTotp());
  await dialog.getByRole("button", { name: "Collega", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(card.getByRole("region", { name: "Portafogli collegati" })).toContainText(portfolio);
}

/** «Attiva denaro reale»: three confirmations, the name, the TOTP. */
async function arm(page: Page, card: Locator, name: string) {
  await card.getByRole("button", { name: "Attiva denaro reale", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("note", { name: "Avviso di attivazione" })).toContainText("denaro reale");
  const confirm = dialog.getByRole("button", { name: "Attiva", exact: true });
  await dialog.getByLabel("Ho letto e accetto i rischi").check();
  await dialog.getByLabel("Il conto è dedicato a SMTrading").check();
  await dialog.getByLabel("Ho impostato i limiti del profilo di rischio").check();
  await dialog.getByLabel("Nome del collegamento").fill(name);
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel("Codice TOTP").fill(await freshTotp());
  await confirm.click();
  await expect(dialog).toBeHidden();
  await expect(card.getByText(/^Denaro reale attivo dal /)).toBeVisible();
}

async function openPortfolio(page: Page, name: string) {
  await page.goto("/portafogli");
  await page.getByRole("link", { name, exact: true }).first().click();
  await expect(page.getByRole("heading", { name })).toBeVisible();
}

/** «Disattiva denaro reale»: one click, no dialog, no TOTP. */
async function disarm(page: Page, name: string) {
  await page.goto("/broker");
  const armed = await liveCard(page, name);
  const disarmed = page.waitForResponse(
    (r) => r.request().method() === "POST" && new URL(r.url()).pathname.endsWith("/disarm"),
  );
  await armed.getByRole("button", { name: "Disattiva denaro reale", exact: true }).click();
  expect((await disarmed).ok()).toBe(true);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(armed.getByText(/^Disattivato/)).toBeVisible();
  await expect(page.getByRole("link", { name: /Denaro reale attivo/ })).toHaveCount(0);
}

test.describe("denaro reale @live", () => {
  test.skip(!process.env.SMT_E2E_TOTP_SECRET, "solo dopo il seed e2e (scripts/e2e.sh)");
  test.setTimeout(240_000); // three TOTP codes: up to two 30 s windows of waiting

  test("chiavi canarino, test, prova a secco, attivazione, collegamento e disattivazione", async ({ page }) => {
    await loginWithTotp(page);
    const name = `Reale ${uniqueName()}`;

    // 1. Chiavi (Trading 212: the fake account is in EUR, like the seeded portfolio)
    await page.goto("/broker");
    await page.getByRole("button", { name: "Nuovo collegamento reale" }).click();
    const form = page.getByRole("form", { name: "Nuovo collegamento denaro reale" });
    await form.getByLabel("Nome", { exact: true }).fill(name);
    await form.getByLabel("Trading 212 — denaro reale").check();
    await form.getByLabel("API key", { exact: true }).fill(CANARY_KEY);
    await form.getByLabel("API secret", { exact: true }).fill(CANARY_SECRET);
    await form.getByRole("button", { name: "Salva chiavi" }).click();

    const card = await liveCard(page, name);
    await expect(card.getByText("••••0001")).toBeVisible();
    await expect(page.getByText(CANARY_SECRET)).toHaveCount(0);

    // 2. Test in sola lettura: empty account, 1000 EUR
    await card.getByRole("button", { name: "Esegui il test in sola lettura" }).click();
    const result = card.getByLabel("Risultato del test");
    await expect(result).toBeVisible();
    await expect(result).toContainText("EUR");
    await expect(card.getByText("Il conto non è dedicato")).toHaveCount(0);

    // 3. Prova a secco
    await card.getByRole("button", { name: "Avvia la prova a secco" }).click();
    await expect(card.getByRole("button", { name: "Termina la prova a secco" })).toBeVisible();

    // 4. Attiva denaro reale, then link the portfolio (TOTP)
    await arm(page, card, name);
    await linkPortfolio(page, card, PORTFOLIO);

    // red-dot header badge and «🔴 Trading 212 · reale» badge
    await expect(page.getByRole("link", { name: /Denaro reale attivo su 1 portafoglio/ })).toBeVisible();
    await openPortfolio(page, PORTFOLIO);
    const badge = page.locator("[data-live-state]").first();
    await expect(badge).toHaveAttribute("data-live-state", "armed");
    await expect(badge).toHaveText("🔴 Trading 212 · reale");
    await expect(page.getByRole("link", { name: /Denaro reale attivo/ })).toBeVisible();

    await disarm(page, name);

    // the portfolio stays linked and shows «🔴 Trading 212 · reale · sospeso»
    await openPortfolio(page, PORTFOLIO);
    const suspended = page.locator("[data-live-state]").first();
    await expect(suspended).toHaveAttribute("data-live-state", "configured");
    await expect(suspended).toHaveText("🔴 Trading 212 · reale · sospeso");
  });

  test("eToro: chiavi, test, prova a secco, attivazione, ordine manuale e due portafogli", async ({ page }) => {
    test.setTimeout(360_000); // five TOTP codes
    await loginWithTotp(page);
    const name = `eToro ${uniqueName()}`;

    // 1. Chiavi eToro (the fake account is in USD: the EUR portfolios use the FX rate)
    await page.goto("/broker");
    await page.getByRole("button", { name: "Nuovo collegamento reale" }).click();
    const form = page.getByRole("form", { name: "Nuovo collegamento denaro reale" });
    await form.getByLabel("Nome", { exact: true }).fill(name);
    await form.getByLabel("eToro — denaro reale").check();
    await form.getByLabel("Chiave API (x-api-key)").fill(ETORO_API_KEY);
    await form.getByLabel("Chiave utente reale (x-user-key)").fill(ETORO_USER_KEY);
    await form.getByRole("button", { name: "Salva chiavi" }).click();
    const card = await liveCard(page, name);
    await expect(page.getByText(ETORO_USER_KEY)).toHaveCount(0);

    // 2. test in sola lettura, 3. prova a secco, 4. attivazione (TOTP), collegamento (TOTP)
    await card.getByRole("button", { name: "Esegui il test in sola lettura" }).click();
    await expect(card.getByLabel("Risultato del test")).toContainText("USD");
    await card.getByRole("button", { name: "Avvia la prova a secco" }).click();
    await expect(card.getByRole("button", { name: "Termina la prova a secco" })).toBeVisible();
    await arm(page, card, name);
    await linkPortfolio(page, card, ETORO_A);

    // 5. a manual order: «Nuovo ordine» → anteprima → «🔴 Ordine con denaro reale» (TOTP)
    await openPortfolio(page, ETORO_A);
    await page.getByRole("tab", { name: "Ordini" }).click();
    await page.getByRole("button", { name: "Nuovo ordine" }).click();
    const order = page.getByRole("dialog", { name: "Nuovo ordine" });
    await order.getByLabel("Simbolo").fill("BTC-USD");
    await order.getByRole("list", { name: "Strumenti trovati" }).getByRole("button", { name: /^BTC-USD/ }).click();
    await order.getByLabel("Quantità").fill("0.0005");
    await order.getByRole("button", { name: "Anteprima" }).click();
    await expect(order.getByLabel("Anteprima dell'ordine")).toBeVisible();
    await order.getByRole("button", { name: "Invia" }).click();
    const confirm = page.getByRole("dialog", { name: "🔴 Ordine con denaro reale" });
    await expect(confirm.getByLabel("Riepilogo dell'ordine")).toContainText(ETORO_A);
    const send = confirm.getByRole("button", { name: "Invia ordine reale" });
    await expect(send).toBeDisabled();
    await confirm.getByLabel("Codice TOTP").fill(await freshTotp());
    await send.click();
    await expect(confirm).toBeHidden();
    await page.locator("#order-status").selectOption({ label: "Tutti" });
    const row = page.getByRole("table", { name: "Ordini" }).getByRole("row").filter({ hasText: "BTC-USD" });
    await expect(async () => {
      await page.reload();
      await page.getByRole("tab", { name: "Ordini" }).click();
      await page.locator("#order-status").selectOption({ label: "Tutti" });
      await expect(row.filter({ hasText: "Eseguito" })).toHaveCount(1, { timeout: 3_000 });
    }).toPass({ timeout: 90_000 });
    await expect(row.first()).toContainText("admin");

    // 6. a second portfolio on the same account: a fresh test, then the link (TOTP)
    await page.goto("/broker");
    const card2 = await liveCard(page, name);
    await card2.getByRole("button", { name: "Esegui il test in sola lettura" }).click();
    await expect(card2.getByLabel("Risultato del test")).toBeVisible();
    await linkPortfolio(page, card2, ETORO_B);
    const linked = card2.getByRole("region", { name: "Portafogli collegati" });
    await expect(linked.getByRole("row")).toHaveCount(3); // header + two portfolios
    await expect(linked).toContainText(ETORO_A);
    await expect(linked).toContainText(ETORO_B);
    await expect(linked.getByText(/^Non assegnato: /)).not.toContainText("non disponibile");
    await expect(page.getByRole("link", { name: /Denaro reale attivo su 2 portafogli/ })).toBeVisible();

    await openPortfolio(page, ETORO_B);
    const badge = page.locator("[data-live-state]").first();
    await expect(badge).toHaveAttribute("data-live-state", "armed");
    await expect(badge).toHaveText("🔴 eToro · reale");

    await disarm(page, name);
  });
});
