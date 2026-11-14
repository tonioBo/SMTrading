import { expect, test, type Page } from "@playwright/test";
import { freshTotp, loginWithTotp, uniqueName } from "./helpers";

/**
 * «Denaro reale», the whole procedure against the in-memory FakeLiveBroker of the e2e stack
 * (SMT_LIVE_FAKE_BROKER=1 there only; the live gate stays on). scripts/e2e.sh runs it after
 * the other specs and after `python -m smtrading.devtools.live_e2e_seed`, which enables the
 * admin's TOTP with SMT_E2E_TOTP_SECRET, makes Telegram «ready» and creates the empty EUR
 * portfolio «Reale E2E». Canary keys only: nothing real is ever used or reachable.
 */
const PORTFOLIO = "Reale E2E";
const CANARY_KEY = "CANARYKEYID0001";
const CANARY_SECRET = "CANARYSECRET-do-not-use";

async function liveCard(page: Page, name: string) {
  const card = page.locator(`[aria-label="Denaro reale ${name}"]`);
  await expect(card).toBeVisible();
  return card;
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

    // 4. Attiva denaro reale: three confirmations, the name, the TOTP
    await card.getByRole("button", { name: "Attiva denaro reale", exact: true }).click();
    const arm = page.getByRole("dialog");
    await expect(arm.getByRole("note", { name: "Avviso di attivazione" })).toContainText("denaro reale");
    const confirm = arm.getByRole("button", { name: "Attiva", exact: true });
    await arm.getByLabel("Ho letto e accetto i rischi").check();
    await arm.getByLabel("Il conto è dedicato a SMTrading").check();
    await arm.getByLabel("Ho impostato i limiti del profilo di rischio").check();
    await arm.getByLabel("Nome del collegamento").fill(name);
    await expect(confirm).toBeDisabled();
    await arm.getByLabel("Codice TOTP").fill(await freshTotp());
    await confirm.click();
    await expect(arm).toBeHidden();
    await expect(card.getByText(/^Denaro reale attivo dal /)).toBeVisible();

    // collega il portafoglio (TOTP)
    await card.getByLabel("Portafoglio da collegare").selectOption({ label: PORTFOLIO });
    await card.getByRole("button", { name: "Collega portafoglio" }).click();
    const linkDialog = page.getByRole("dialog");
    await linkDialog.getByLabel("Codice TOTP").fill(await freshTotp());
    await linkDialog.getByRole("button", { name: "Collega", exact: true }).click();
    await expect(linkDialog).toBeHidden();
    await expect(card.getByText(`Portafoglio collegato: ${PORTFOLIO}`)).toBeVisible();

    // red bar and «🔴 REALE» badge
    const banner = page.getByRole("status", { name: "Modalità di trading" });
    await expect(banner).toContainText("Denaro reale attivo su 1 portafoglio");
    await page.goto("/portafogli");
    await page.getByRole("link", { name: PORTFOLIO, exact: true }).first().click();
    await expect(page.getByRole("heading", { name: PORTFOLIO })).toBeVisible();
    const badge = page.locator("[data-live-state]").first();
    await expect(badge).toHaveAttribute("data-live-state", "armed");
    await expect(badge).toHaveText(/REALE$/);
    await expect(page.getByRole("status", { name: "Modalità di trading" })).toContainText("Denaro reale attivo");

    // «Disattiva denaro reale»: one click, no dialog, no TOTP
    await page.goto("/broker");
    const armed = await liveCard(page, name);
    const disarmed = page.waitForResponse(
      (r) => r.request().method() === "POST" && new URL(r.url()).pathname.endsWith("/disarm"),
    );
    await armed.getByRole("button", { name: "Disattiva denaro reale", exact: true }).click();
    expect((await disarmed).ok()).toBe(true);
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(armed.getByText(/^Disattivato/)).toBeVisible();
    await expect(page.getByRole("status", { name: "Modalità di trading" })).not.toContainText("Denaro reale attivo");

    // the portfolio stays linked and shows «🔴 REALE · SOSPESO»
    await page.goto("/portafogli");
    await page.getByRole("link", { name: PORTFOLIO, exact: true }).first().click();
    const suspended = page.locator("[data-live-state]").first();
    await expect(suspended).toHaveAttribute("data-live-state", "configured");
    await expect(suspended).toHaveText(/REALE · SOSPESO$/);
  });
});
