import { expect, test, type Page } from "@playwright/test";
import { createPortfolio, login, uniqueName } from "./helpers";

/**
 * «Nuovo ordine» from the portal on a portfolio without a broker: preview, send, the order
 * in the list, «Annulla» on an open limit. BTC-USD: the crypto market is always open.
 */
const SYMBOL = "BTC-USD";

async function openOrderForm(page: Page) {
  await page.getByRole("button", { name: "Nuovo ordine" }).click();
  const dialog = page.getByRole("dialog", { name: "Nuovo ordine" });
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("Simbolo").fill(SYMBOL);
  await dialog.getByRole("list", { name: "Strumenti trovati" }).getByRole("button", { name: new RegExp(`^${SYMBOL}`) }).click();
  return dialog;
}

test("nuovo ordine: anteprima, invio, elenco e annullo di un limite aperto", async ({ page }) => {
  test.setTimeout(90_000);
  await login(page);
  await createPortfolio(page, uniqueName(), "1000");
  await page.getByRole("tab", { name: "Ordini" }).click();

  // 1. a market buy: «Invia» only after an ok «Anteprima»
  let dialog = await openOrderForm(page);
  await dialog.getByLabel("Quantità").fill("0.0001");
  await expect(dialog.getByRole("button", { name: "Invia" })).toBeDisabled();
  await dialog.getByRole("button", { name: "Anteprima" }).click();
  await expect(dialog.getByLabel("Anteprima dell'ordine")).toContainText("Prezzo stimato");
  await dialog.getByRole("button", { name: "Invia" }).click();
  await expect(dialog).toBeHidden();

  const orders = page.getByRole("table", { name: "Ordini" });
  await page.locator("#order-status").selectOption({ label: "Eseguiti" });
  await expect(orders.getByRole("row").filter({ hasText: SYMBOL }).filter({ hasText: "Eseguito" })).toHaveCount(1);

  // 2. a limit buy far below the price: it stays open, then «Annulla»
  dialog = await openOrderForm(page);
  await dialog.getByLabel("Tipo").selectOption({ label: "Limite" });
  await dialog.getByLabel("Quantità").fill("0.0001");
  await dialog.getByLabel("Prezzo limite").fill("1000");
  await dialog.getByRole("button", { name: "Anteprima" }).click();
  await expect(dialog.getByLabel("Anteprima dell'ordine")).toBeVisible();
  await dialog.getByRole("button", { name: "Invia" }).click();
  await expect(dialog).toBeHidden();

  await page.locator("#order-status").selectOption({ label: "Aperti" });
  const open = orders.getByRole("row").filter({ hasText: SYMBOL }).filter({ hasText: "In attesa" });
  await expect(open).toHaveCount(1);
  await open.getByRole("button", { name: "Annulla" }).click();
  const confirm = page.getByRole("dialog", { name: "Annullare l'ordine?" });
  await confirm.getByRole("button", { name: "Annulla ordine" }).click();
  await expect(confirm).toBeHidden();
  await expect(open).toHaveCount(0);

  await page.locator("#order-status").selectOption({ label: "Annullati" });
  await expect(orders.getByRole("row").filter({ hasText: SYMBOL }).filter({ hasText: "Annullato" })).toHaveCount(1);
});
