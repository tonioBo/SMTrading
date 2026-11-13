import { expect, type Page } from "@playwright/test";

/** Admin credentials come from the environment (scripts/e2e.sh generates them for its isolated stack); never logged. */
export async function login(page: Page): Promise<void> {
  const email = process.env.SMT_ADMIN_EMAIL;
  const password = process.env.SMT_ADMIN_PASSWORD;
  if (!email || !password) throw new Error("SMT_ADMIN_EMAIL/SMT_ADMIN_PASSWORD mancanti: lancia i test con scripts/e2e.sh");
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Accedi" }).click();
  try {
    await expect(page.getByRole("button", { name: "Esci" })).toBeVisible({ timeout: 15_000 });
  } catch (err) {
    throw new Error(`Login non riuscito (${(err as Error).message.split("\n")[0]})`);
  }
}

export const uniqueName = () => `E2E-${Date.now()}`;

/** Creates a portfolio via the UI and lands on its detail page. */
export async function createPortfolio(page: Page, name: string, cash: string): Promise<void> {
  await page.goto("/portafogli");
  await page.getByRole("button", { name: "Nuovo portafoglio" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("Nome", { exact: true }).fill(name);
  await dialog.getByLabel("Saldo iniziale").fill(cash);
  await dialog.getByRole("button", { name: "Crea" }).click();
  await expect(page).toHaveURL(/\/portafogli\/[0-9a-f-]{36}/);
  await expect(page.getByRole("heading", { name })).toBeVisible();
}
