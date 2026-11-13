import { expect, test } from "@playwright/test";
import { createPortfolio, login, uniqueName } from "./helpers";

test("portafoglio: deposito e storno", async ({ page }) => {
  const name = uniqueName();
  await login(page);
  await createPortfolio(page, name, "5000");

  // balance after creation
  const cash = page.getByRole("list", { name: "Cash per valuta" });
  await expect(cash).toContainText("5.000,00 €");

  // deposit 100 € (it-IT input)
  await page.getByRole("tab", { name: "Operazioni" }).click();
  await page.getByRole("button", { name: "Nuovo deposito" }).click();
  const dep = page.getByRole("dialog");
  await dep.getByLabel("Importo").fill("100,00");
  await dep.getByRole("button", { name: "Deposita" }).click();
  await expect(dep).toBeHidden();

  await page.getByRole("tab", { name: "Panoramica" }).click();
  await expect(cash).toContainText("5.100,00 €");

  // reverse the deposit with a reason
  await page.getByRole("tab", { name: "Movimenti" }).click();
  const row = page.getByRole("row").filter({ hasText: "Deposito" }).filter({ hasText: "100,00 €" });
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "Storna" }).click();
  const rev = page.getByRole("dialog");
  await rev.getByLabel("Motivazione").fill("e2e: storno di prova");
  await rev.getByRole("button", { name: "Storna" }).click();
  await expect(rev).toBeHidden();

  await page.getByRole("tab", { name: "Panoramica" }).click();
  await expect(cash).toContainText("5.000,00 €");
});
