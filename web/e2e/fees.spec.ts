import { expect, test } from "@playwright/test";
import { createPortfolio, login, uniqueName } from "./helpers";

test("regola di commissione per portafoglio e simulazione", async ({ page }) => {
  const name = uniqueName();
  const rule = `${name}-fee`;
  await login(page);
  await createPortfolio(page, name, "5000");

  await page.goto("/commissioni");
  await page.getByRole("button", { name: "Nuova regola" }).click();
  const dlg = page.getByRole("dialog");
  await dlg.getByLabel("Nome", { exact: true }).fill(rule);
  await dlg.getByLabel("Portata").selectOption({ label: "portafoglio" });
  await dlg.getByLabel("Riferimento").selectOption({ label: name });
  await dlg.getByLabel("Commissione fissa").fill("1,50");
  await dlg.getByLabel("Priorità").fill("100");
  await dlg.getByRole("button", { name: "Salva" }).click();
  await expect(dlg).toBeHidden();
  await expect(page.getByRole("row").filter({ hasText: rule })).toBeVisible();

  // simulation (by notional, no market data needed)
  await page.getByLabel("Portafoglio", { exact: true }).selectOption({ label: name });
  await page.getByLabel("Simbolo").fill("AAPL");
  await page.getByLabel("Indica").selectOption({ label: "controvalore" });
  await page.getByLabel("Controvalore").fill("1000,00");
  await page.getByRole("button", { name: "Simula" }).click();
  const result = page.getByRole("region", { name: "Risultato simulazione" });
  await expect(result).toContainText(rule);
  await expect(result).toContainText("1,50");

  // leave the rule inactive so it does not influence later runs
  await page.getByRole("button", { name: `Disattiva ${rule}` }).click();
  await expect(page.getByRole("button", { name: `Disattiva ${rule}` })).toHaveCount(0);
});
