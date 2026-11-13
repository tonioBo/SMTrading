import { expect, test, type Page } from "@playwright/test";
import { login } from "./helpers";

/** Whatever happens, leave the kill-switch OFF: waits for the state to load and, when it
 * is on, for the POST turning it off to finish and the header to show it. */
async function ensureOff(page: Page) {
  await page.goto("/");
  const turnOn = page.getByRole("button", { name: "Attiva kill-switch" });
  const turnOff = page.getByRole("button", { name: "Disattiva kill-switch" });
  await expect(turnOn.or(turnOff)).toBeVisible();
  if (await turnOff.isVisible()) {
    await turnOff.click();
    const posted = page.waitForResponse(
      (r) => r.request().method() === "POST" && new URL(r.url()).pathname === "/api/kill-switch",
    );
    await page.getByRole("dialog").getByRole("button", { name: "Disattiva", exact: true }).click();
    expect((await posted).ok()).toBe(true);
    await expect(turnOn).toBeVisible();
    await expect(page.getByText("Trading attivo")).toBeVisible();
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
