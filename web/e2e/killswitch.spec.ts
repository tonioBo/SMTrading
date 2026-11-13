import { expect, test, type Page } from "@playwright/test";
import { login } from "./helpers";

/** Whatever happens, leave the kill-switch OFF. */
async function ensureOff(page: Page) {
  await page.goto("/");
  const off = page.getByRole("button", { name: "Disattiva kill-switch" });
  if (await off.isVisible().catch(() => false)) {
    await off.click();
    await page.getByRole("dialog").getByRole("button", { name: "Disattiva", exact: true }).click();
  }
}

test.describe("kill-switch", () => {
  test.afterEach(async ({ page }) => {
    await ensureOff(page);
  });

  test("attiva e disattiva con badge nell'header", async ({ page }) => {
    await login(page);
    await ensureOff(page);
    await expect(page.getByText("Trading attivo")).toBeVisible();

    await page.getByRole("button", { name: "Attiva kill-switch" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Attiva", exact: true }).click();
    await expect(page.getByText("Kill-switch attivo")).toBeVisible();

    await page.getByRole("button", { name: "Disattiva kill-switch" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Disattiva", exact: true }).click();
    await expect(page.getByText("Trading attivo")).toBeVisible();
  });
});
